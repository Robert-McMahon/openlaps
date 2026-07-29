"""CAN collector decode, source-ref formatting, and read-loop resilience tests."""

from __future__ import annotations

import collections
import logging
import threading
import time
from pathlib import Path

import can
import pytest

from collectors.can import (
    CanCollector,
    CanDecoderError,
    MonotonicWallClock,
)
from core.catalog import build_runtime_catalog
from core.config import BusConfig, DbcConfig, load_profile
from core.samples import Sample

EXAMPLE_PROFILE = Path(__file__).parents[1] / "profiles" / "example-club-racer"
CANDUMP_DIR = Path(__file__).parent / "fixtures" / "candump"
ENGINE_LOG = CANDUMP_DIR / "candump-sample.log"
IMU_LOG = CANDUMP_DIR / "candump-imu-sample.log"

# Physical limits (not expected operating values) for signals that must appear
# in any capture taken with the car powered up. The engine capture was taken at
# walking pace while loading a trailer, so keep the bounds wide.
EXPECTED_RANGES = {
    "can0:haltech.ENGINE1.ENGINE_SPEED": (0, 16000),
    "can0:haltech.ENGINE1.THROTTLE_POSITION": (0, 100),
    "can0:haltech.MISC4.BATTERY_VOLTAGE": (8, 16),
    "can0:haltech.TEMPERATURE1.COOLANT_TEMPERATURE": (233, 423),
    "can0:haltech2.PD16A_DIAGNOSTICS.PD16A_BATTERY_VOLTAGE": (8, 16),
    "can0:haltech2.PD16A_DIAGNOSTICS.PD16A_TOTAL_CURRENT": (0, 300),
    "can0:wideband.WB1_LAMBDA.WB1_LAMBDA_1": (0, 32),
}

_MINIMAL_DBC = """VERSION ""

NS_ :

BS_:

BU_: NODE

BO_ 256 STATUS: 2 NODE
 SG_ LEVEL : 0|16@1+ (1,0) [0|65535] "" NODE
"""


class _FakeBus:
    """A scripted stand-in for `can.BusABC` used by the read-loop tests."""

    def __init__(self, script: list[can.Message | Exception | None]) -> None:
        self.script = list(script)
        self.shutdowns = 0

    def recv(self, timeout: float | None = None) -> can.Message | None:
        if not self.script:
            raise can.CanOperationError("bus-off")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def shutdown(self) -> None:
        self.shutdowns += 1


def _bus_config(dbcs: list[DbcConfig], *, interface: str = "can0") -> BusConfig:
    return BusConfig(name="can0", interface=interface, bitrate=1_000_000, dbcs=dbcs)


def _frame(arbitration_id: int, data: bytes, *, timestamp: float = 1.0, **kwargs) -> can.Message:
    kwargs.setdefault("is_extended_id", False)
    return can.Message(arbitration_id=arbitration_id, data=data, timestamp=timestamp, **kwargs)


def _collector(emit, **kwargs) -> CanCollector:
    profile = load_profile(EXAMPLE_PROFILE)
    return CanCollector(profile.vehicle.buses[0], profile.path, emit, **kwargs)


def _replay(log_path: Path, **kwargs) -> tuple[CanCollector, list[Sample]]:
    samples: list[Sample] = []
    collector = _collector(samples.append, **kwargs)
    collector.replay(can.CanutilsLogReader(str(log_path)))
    return collector, samples


@pytest.fixture(scope="module")
def engine_replay() -> tuple[CanCollector, list[Sample]]:
    return _replay(ENGINE_LOG)


@pytest.fixture(scope="module")
def imu_replay() -> tuple[CanCollector, list[Sample]]:
    return _replay(IMU_LOG)


def test_recorded_engine_capture_decodes_into_plausible_samples(engine_replay):
    collector, samples = engine_replay
    stats = collector.decode_stats

    assert stats.frames == 8000
    assert stats.decoded_frames + stats.unknown_frames == stats.frames
    assert stats.malformed_frames == 0
    assert stats.samples == len(samples)

    values = collections.defaultdict(list)
    for sample in samples:
        values[sample.source_ref].append(sample.value)
    for source_ref, (low, high) in EXPECTED_RANGES.items():
        observed = values[source_ref]
        assert observed, f"{source_ref} never appeared in the capture"
        assert low <= min(observed) and max(observed) <= high, f"{source_ref} out of range"


