"""`tools/bench_check.py`: the day-one gate on the bench's signal mix.

A bench that has silently lost GPS or the IMU still produces a perfectly
plausible bandwidth figure, of the wrong signal set. These tests cover the
arithmetic that is supposed to notice -- prediction from the fixtures,
classification of source refs, and the measured-vs-predicted gate -- on a
host with neither vcan nor a receiver, using the same collector injection
points `VehicleAgent` takes.
"""

from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path

import can
import pytest
import serial
from conftest import EXAMPLE_PROFILE

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import bench_check  # noqa: E402
import bench_gps  # noqa: E402

from agent.agent import agent_derived_channels  # noqa: E402
from core.catalog import build_runtime_catalog  # noqa: E402
from core.config import load_profile  # noqa: E402

BENCH_PROFILE = Path(__file__).parents[1] / "profiles" / "example-club-racer-bench"
FIXTURES = Path(__file__).parent / "fixtures" / "candump"
CANDUMPS = [FIXTURES / "candump-sample.log", FIXTURES / "candump-imu-sample.log"]
STATS = Path(__file__).parent / "fixtures" / "mqtt_payload_stats.json"


def _catalog(tmp_path: Path, profile_dir: Path = BENCH_PROFILE):
    profile = load_profile(profile_dir)
    names = [bus.name for bus in profile.vehicle.buses]
    names += [source.name for source in profile.vehicle.serial]
    if profile.vehicle.host.enabled:
        names.append("host")
    catalog = build_runtime_catalog(
        profile,
        state_path=tmp_path / "registry-state.json",
        derived_channels=agent_derived_channels(names),
    )
    return profile, catalog


# -- classification -------------------------------------------------------------


@pytest.mark.parametrize(
    ("source_ref", "expected"),
    [
        ("can0:haltech.ENGINE1.RPM", (bench_check.CAN_CLASS, "haltech")),
        ("can0:wideband.WIDEBAND1.LAMBDA", (bench_check.CAN_CLASS, "wideband")),
        # The IMU rolls up separately because §2 models it separately.
        ("can0:imu.FDI_IMU_ACCEL.IMU_ACCEL_X", (bench_check.IMU_CLASS, "imu")),
        ("serial0:um980.RMC.lat", (bench_check.GPS_CLASS, "um980")),
        ("host:cpu.percent", (bench_check.HOST_CLASS, bench_check.HOST_CLASS)),
        ("derived:lap.number", (bench_check.DERIVED_CLASS, bench_check.DERIVED_CLASS)),
    ],
)
def test_source_refs_land_in_the_class_the_model_compares_them_against(source_ref, expected):
    assert bench_check.classify(source_ref) == expected


# -- prediction -------------------------------------------------------------------


def test_the_candump_fixtures_predict_the_rate_they_actually_contain(tmp_path: Path):
    """Decoded through the real collector and divided by each log's own span."""
    profile, catalog = _catalog(tmp_path)

    mix = bench_check.predict_can(profile, catalog, CANDUMPS)

    assert mix.rate(bench_check.CAN_CLASS) == pytest.approx(2034.6, rel=0.01)
    assert mix.rate(bench_check.IMU_CLASS) == pytest.approx(752.5, rel=0.01)
    devices = mix.device_rates()
    assert set(devices) == {"haltech", "haltech2", "wideband", "imu"}
    assert devices["haltech"] == pytest.approx(1264.6, rel=0.01)


