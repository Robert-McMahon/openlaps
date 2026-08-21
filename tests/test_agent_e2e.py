"""Mini end-to-end: real collectors -> pipeline -> real JetStream -> decode.

Replays the candump fixture through the real CAN collector and a generated
Wanneroo lap (real RMC sentences) through the real serial collector/NMEA
decoder, drives the real pipeline (timing tap included), publishes to a
real nats-server, then plays the consumer role: recover the registry by
seq and assert channel values, capture timestamps, and — the point of the
whole exercise — lap events arriving as derived channels.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from pathlib import Path

import can
import nats
import pytest
import serial
from conftest import EXAMPLE_PROFILE, lap_path, rmc_sentence, wanneroo_track
from nats.js import api

from agent.agent import AgentSettings, VehicleAgent
from agent.clock import SystemClock
from agent.pipeline import Pipeline
from agent.publisher import (
    MSG_TYPE_BATCH,
    MSG_TYPE_HEADER,
    MSG_TYPE_REGISTRY,
    JetStreamPublisher,
)
from agent.queues import SampleQueue
from agent.timing_app import build_lap_timing_app
from collectors.can import CanCollector
from collectors.serial.transport import SerialCollector
from core.catalog import build_runtime_catalog
from core.config import load_profile
from core.pb import telemetry_pb2 as pb

CANDUMP = Path(__file__).parent / "fixtures" / "candump" / "candump-sample.log"
VEHICLE = "example-club-racer"
CAN_FRAMES = 2000


async def _collect_all(url: str, subject: str):
    client = await nats.connect(url)
    try:
        js = client.jetstream()
        subscription = await js.subscribe(
            subject, ordered_consumer=True, deliver_policy=api.DeliverPolicy.ALL
        )
        messages = []
        while True:
            try:
                messages.append(await subscription.next_msg(timeout=2.0))
            except TimeoutError:
                return messages
    finally:
        await client.close()


def test_candump_and_nmea_replay_end_to_end(nats_url, tmp_path: Path):
    profile = load_profile(EXAMPLE_PROFILE)
    catalog = build_runtime_catalog(
        profile, state_path=tmp_path / "registry-state.json", created_unix_ms=1
    )
    timing_app = build_lap_timing_app(
        catalog, "position.*", "Wanneroo", str(EXAMPLE_PROFILE / "tracks")
    )
    timing_app.apply_session({"session_id": "e2e-race", "driver": "Alice"})
    clock = SystemClock()
    pipeline = Pipeline(catalog, tick_ms=20, timing_app=timing_app)
    publisher = JetStreamPublisher(
        nats_url=nats_url,
        vehicle_id=VEHICLE,
        registry_payload=catalog.registry.SerializeToString(),
        registry_interval_s=3600.0,
    )
    publisher.start()
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and publisher.registry_publishes < 1:
            time.sleep(0.05)
        assert publisher.registry_publishes >= 1, "publisher never reached JetStream"

        # Real CAN collector, fed the recorded fixture frames.
        can_queue = SampleQueue("can0")
        can_collector = CanCollector(
            profile.vehicle.buses[0], EXAMPLE_PROFILE, can_queue.put, wall_clock=clock
        )
        with CANDUMP.open() as log:
            frames = list(itertools.islice(can.CanutilsLogReader(log), CAN_FRAMES))
        assert can_collector.replay(frames) > 0
        assert can_collector.decode_stats.decoded_frames > 0

        # Real serial collector + NMEA decoder, fed a generated Wanneroo lap.
        serial_queue = SampleQueue("serial0")
        serial_collector = SerialCollector(
            profile.vehicle.serial[0], serial_queue.put, wall_clock=clock
        )
        base_mono_ns = time.monotonic_ns()
        fixes = lap_path(wanneroo_track(), laps=1)
        for t_s, lat, lon in fixes:
            emitted = serial_collector.handle_line(
                rmc_sentence(lat, lon), t_mono_ns=base_mono_ns + int(t_s * 1e9)
            )
            assert emitted == 5  # lat, lon, speed, heading, mode

        # One deterministic pipeline pass over everything captured.
        for queue in (can_queue, serial_queue):
            assert queue.dropped == 0
            for sample in queue.drain():
                pipeline.ingest(queue.source_class, sample)
        batches = pipeline.flush(clock)
        assert pipeline.encode_failures == 0
        assert {batch.source_class for batch in batches} == {"can0", "serial0", "derived"}
        for batch in batches:
            publisher.submit(batch)
        assert publisher.drain(timeout_s=20.0)
        assert publisher.publish_drops == 0
    finally:
        publisher.stop()

    # ---- consumer side ------------------------------------------------------
    messages = asyncio.run(_collect_all(nats_url, f"tele.{VEHICLE}.>"))
    by_type: dict[str, list] = {MSG_TYPE_REGISTRY: [], MSG_TYPE_BATCH: []}
    for message in messages:
        by_type[message.headers[MSG_TYPE_HEADER]].append(message)

    registries = {}
    for message in by_type[MSG_TYPE_REGISTRY]:
        registry = pb.ChannelRegistry()
        registry.ParseFromString(message.data)
        registries[registry.registry_seq] = registry

    samples_by_channel: dict[str, list[tuple[float, object]]] = {}
    for message in by_type[MSG_TYPE_BATCH]:
        batch = pb.SampleBatch()
        batch.ParseFromString(message.data)
        assert batch.format_version == 1
        # Registry recovery is a hard equality check on the seq.
        registry = registries[batch.registry_seq]
        names = {channel.id: channel.name for channel in registry.channels}
        for sample in batch.samples:
            assert 0 <= sample.t_offset_us < 20_000
            capture_ms = batch.batch_epoch_unix_ms + sample.t_offset_us / 1000.0
            kind = sample.WhichOneof("value")
            samples_by_channel.setdefault(names[sample.channel_id], []).append(
                (capture_ms, getattr(sample, kind))
            )

    # CAN channels decoded from the recorded bus, in physical range.
    rpm_values = [value for _, value in samples_by_channel["car.rpm"]]
    assert rpm_values and all(0 <= value <= 16000 for value in rpm_values)
    assert "car.battery_v" in samples_by_channel

    # GNSS channels decoded from the generated sentences.
    lats = [value for _, value in samples_by_channel["position.lat"]]
    assert len(lats) == len(fixes)
    assert all(value == pytest.approx(-31.66, abs=0.02) for value in lats)

    # And the point of it all: lap events arrived as derived channels.
    events = [json.loads(value) for _, value in samples_by_channel["lap.event"]]
    lap_completed = [event for event in events if event["type"] == "lap_completed"]
    assert len(lap_completed) == 1
    assert lap_completed[0]["valid"] is True
    assert lap_completed[0]["lap_time"] == pytest.approx(93.0, abs=1.0)
    assert lap_completed[0]["driver"] == "Alice"
    assert lap_completed[0]["session_id"] == "e2e-race"
    assert samples_by_channel["lap.number"][-1][1] == 2
    assert max(value for _, value in samples_by_channel["timing.distance"]) > 1000.0


def test_full_agent_lifecycle_against_real_nats(nats_url, tmp_path: Path):
    """The whole VehicleAgent, hardware absent: health telemetry still flows."""

    def no_bus(config):
        raise OSError(f"no such interface {config.interface}")

    def no_port(config):
        raise serial.SerialException(f"no such port {config.port}")

    settings = AgentSettings(
        profile_dir=EXAMPLE_PROFILE,
        nats_url=nats_url,
        tick_ms=10,
        state_dir=tmp_path / "state",
        health_interval_s=0.1,
    )
    agent = VehicleAgent(settings, bus_factory=no_bus, serial_factory=no_port)
    agent.start()
    try:
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and agent.publisher.published < 5:
            time.sleep(0.05)
        assert agent.publisher.published >= 5, "health batches never reached JetStream"
    finally:
        agent.stop()

    messages = asyncio.run(_collect_all(nats_url, f"tele.{VEHICLE}.>"))
    registries: dict[int, pb.ChannelRegistry] = {}
    statuses: list[str] = []
    unmapped: list[int] = []
    for message in messages:
        if message.headers[MSG_TYPE_HEADER] == MSG_TYPE_REGISTRY:
            registry = pb.ChannelRegistry()
            registry.ParseFromString(message.data)
            registries[registry.registry_seq] = registry
    for message in messages:
        if message.headers[MSG_TYPE_HEADER] != MSG_TYPE_BATCH:
            continue
        batch = pb.SampleBatch()
        batch.ParseFromString(message.data)
        names = {channel.id: channel.name for channel in registries[batch.registry_seq].channels}
        for sample in batch.samples:
            if names[sample.channel_id] == "sys.agent.status":
                statuses.append(sample.s)
            elif names[sample.channel_id] == "sys.agent.unmapped_refs":
                unmapped.append(sample.i)

    assert statuses, "sys.agent.status heartbeats should be in the stream"
    assert json.loads(statuses[0])["state"] == "running"
    # Shutdown published a final stopping status (docs/AGENT_DESIGN.md).
    assert json.loads(statuses[-1])["state"] == "stopping"
    # The host collector ran against real psutil; its refs are unmapped in
    # the example catalog and visibly counted rather than silently lost.
    assert unmapped and unmapped[-1] > 0