def test_recorded_capture_uses_the_device_alias_of_the_dbc_that_decoded_it(
    engine_replay, imu_replay
):
    _, engine_samples = engine_replay
    _, imu_samples = imu_replay

    def devices(samples: list[Sample]) -> set[str]:
        return {sample.source_ref.split(":", 1)[1].split(".", 1)[0] for sample in samples}

    assert devices(engine_samples) == {"haltech", "haltech2", "wideband"}
    assert devices(imu_samples) == {"imu"}
    assert all(sample.source_ref.startswith("can0:") for sample in engine_samples)
    assert all(len(sample.source_ref.split(".")) == 3 for sample in engine_samples)


def test_replayed_source_refs_resolve_through_the_runtime_catalog(
    engine_replay, imu_replay, tmp_path: Path
):
    profile = load_profile(EXAMPLE_PROFILE)
    runtime = build_runtime_catalog(profile, state_path=tmp_path / "registry-state.json")
    _, engine_samples = engine_replay
    _, imu_samples = imu_replay

    mapped = {
        runtime.source_map[sample.source_ref][1].name
        for sample in engine_samples + imu_samples
        if sample.source_ref in runtime.source_map
    }

    # The collector decodes everything its DBCs know; the catalog is what
    # narrows it, so only a subset maps -- but the canonical channels the
    # example profile promises must all be present.
    assert {"car.rpm", "car.coolant_temp", "car.battery_v", "car.wb1_lambda1"} <= mapped
    assert {"car.accel_x", "car.gyro_z", "car.roll", "car.imu_temp"} <= mapped


def test_unmapped_signals_are_still_emitted(engine_replay, tmp_path: Path):
    profile = load_profile(EXAMPLE_PROFILE)
    runtime = build_runtime_catalog(profile, state_path=tmp_path / "registry-state.json")
    _, samples = engine_replay

    unmapped = {s.source_ref for s in samples} - set(runtime.source_map)

    # PD16A analog inputs are deliberately unmapped (wiring-dependent, see
    # docs/CATALOG.md) -- the collector must not be the thing that filters.
    assert "can0:haltech2.PD16A_AVI_VOLTAGES.PD16A_AVI_1_VOLTAGE" in unmapped


def test_colliding_message_names_stay_separate_per_device(tmp_path: Path):
    for name in ("first.dbc", "second.dbc"):
        (tmp_path / name).write_text(_MINIMAL_DBC, encoding="utf-8")
    config = _bus_config(
        [DbcConfig(device="ecu", file="first.dbc"), DbcConfig(device="pdm", file="second.dbc")]
    )
    samples: list[Sample] = []
    collector = CanCollector(config, tmp_path, samples.append)

    emitted = collector.handle_message(_frame(0x100, b"\x39\x30"))

    assert emitted == 2
    assert [sample.source_ref for sample in samples] == [
        "can0:ecu.STATUS.LEVEL",
        "can0:pdm.STATUS.LEVEL",
    ]
    assert {sample.value for sample in samples} == {0x3039}
    assert collector.decode_stats.decoded_frames == 1


def test_unknown_frame_ids_are_counted_and_reported_once(tmp_path: Path, caplog):
    (tmp_path / "only.dbc").write_text(_MINIMAL_DBC, encoding="utf-8")
    samples: list[Sample] = []
    collector = CanCollector(
        _bus_config([DbcConfig(device="ecu", file="only.dbc")]), tmp_path, samples.append
    )

    with caplog.at_level(logging.INFO, logger="collectors.can"):
        for _ in range(3):
            collector.handle_message(_frame(0x7FF, b"\x00\x00"))
        collector.handle_message(_frame(0x100, b"\x00\x01", is_extended_id=True))

    assert not samples
    assert collector.decode_stats.unknown_frames == 4
    assert collector.decode_stats.decoded_frames == 0
    assert sum("0x7FF" in record.message for record in caplog.records) == 1