def test_a_fixture_with_no_usable_timeline_is_refused(tmp_path: Path):
    profile, catalog = _catalog(tmp_path)
    frozen = tmp_path / "frozen.log"
    frozen.write_text(
        "(1000.000000) can0 360#0000000000000000\n(1000.000000) can0 360#0000000000000000\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="do not advance"):
        bench_check.predict_can(profile, catalog, [frozen])


def test_gps_is_predicted_from_a_sentence_the_real_decoder_accepts(tmp_path: Path):
    profile, catalog = _catalog(tmp_path)

    # Five mapped `position.*` channels, one per RMC field the decoder emits.
    assert bench_check.predict_gps(profile, catalog, 50.0) == 250.0
    assert bench_check.predict_gps(profile, catalog, 10.0) == 50.0


def test_host_metrics_contribute_nothing_because_the_catalog_maps_none(tmp_path: Path):
    profile, catalog = _catalog(tmp_path)

    assert bench_check.predict_host(profile, catalog) == 0.0


def test_the_bench_profile_and_the_example_profile_predict_the_same_mix(tmp_path: Path):
    """The whole point of the bench profile, seen from the checking tool."""
    bench_profile, bench_catalog = _catalog(tmp_path / "bench", BENCH_PROFILE)
    example_profile, example_catalog = _catalog(tmp_path / "example", EXAMPLE_PROFILE)

    bench = bench_check.predict_can(bench_profile, bench_catalog, CANDUMPS)
    example = bench_check.predict_can(example_profile, example_catalog, CANDUMPS)

    assert bench.device_rates() == example.device_rates()
    assert bench_check.predict_gps(bench_profile, bench_catalog, 50.0) == (
        bench_check.predict_gps(example_profile, example_catalog, 50.0)
    )


def test_the_modelled_rates_are_link_budget_section_2s_own_numbers():
    modelled = bench_check.modelled_rates(STATS)

    assert modelled[bench_check.CAN_CLASS] == pytest.approx(2787.2, rel=0.001)
    assert modelled[bench_check.IMU_CLASS] == 1000.0
    assert modelled[bench_check.GPS_CLASS] == 300.0
    assert sum(modelled.values()) == pytest.approx(4087.2, rel=0.001)


# -- the live run ------------------------------------------------------------------


class _ReplayBus:
    """A `can.BusABC` stand-in that loops the fixture frames as fast as asked."""

    def __init__(self, frames: list[can.Message]) -> None:
        self._frames = frames
        self._index = 0

    def recv(self, timeout: float | None = None) -> can.Message | None:
        message = self._frames[self._index % len(self._frames)]
        self._index += 1
        # Restamp so the collector's frame clock sees a live bus rather than
        # a recording that keeps starting over.
        return can.Message(
            timestamp=time.time(),
            arbitration_id=message.arbitration_id,
            is_extended_id=message.is_extended_id,
            data=message.data,
        )

    def shutdown(self) -> None:
        pass


class _RmcPort:
    """A serial port that hands back one valid RMC per `readline`."""

    def __init__(self) -> None:
        self.fix = (-31.6725, 115.7815, 120.0, 90.0)

    def reset_input_buffer(self) -> None:
        pass

    def write(self, data: bytes) -> int:
        return len(data)

    def readline(self, size: int = -1) -> bytes:
        return bench_gps.rmc_sentence(*self.fix)

    def close(self) -> None:
        pass


def _frames(path: Path) -> list[can.Message]:
    with path.open() as handle:
        return list(can.CanutilsLogReader(handle))


def test_a_live_run_counts_every_class_and_produces_batches(tmp_path: Path):
    profile, catalog = _catalog(tmp_path)
    frames = _frames(CANDUMPS[0])[:400] + _frames(CANDUMPS[1])[:100]

    result = bench_check.run_live(
        profile,
        catalog,
        seconds=1.0,
        tick_ms=20,
        stop=threading.Event(),
        bus_factory=lambda config: _ReplayBus(frames),
        serial_factory=lambda config: _RmcPort(),
    )

    assert result.mix.rate(bench_check.CAN_CLASS) > 0
    assert result.mix.rate(bench_check.IMU_CLASS) > 0
    assert result.mix.rate(bench_check.GPS_CLASS) > 0
    assert result.batches > 0
    assert result.payload_bytes > 0
    # The fixtures carry plenty the catalog deliberately ignores; that is
    # normal, and it must be visible rather than counted as offered load.
    assert result.unmapped_refs > 0
    assert result.mix.emitted_rate(bench_check.CAN_CLASS) > result.mix.rate(bench_check.CAN_CLASS)


def test_an_unreachable_source_is_zero_and_says_why(tmp_path: Path):
    """The failure the tool exists for: a class that is simply not there."""
    profile, catalog = _catalog(tmp_path)

    def no_bus(config):
        raise OSError(f"no such interface {config.interface}")

    def no_port(config):
        raise serial.SerialException(f"no such port {config.port}")

    result = bench_check.run_live(
        profile,
        catalog,
        seconds=0.5,
        tick_ms=20,
        stop=threading.Event(),
        bus_factory=no_bus,
        serial_factory=no_port,
    )
    out = io.StringIO()
    failures = bench_check.report_run(
        result,
        {bench_check.CAN_CLASS: 1397.0, bench_check.GPS_CLASS: 250.0},
        0.10,
        20,
        out,
    )

    assert result.mix.rate(bench_check.CAN_CLASS) == 0.0
    assert result.transport_notes["can0"].startswith("open_failures=")
    assert len(failures) == 2
    assert "canplayer" in " ".join(failures)
    assert "bench_gps" in " ".join(failures)


def test_a_class_inside_tolerance_does_not_fail_the_gate():
    result = bench_check.RunResult(mix=bench_check.Mix(elapsed_s=10.0))
    for _ in range(13_500):
        result.mix.add("can0:haltech.ENGINE1.RPM", mapped=True)

    out = io.StringIO()
    within = bench_check.report_run(result, {bench_check.CAN_CLASS: 1397.0}, 0.10, 20, out)
    beyond = bench_check.report_run(result, {bench_check.CAN_CLASS: 1600.0}, 0.10, 20, out)

    assert within == []
    assert len(beyond) == 1
    assert beyond[0].startswith("can: measured 1350.0/s against 1600.0/s predicted")


def test_a_discarded_tick_window_fails_the_gate_and_is_named():
    """The mix alone looks merely quiet; an encode failure has to say so."""
    result = bench_check.RunResult(mix=bench_check.Mix(elapsed_s=10.0), encode_failures=3)
    for _ in range(13_970):
        result.mix.add("can0:haltech.ENGINE1.RPM", mapped=True)

    out = io.StringIO()
    failures = bench_check.report_run(result, {bench_check.CAN_CLASS: 1397.0}, 0.10, 20, out)

    assert "encode_failures=3" in out.getvalue()
    assert len(failures) == 1
    assert failures[0].startswith("encode: 3 tick window(s) discarded")


def test_a_class_the_model_does_not_cover_is_reported_without_gating():
    """Derived lap/timing channels are real offered load and no model's business."""
    result = bench_check.RunResult(mix=bench_check.Mix(elapsed_s=1.0))
    result.mix.add("derived:timing.distance", mapped=True)

    out = io.StringIO()
    failures = bench_check.report_run(result, {}, 0.10, 20, out)

    assert failures == []
    assert "derived" in out.getvalue()


# -- the command line ----------------------------------------------------------------


def test_predict_mode_needs_no_hardware_and_reports_the_model_gap():
    out = io.StringIO()

    status = bench_check.main(["--predict", "--profile", str(BENCH_PROFILE)], out=out)

    text = out.getvalue()
    assert status == 0
    assert "vcan0" in text
    assert "Predicted bench mix vs. LINK_BUDGET.md §2 model" in text
    assert "3037.0" in text and "4087.2" in text


def test_the_model_gap_can_be_made_fatal_for_those_who_want_it():
    out = io.StringIO()

    status = bench_check.main(
        ["--predict", "--profile", str(BENCH_PROFILE), "--model-tolerance", "0.05"], out=out
    )

    assert status == 1
    assert "FAIL: predicted" in out.getvalue()


def test_predict_mode_leaves_the_profiles_registry_generation_alone(tmp_path: Path):
    """Running the check must never bump the generation of the run after it."""
    state = BENCH_PROFILE / ".registry-state.json"
    before = state.read_bytes() if state.exists() else None

    bench_check.main(["--predict", "--profile", str(BENCH_PROFILE)], out=io.StringIO())

    after = state.read_bytes() if state.exists() else None
    assert after == before


def test_a_missing_profile_is_an_error_not_a_traceback():
    assert bench_check.main(["--predict", "--profile", "/nonexistent"], out=io.StringIO()) == 2
