"""The compiled CAN ingest path must be indistinguishable from the Python one.

Recorded buses are run through both: `CanCollector` feeding `Pipeline` (the
reference), and the `openlaps_canfast` engine driven by the plan
`collectors.can_fast` builds. The batches have to match byte for byte, the
tapped samples value for value and the counters number for number, with the
pipeline flushed mid-window the way the live agent flushes it.
"""

from __future__ import annotations

from pathlib import Path

import can
import pytest

from agent.pipeline import Pipeline
from collectors import can_fast
from collectors.can import CanCollector
from core.catalog import build_runtime_catalog
from core.config import load_profile
from core.samples import Sample

pytestmark = pytest.mark.skipif(
    not can_fast.available(), reason="openlaps_canfast is not built (rust/canfast)"
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PROFILE = ROOT / "profiles" / "example-club-racer"
CANDUMPS = sorted((ROOT / "tests" / "fixtures" / "candump").glob("*.log"))
TICK_MS = 20
# A fixed realtime/monotonic pair, so both paths compute the same wall epoch.
REALTIME_NS = 1_794_600_000_123_456_789
MONOTONIC_NS = 5_000_000_000
BASE_MONO_NS = 4_000_000_000


def _wall(t_mono_ns: int) -> float:
    return (REALTIME_NS + t_mono_ns - MONOTONIC_NS) / 1e6


def run_both(frames: list[can.Message], tmp_path: Path, *, flush_every: int, tap_names=()):
    """Feed ``frames`` to both paths, flushing every ``flush_every`` frames."""
    profile = load_profile(EXAMPLE_PROFILE)
    catalog = build_runtime_catalog(profile, state_path=tmp_path / "registry-state.json")
    bus = profile.vehicle.buses[0]
    taps = {catalog.channel_ids[name] for name in tap_names}

    pipeline = Pipeline(catalog, tick_ms=TICK_MS)
    reference_taps: list[tuple[int, int, object]] = []

    def emit(sample: Sample) -> None:
        channel_id = catalog.source_map[sample.source_ref][0]
        if channel_id in taps:
            reference_taps.append((channel_id, sample.t_mono_ns, sample.value))
        pipeline.ingest(bus.name, sample)

    reference = CanCollector(bus, profile.path, emit, wanted=catalog.source_map.keys())
    fast = can_fast.FastCanCollector(bus, profile.path, catalog, tick_ms=TICK_MS, taps=taps)

    reference_batches: list[tuple[bytes, int, int]] = []
    fast_batches: list[tuple[bytes, int, int]] = []
    fast_taps: list[tuple[int, int, object]] = []

    def flush() -> None:
        reference_batches.extend(
            (batch.payload, batch.epoch_unix_ms, batch.epoch_mono_ns)
            for batch in pipeline.flush(_wall)
        )
        tapped, batches = fast.drain(REALTIME_NS, MONOTONIC_NS)
        fast_taps.extend((tap.channel_id, tap.t_mono_ns, tap.value) for tap in tapped)
        fast_batches.extend(
            (batch.payload, batch.epoch_unix_ms, batch.epoch_mono_ns) for batch in batches
        )

    first = frames[0].timestamp
    for index, frame in enumerate(frames, start=1):
        t_mono_ns = BASE_MONO_NS + round((frame.timestamp - first) * 1e9)
        if frame.is_error_frame or frame.is_remote_frame:
            continue
        data = bytes(frame.data)
        reference._emit_frame(frame.arbitration_id, bool(frame.is_extended_id), data, t_mono_ns)
        fast.feed(frame.arbitration_id, bool(frame.is_extended_id), data, t_mono_ns)
        if index % flush_every == 0:
            flush()
    flush()
    return reference, pipeline, reference_batches, reference_taps, fast, fast_batches, fast_taps


@pytest.mark.parametrize("log", CANDUMPS, ids=lambda path: path.stem)
@pytest.mark.parametrize("flush_every", [1, 37, 10_000])
def test_recorded_buses_come_out_byte_identical(log: Path, flush_every: int, tmp_path: Path):
    frames = list(can.CanutilsLogReader(str(log)))
    tap_names = ("car.accel_x", "car.gyro_z", "car.rpm")

    reference, pipeline, ref_batches, ref_taps, fast, fast_batches, fast_taps = run_both(
        frames, tmp_path, flush_every=flush_every, tap_names=tap_names
    )

    assert len(ref_batches) > 0
    assert fast_batches == ref_batches
    assert fast_taps == ref_taps
    assert [type(value) for _, _, value in fast_taps] == [type(v) for _, _, v in ref_taps]
    ours, theirs = fast.decode_stats, reference.decode_stats
    for name in (
        "frames",
        "decoded_frames",
        "unknown_frames",
        "malformed_frames",
        "unwanted_frames",
        "samples",
    ):
        assert getattr(ours, name) == getattr(theirs, name), name
    assert fast.rbe_suppressed == pipeline.rbe_suppressed
    assert fast.encode_failures == pipeline.encode_failures == 0


def test_a_short_frame_and_an_undefined_multiplexer_value_are_malformed_in_both(tmp_path: Path):
    frames = [
        can.Message(arbitration_id=0x360, data=bytes(3), timestamp=1.0, is_extended_id=False),
        can.Message(arbitration_id=0x360, data=bytes(8), timestamp=1.01, is_extended_id=False),
        # PD16A_OUTPUT_STATUS with every multiplexer bit set: no such output.
        can.Message(arbitration_id=0x6D3, data=b"\xff" * 8, timestamp=1.02, is_extended_id=False),
        can.Message(arbitration_id=0x7F1, data=bytes(8), timestamp=1.03, is_extended_id=False),
    ]

    reference, _, ref_batches, _, fast, fast_batches, _ = run_both(frames, tmp_path, flush_every=1)

    assert fast_batches == ref_batches
    assert fast.decode_stats.malformed_frames == reference.decode_stats.malformed_frames >= 1
    assert fast.decode_stats.unknown_frames == reference.decode_stats.unknown_frames == 1


def test_the_plan_refuses_what_it_cannot_express(tmp_path: Path):
    profile = load_profile(EXAMPLE_PROFILE)
    catalog = build_runtime_catalog(profile, state_path=tmp_path / "registry-state.json")
    plan = can_fast.build_plan(profile.vehicle.buses[0], profile.path, catalog, tick_ms=TICK_MS)

    assert plan["registry_seq"] == catalog.registry.registry_seq
    assert len(plan["messages"]) > 20
    assert plan["unwanted"], "the example car broadcasts frames nothing maps"
    with pytest.raises(ValueError, match="tick"):
        can_fast.openlaps_canfast.CanIngest(
            '{"registry_seq":1,"tick_ns":1,"messages":[],"unwanted":[]}'
        )