def test_malformed_payloads_are_counted_and_skipped(tmp_path: Path):
    (tmp_path / "only.dbc").write_text(_MINIMAL_DBC, encoding="utf-8")
    samples: list[Sample] = []
    collector = CanCollector(
        _bus_config([DbcConfig(device="ecu", file="only.dbc")]), tmp_path, samples.append
    )

    collector.handle_message(_frame(0x100, b"\x01"))  # truncated: STATUS is 2 bytes
    collector.handle_message(_frame(0x100, b"\x00\x00", is_error_frame=True))
    collector.handle_message(_frame(0x100, b"", is_remote_frame=True))
    collector.handle_message(_frame(0x100, b"\x00\x02"))  # a good frame still lands

    assert collector.decode_stats.malformed_frames == 3
    assert collector.decode_stats.decoded_frames == 1
    assert [sample.value for sample in samples] == [0x0200]


def test_frame_timestamps_are_projected_onto_the_monotonic_timebase(tmp_path: Path):
    (tmp_path / "only.dbc").write_text(_MINIMAL_DBC, encoding="utf-8")
    samples: list[Sample] = []
    collector = CanCollector(
        _bus_config([DbcConfig(device="ecu", file="only.dbc")]),
        tmp_path,
        samples.append,
        wall_clock=lambda t_mono_ns: t_mono_ns / 1e6,
    )

    before = time.monotonic_ns()
    collector.handle_message(_frame(0x100, b"\x00\x01", timestamp=1_000.000_000))
    collector.handle_message(_frame(0x100, b"\x00\x02", timestamp=1_000.002_500))
    collector.handle_message(_frame(0x100, b"\x00\x03", timestamp=1_000.100_000))
    after = time.monotonic_ns()

    first, second, third = (sample.t_mono_ns for sample in samples)
    assert before <= first <= after
    assert second - first == 2_500_000
    assert third - first == 100_000_000
    assert [sample.t_wall_ms for sample in samples] == [s.t_mono_ns / 1e6 for s in samples]


def test_unstamped_frames_fall_back_to_capture_time(tmp_path: Path):
    (tmp_path / "only.dbc").write_text(_MINIMAL_DBC, encoding="utf-8")
    samples: list[Sample] = []
    collector = CanCollector(
        _bus_config([DbcConfig(device="ecu", file="only.dbc")]), tmp_path, samples.append
    )

    before = time.monotonic_ns()
    collector.handle_message(_frame(0x100, b"\x00\x01", timestamp=0.0))
    after = time.monotonic_ns()

    assert before <= samples[0].t_mono_ns <= after


def test_monotonic_wall_clock_tracks_the_system_clock():
    clock = MonotonicWallClock()

    now_ms = clock(time.monotonic_ns())

    assert abs(now_ms - time.time() * 1000.0) < 100.0
    assert clock(1_000_000_000) - clock(0) == pytest.approx(1000.0)


def test_missing_or_invalid_dbc_fails_loudly(tmp_path: Path):
    config = _bus_config([DbcConfig(device="ecu", file="absent.dbc")])

    with pytest.raises(CanDecoderError, match="cannot load DBC for device 'ecu'"):
        CanCollector(config, tmp_path, lambda sample: None)


def test_run_retries_with_backoff_while_the_interface_is_absent(tmp_path: Path):
    (tmp_path / "only.dbc").write_text(_MINIMAL_DBC, encoding="utf-8")
    stop = threading.Event()
    attempts: list[str] = []

    def factory(config: BusConfig):
        attempts.append(config.interface)
        if len(attempts) == 3:
            stop.set()
        raise can.CanInitializationError("no such device")

    collector = CanCollector(
        _bus_config([DbcConfig(device="ecu", file="only.dbc")], interface="can9"),
        tmp_path,
        lambda sample: None,
        bus_factory=factory,
        backoff_start_s=0.001,
        backoff_max_s=0.001,
    )
    collector.run(stop)

    assert attempts == ["can9", "can9", "can9"]
    assert collector.stats.open_failures == 3
    assert collector.stats.reconnects == 0


