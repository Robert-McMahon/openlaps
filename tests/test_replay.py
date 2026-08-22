"""tools/replay.py against a real nats-server: candump-only replay at an
accelerated rate, consumer-side assertions on the resulting stream.

Skipped automatically when docker is unavailable -- see the ``nats_url``
fixture in conftest. The incremental-flush tests below need no docker: they
are about P4.6's 24.7 h parity replay, which cannot hold its ~3.5 M batches
in memory and so streams them out of the pipeline in pieces.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import nats
from conftest import TEST_TELE_MAX_BYTES
from nats.js import api

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import _bench  # noqa: E402
import replay  # noqa: E402
from _bench import DEFAULT_PROFILE, load_catalog  # noqa: E402

from agent.pipeline import TickBatch  # noqa: E402
from collectors.clock import MonotonicWallClock  # noqa: E402
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
            "--tele-max-bytes",
            str(TEST_TELE_MAX_BYTES),
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


def _gps_rows(count: int, *, interval_s: float = 0.05):
    """A lap-ish arc of fixes at the June-2025 event's ~20 Hz."""
    return [
        (
            index * interval_s,
            -31.6636 + index * 2e-5,
            115.7867 + index * 1e-5,
            90.0,
            120.0,
        )
        for index in range(count)
    ]


def test_replay_streams_batches_instead_of_materialising_them(tmp_path: Path):
    profile, catalog, _ = load_catalog(DEFAULT_PROFILE, vehicle=None, state_dir=str(tmp_path))

    batches = replay.replay_cycle(
        profile,
        catalog,
        MonotonicWallClock(),
        candump_frames=[],
        nmea_lines=[],
        gps_rows=_gps_rows(64),
    )

    assert inspect.isgenerator(batches)
    assert list(batches), "the trace should have produced batches"


def test_incremental_flushes_never_reuse_a_message_id(tmp_path: Path):
    """The flush seam is where a silent loss would live.

    `msg_id` is ``<source-class>:<epoch-ms>`` and TELE deduplicates on it for
    two minutes, so two batches sharing an id means the second is discarded by
    JetStream without a word. A trace long enough to cross several flush
    boundaries has to come out with every id distinct.
    """
    profile, catalog, _ = load_catalog(DEFAULT_PROFILE, vehicle=None, state_dir=str(tmp_path))
    fixes = replay.FLUSH_EVERY_FIXES * 3 + 17

    batches = list(
        replay.replay_cycle(
            profile,
            catalog,
            MonotonicWallClock(),
            candump_frames=[],
            nmea_lines=[],
            gps_rows=_gps_rows(fixes),
        )
    )

    ids = [batch.msg_id for batch in batches]
    assert len(ids) == len(set(ids))
    assert len(batches) > fixes, "each fix should yield at least its own serial batch"


def test_flush_waits_for_a_gap_of_at_least_one_tick():
    """Flushing mid-tick would split one window across two batches -- and two
    batches anchored in the same window carry the same `msg_id`."""
    tight = _gps_rows(4, interval_s=replay.TICK_MS / 2_000.0)
    spaced = _gps_rows(4, interval_s=0.05)

    assert replay._flush_due(tight, 1, staged=True) is False
    assert replay._flush_due(spaced, 1, staged=True) is True
    assert replay._flush_due(spaced, 1, staged=False) is False
    # Never at the final row: the closing flush covers it.
    assert replay._flush_due(spaced, len(spaced) - 1, staged=True) is False


class _FakePublisher:
    """A publisher that reports a lag until it has been asked often enough."""

    def __init__(self, *, lag_reports: int) -> None:
        self._remaining = lag_reports
        self.submitted: list[str] = []
        self.polls = 0

    def publish_lag_ms(self) -> float:
        self.polls += 1
        if self._remaining > 0:
            self._remaining -= 1
            return _bench.MAX_PUBLISH_LAG_MS + 1.0
        return 0.0

    def submit(self, batch) -> None:
        self.submitted.append(batch.msg_id)


def _batch(index: int):
    return TickBatch(
        source_class="serial0",
        payload=b"x",
        epoch_unix_ms=index,
        epoch_mono_ns=index * 1_000_000,
        msg_id=f"serial0:{index}",
    )


def test_publishing_waits_for_a_lagging_publisher_rather_than_letting_it_shed():
    """`submit` sheds oldest-first past its byte budget: an unpaced replay that
    outran JetStream would lose batches silently."""
    publisher = _FakePublisher(lag_reports=3)

    submitted = _bench.publish_paced(publisher, [_batch(0), _batch(1)], rate=0.0)

    assert submitted == 2
    assert publisher.submitted == ["serial0:0", "serial0:1"]
    assert publisher.polls > 2, "the lag should have been waited on, not ignored"


def test_a_wedged_publisher_does_not_stall_the_replay_forever(monkeypatch):
    monkeypatch.setattr(_bench, "MAX_PUBLISH_STALL_S", 0.05)
    publisher = _FakePublisher(lag_reports=10_000)

    submitted = _bench.publish_paced(publisher, [_batch(0)], rate=0.0)

    assert submitted == 1, "past the stall bound the loss becomes visible, not invisible"


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
