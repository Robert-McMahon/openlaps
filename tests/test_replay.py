"""tools/replay.py against a real nats-server: candump-only replay at an
accelerated rate, consumer-side assertions on the resulting stream.

Skipped automatically when docker is unavailable -- see the ``nats_url``
fixture in conftest.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import nats
from nats.js import api

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import replay  # noqa: E402
from _bench import DEFAULT_PROFILE  # noqa: E402

from core.pb import telemetry_pb2 as pb  # noqa: E402

VEHICLE = "example-club-racer"


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


def test_candump_replay_delivers_the_expected_channels_and_samples(nats_url, tmp_path: Path):
    exit_code = replay.main(
        [
            "--server",
            nats_url,
            "--profile",
            str(DEFAULT_PROFILE),
            "--rate",
            "50",
            "--can-frames",
            "2000",
            "--nmea",
            "",
            "--gps-trace",
            "",
            "--state-dir",
            str(tmp_path),
        ]
    )
    assert exit_code == 0

    messages = asyncio.run(_collect_all(nats_url, f"tele.{VEHICLE}.>"))
    registries: dict[int, pb.ChannelRegistry] = {}
    for message in messages:
        if message.headers.get("Openlaps-Msg-Type") == "registry":
            registry = pb.ChannelRegistry()
            registry.ParseFromString(message.data)
            registries[registry.registry_seq] = registry
    assert registries, "the registry should have been published"

    samples_by_channel: dict[str, list[object]] = {}
    for message in messages:
        if message.headers.get("Openlaps-Msg-Type") != "batch":
            continue
        batch = pb.SampleBatch()
        batch.ParseFromString(message.data)
        names = {channel.id: channel.name for channel in registries[batch.registry_seq].channels}
        for sample in batch.samples:
            kind = sample.WhichOneof("value")
            samples_by_channel.setdefault(names[sample.channel_id], []).append(
                getattr(sample, kind)
            )

    # The candump fixture decodes against the Haltech DBCs, same as
    # test_agent_e2e's candump-only assertions.
    rpm_values = samples_by_channel["car.rpm"]
    assert rpm_values and all(0 <= value <= 16000 for value in rpm_values)
    assert "car.battery_v" in samples_by_channel
    # No NMEA/GPS source was selected: no position channel should appear.
    assert "position.lat" not in samples_by_channel


def test_no_sources_selected_is_a_clean_error(tmp_path: Path):
    exit_code = replay.main(
        [
            "--server",
            "nats://127.0.0.1:1",  # unreachable; must not even be dialed
            "--profile",
            str(DEFAULT_PROFILE),
            "--candump",
            "",
            "--nmea",
            "",
            "--gps-trace",
            "",
            "--state-dir",
            str(tmp_path),
        ]
    )
    assert exit_code == 2
