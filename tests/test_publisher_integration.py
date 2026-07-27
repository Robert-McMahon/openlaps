"""Publisher integration tests against a real JetStream (docker nats-server).

Skipped automatically when docker (or the nats image) is unavailable — see
the ``nats_url`` fixture in conftest.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import nats
import pytest
from conftest import EXAMPLE_PROFILE
from nats.js import api

from agent.pipeline import Pipeline, TickBatch
from agent.publisher import (
    MSG_TYPE_BATCH,
    MSG_TYPE_HEADER,
    MSG_TYPE_REGISTRY,
    JetStreamPublisher,
)
from core.catalog import build_runtime_catalog
from core.pb import telemetry_pb2 as pb
from core.samples import Sample

VEHICLE = "example-club-racer"
MS = 1_000_000  # ns


def _wall(t_mono_ns: int) -> float:
    return 1_780_000_000_000.0 + t_mono_ns / 1e6


@pytest.fixture
def catalog(tmp_path: Path):
    return build_runtime_catalog(
        EXAMPLE_PROFILE, state_path=tmp_path / "registry-state.json", created_unix_ms=1
    )


def _publisher(nats_url: str, catalog, **kwargs) -> JetStreamPublisher:
    return JetStreamPublisher(
        nats_url=nats_url,
        vehicle_id=VEHICLE,
        registry_payload=catalog.registry.SerializeToString(),
        registry_interval_s=3600.0,
        **kwargs,
    )


def _wait_until(predicate, timeout_s: float = 15.0, message: str = "condition"):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for {message}")


async def _collect(url: str, subject: str, idle_timeout_s: float = 2.0):
    """Read every retained message on ``subject`` until the stream goes idle."""
    client = await nats.connect(url)
    try:
        js = client.jetstream()
        subscription = await js.subscribe(
            subject, ordered_consumer=True, deliver_policy=api.DeliverPolicy.ALL
        )
        messages = []
        while True:
            try:
                messages.append(await subscription.next_msg(timeout=idle_timeout_s))
            except TimeoutError:
                return messages
    finally:
        await client.close()


async def _js_publish(url: str, subject: str, payload: bytes) -> None:
    client = await nats.connect(url)
    try:
        await client.jetstream().publish(subject, payload)
    finally:
        await client.close()


async def _core_publish(url: str, subject: str, payload: bytes) -> None:
    client = await nats.connect(url)
    try:
        await client.publish(subject, payload)
        await client.flush()
    finally:
        await client.close()


def _registry_by_seq(messages, seq: int) -> pb.ChannelRegistry:
    """The consumer-side recovery walk: find the registry matching a seq."""
    for message in messages:
        if message.headers and message.headers.get(MSG_TYPE_HEADER) == MSG_TYPE_REGISTRY:
            registry = pb.ChannelRegistry()
            registry.ParseFromString(message.data)
            if registry.registry_seq == seq:
                return registry
    pytest.fail(f"no ChannelRegistry with seq {seq} found in the stream")


def test_publish_consume_decode_round_trip(nats_url, catalog):
    publisher = _publisher(nats_url, catalog)
    publisher.start()
    try:
        _wait_until(lambda: publisher.connected, message="publisher connect")
        _wait_until(lambda: publisher.registry_publishes >= 1, message="registry publish")

        pipeline = Pipeline(catalog, tick_ms=20)
        t0 = 50_000 * MS
        pipeline.ingest("can0", Sample("can0:haltech.ENGINE1.ENGINE_SPEED", t0, _wall(t0), 4500.0))
        pipeline.ingest(
            "can0",
            Sample("can0:haltech.MISC4.BATTERY_VOLTAGE", t0 + 3 * MS, _wall(t0 + 3 * MS), 13.8),
        )
        batches = pipeline.flush(_wall)
        assert len(batches) == 1
        for batch in batches:
            publisher.submit(batch)
        assert publisher.drain(timeout_s=10.0)
    finally:
        publisher.stop()

    messages = asyncio.run(_collect(nats_url, f"tele.{VEHICLE}.>"))
    batch_messages = [
        m for m in messages if m.headers and m.headers.get(MSG_TYPE_HEADER) == MSG_TYPE_BATCH
    ]
    assert len(batch_messages) == 1
    assert batch_messages[0].subject == f"tele.{VEHICLE}.can0"

    decoded = pb.SampleBatch()
    decoded.ParseFromString(batch_messages[0].data)
    # Registry recovery: resolve the batch's exact seq from the same stream.
    registry = _registry_by_seq(messages, decoded.registry_seq)
    names = {channel.id: channel.name for channel in registry.channels}
    by_name = {names[sample.channel_id]: sample for sample in decoded.samples}
    assert by_name["car.rpm"].d == 4500.0
    assert by_name["car.battery_v"].d == 13.8
    # Exact capture-time reconstruction across the wire.
    rpm_capture_ms = decoded.batch_epoch_unix_ms + by_name["car.rpm"].t_offset_us / 1000.0
    assert rpm_capture_ms == pytest.approx(_wall(t0), abs=0.5)
    volt_capture_ms = decoded.batch_epoch_unix_ms + by_name["car.battery_v"].t_offset_us / 1000.0
    assert volt_capture_ms == pytest.approx(_wall(t0 + 3 * MS), abs=0.5)


def test_duplicate_msg_id_is_deduped_by_jetstream(nats_url, catalog):
    publisher = _publisher(nats_url, catalog)
    publisher.start()
    try:
        _wait_until(lambda: publisher.connected, message="publisher connect")
        payload = pb.SampleBatch(registry_seq=catalog.registry.registry_seq).SerializeToString()
        batch = TickBatch(
            source_class="can0",
            payload=payload,
            epoch_unix_ms=1_780_000_000_000,
            epoch_mono_ns=1,
            msg_id="can0:1780000000000",
        )
        # A reconnect redelivery is the same batch submitted twice.
        publisher.submit(batch)
        publisher.submit(batch)
        assert publisher.drain(timeout_s=10.0)
    finally:
        publisher.stop()

    messages = asyncio.run(_collect(nats_url, f"tele.{VEHICLE}.can0"))
    assert len(messages) == 1


def test_stream_provisioning_is_idempotent_across_restarts(nats_url, catalog):
    first = _publisher(nats_url, catalog)
    first.start()
    _wait_until(lambda: first.registry_publishes >= 1, message="first registry publish")
    first.stop()

    # A second agent lifetime provisions the same streams and republishes.
    second = _publisher(nats_url, catalog)
    second.start()
    try:
        _wait_until(lambda: second.registry_publishes >= 1, message="second registry publish")
    finally:
        second.stop()

    messages = asyncio.run(_collect(nats_url, f"tele.{VEHICLE}.catalog"))
    registries = [m for m in messages if m.headers.get(MSG_TYPE_HEADER) == MSG_TYPE_REGISTRY]
    assert len(registries) == 2
    registry = _registry_by_seq(messages, catalog.registry.registry_seq)
    assert registry.vehicle_id == VEHICLE


def test_session_last_value_semantics_and_rtcm_forwarding(nats_url, catalog):
    sessions: list[bytes] = []
    corrections: list[bytes] = []
    publisher = _publisher(
        nats_url, catalog, on_session=sessions.append, on_rtcm=corrections.append
    )
    publisher.start()
    try:
        _wait_until(lambda: publisher.connected, message="publisher connect")
        _wait_until(lambda: publisher.registry_publishes >= 1, message="streams provisioned")

        session_1 = json.dumps({"session_id": "s-1", "driver": "Alice"}).encode()
        asyncio.run(_js_publish(nats_url, f"cmd.{VEHICLE}.session", session_1))
        _wait_until(lambda: sessions, message="live session delivery")
        assert json.loads(sessions[-1])["driver"] == "Alice"

        asyncio.run(_core_publish(nats_url, f"rtcm.{VEHICLE}", b"\xd3\x00\x13rtcm-frame"))
        _wait_until(lambda: corrections, message="rtcm delivery")
        assert corrections[0].startswith(b"\xd3")
    finally:
        publisher.stop()

    # Supersede the session while no agent is running, then restart: the
    # CMD stream's last-value retention hands the newest state straight to
    # the fresh subscriber (replaces the old MQTT retained flag).
    session_2 = json.dumps({"session_id": "s-2", "driver": "Bob"}).encode()
    asyncio.run(_js_publish(nats_url, f"cmd.{VEHICLE}.session", session_2))

    late_sessions: list[bytes] = []
    restarted = _publisher(nats_url, catalog, on_session=late_sessions.append)
    restarted.start()
    try:
        _wait_until(lambda: late_sessions, message="last-value session on restart")
        assert json.loads(late_sessions[-1]) == {"session_id": "s-2", "driver": "Bob"}
        assert len(late_sessions) == 1
    finally:
        restarted.stop()