def test_run_reconnects_after_a_bus_error(tmp_path: Path):
    (tmp_path / "only.dbc").write_text(_MINIMAL_DBC, encoding="utf-8")
    stop = threading.Event()
    samples: list[Sample] = []
    buses: list[_FakeBus] = []

    def factory(config: BusConfig) -> _FakeBus:
        if not buses:
            bus = _FakeBus([_frame(0x100, b"\x00\x07"), can.CanOperationError("bus-off")])
        else:
            bus = _FakeBus([])
            stop.set()
        buses.append(bus)
        return bus

    collector = CanCollector(
        _bus_config([DbcConfig(device="ecu", file="only.dbc")]),
        tmp_path,
        samples.append,
        bus_factory=factory,
        backoff_start_s=0.001,
        backoff_max_s=0.001,
    )
    collector.run(stop)

    assert len(buses) == 2
    assert [bus.shutdowns for bus in buses] == [1, 1]
    assert collector.stats.reconnects == 1
    assert collector.stats.bus_errors >= 1
    assert [sample.value for sample in samples] == [0x0700]


def test_start_and_stop_run_the_loop_on_a_thread(tmp_path: Path):
    (tmp_path / "only.dbc").write_text(_MINIMAL_DBC, encoding="utf-8")
    seen = threading.Event()
    samples: list[Sample] = []

    class _IdleBus(_FakeBus):
        def recv(self, timeout: float | None = None) -> can.Message | None:
            seen.set()
            time.sleep(0.001)
            return None

    collector = CanCollector(
        _bus_config([DbcConfig(device="ecu", file="only.dbc")]),
        tmp_path,
        samples.append,
        bus_factory=lambda config: _IdleBus([]),
    )
    collector.start()
    try:
        assert seen.wait(timeout=5.0)
        assert collector.is_running()
    finally:
        collector.stop()

    assert not collector.is_running()
    assert not samples


def test_thread_survives_an_unexpected_failure(tmp_path: Path, caplog):
    (tmp_path / "only.dbc").write_text(_MINIMAL_DBC, encoding="utf-8")

    def factory(config: BusConfig):
        raise RuntimeError("python-can went sideways")

    collector = CanCollector(
        _bus_config([DbcConfig(device="ecu", file="only.dbc")]),
        tmp_path,
        lambda sample: None,
        bus_factory=factory,
    )
    with caplog.at_level(logging.ERROR, logger="collectors.can"):
        collector.start()
        collector.stop()

    assert not collector.is_running()
    assert any("collector thread failed" in record.message for record in caplog.records)


def _vcan_available() -> bool:
    try:
        bus = can.Bus(channel="vcan0", interface="socketcan")
    except Exception:
        return False
    bus.shutdown()
    return True


@pytest.mark.skipif(not _vcan_available(), reason="no vcan0 interface on this host")
def test_vcan_roundtrip_emits_samples(tmp_path: Path):
    """The only test here that touches a real socketCAN interface.

    CI has no vcan and will keep skipping this, which is fine: its value is
    on the Phase 4 bench host, where `vcan0` is exactly the interface
    `profiles/example-club-racer-bench` opens and where a permissions or
    kernel-module problem would otherwise show up as a bandwidth run that
    quietly measured no CAN at all. Create the interface with
    `sudo modprobe vcan && sudo ip link add dev vcan0 type vcan &&
    sudo ip link set up vcan0`, then run this file.
    """
    (tmp_path / "only.dbc").write_text(_MINIMAL_DBC, encoding="utf-8")
    samples: list[Sample] = []
    collector = CanCollector(
        _bus_config([DbcConfig(device="ecu", file="only.dbc")], interface="vcan0"),
        tmp_path,
        samples.append,
    )
    collector.start()
    try:
        writer = can.Bus(channel="vcan0", interface="socketcan")
        try:
            deadline = time.monotonic() + 5.0
            while not samples and time.monotonic() < deadline:
                writer.send(_frame(0x100, b"\x00\x2a"))
                time.sleep(0.05)
        finally:
            writer.shutdown()
    finally:
        collector.stop()

    assert samples
    assert samples[0].source_ref == "can0:ecu.STATUS.LEVEL"
    assert samples[0].value == 0x2A00
