"""Host collector metric-name stability, probe isolation, and poll-loop tests."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from collectors.host import HostCollector, HostMetricsReader, parse_chronyc_tracking
from core.catalog import build_runtime_catalog
from core.config import HostConfig
from core.samples import Sample

# The metric names a profile's `from:` refs bind to. Renaming any of these
# silently unmaps a channel in every deployed catalog, so the set is pinned.
EXPECTED_REFS = {
    "host:cpu.percent",
    "host:cpu.percent.0",
    "host:cpu.percent.1",
    "host:cpu.freq_mhz",
    "host:load.avg_1m",
    "host:load.avg_5m",
    "host:load.avg_15m",
    "host:temp.cpu_thermal.0",
    "host:temp.coretemp.core_0",
    "host:mem.percent",
    "host:mem.used_bytes",
    "host:mem.available_bytes",
    "host:mem.total_bytes",
    "host:swap.percent",
    "host:swap.used_bytes",
    "host:disk.percent",
    "host:disk.used_bytes",
    "host:disk.free_bytes",
    "host:disk.read_bytes",
    "host:disk.write_bytes",
    "host:net.bytes_sent",
    "host:net.bytes_recv",
    "host:net.packets_sent",
    "host:net.packets_recv",
    "host:net.err_in",
    "host:net.err_out",
    "host:net.drop_in",
    "host:net.drop_out",
    "host:clock_offset_s",
    "host:clock_source",
    "host:clock_stratum",
    "host:clock_root_dispersion_s",
}

CHRONY_TRACKING = """Reference ID    : 47505300 (GPS)
Stratum         : 1
System time     : 0.000123456 seconds fast of NTP time
Root dispersion : 0.000456789 seconds
Leap status     : Normal
"""


@dataclass
class _Temp:
    label: str
    current: float


@dataclass
class _Freq:
    current: float


@dataclass
class _Memory:
    total: int
    available: int
    used: int
    percent: float


@dataclass
class _Swap:
    used: int
    percent: float


@dataclass
class _Disk:
    used: int
    free: int
    percent: float


@dataclass
class _DiskIo:
    read_bytes: int
    write_bytes: int


@dataclass
class _Net:
    bytes_sent: int
    bytes_recv: int
    packets_sent: int
    packets_recv: int
    errin: int
    errout: int
    dropin: int
    dropout: int


@dataclass
class FakePsutil:
    """A scripted stand-in for the psutil module."""

    temperatures: dict[str, list[_Temp]] = field(
        default_factory=lambda: {
            "cpu_thermal": [_Temp(label="", current=48.5)],
            "coretemp": [_Temp(label="Core 0", current=51.0)],
        }
    )
    freq: _Freq | None = field(default_factory=lambda: _Freq(current=1500.0))
    disk_io: _DiskIo | None = field(default_factory=lambda: _DiskIo(read_bytes=10, write_bytes=20))
    net: _Net | None = field(
        default_factory=lambda: _Net(
            bytes_sent=1,
            bytes_recv=2,
            packets_sent=3,
            packets_recv=4,
            errin=5,
            errout=6,
            dropin=7,
            dropout=8,
        )
    )
    raises: dict[str, Exception] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    disk_paths: list[str] = field(default_factory=list)

    def _check(self, name: str) -> None:
        self.calls.append(name)
        error = self.raises.get(name)
        if error is not None:
            raise error

    def cpu_percent(self, interval: float | None = None, percpu: bool = False):
        self._check("cpu_percent_percpu" if percpu else "cpu_percent")
        return [11.0, 22.0] if percpu else 33.0

    def cpu_freq(self) -> _Freq | None:
        self._check("cpu_freq")
        return self.freq

    def getloadavg(self) -> tuple[float, float, float]:
        self._check("getloadavg")
        return (0.5, 0.75, 1.0)

    def sensors_temperatures(self) -> dict[str, list[_Temp]]:
        self._check("sensors_temperatures")
        return self.temperatures

    def virtual_memory(self) -> _Memory:
        self._check("virtual_memory")
        return _Memory(total=8_000, available=6_000, used=2_000, percent=25.0)

    def swap_memory(self) -> _Swap:
        self._check("swap_memory")
        return _Swap(used=100, percent=1.5)

    def disk_usage(self, path: str) -> _Disk:
        self._check("disk_usage")
        self.disk_paths.append(path)
        return _Disk(used=1_000, free=3_000, percent=25.0)

    def disk_io_counters(self) -> _DiskIo | None:
        self._check("disk_io_counters")
        return self.disk_io

    def net_io_counters(self) -> _Net | None:
        self._check("net_io_counters")
        return self.net


def _host_config(interval: str = "5s", *, enabled: bool = True) -> HostConfig:
    return HostConfig(enabled=enabled, interval=interval)


# The sysfs-derived groups (per-policy clocks, cooling devices) read the
# real tree by default; the pinned-name tests are about the psutil-derived
# names, so they point at a tree that does not exist and get none.
NO_SYSFS = Path("/nonexistent-sysfs")


def _reader(fake: FakePsutil | None = None) -> tuple[HostMetricsReader, FakePsutil]:
    fake = FakePsutil() if fake is None else fake
    reader = HostMetricsReader(
        psutil_module=fake, chrony_runner=lambda: CHRONY_TRACKING, sysfs_root=NO_SYSFS
    )
    return reader, fake


def test_chrony_tracking_exposes_clock_health():
    assert parse_chronyc_tracking(CHRONY_TRACKING) == {
        "host:clock_offset_s": 0.000123456,
        "host:clock_source": "GPS",
        "host:clock_stratum": 1,
        "host:clock_root_dispersion_s": 0.000456789,
    }

    slow = CHRONY_TRACKING.replace("fast", "slow")
    assert parse_chronyc_tracking(slow)["host:clock_offset_s"] == -0.000123456


def test_read_emits_the_pinned_metric_names():
    reader, _ = _reader()

    readings = dict(reader.read())

    assert set(readings) == EXPECTED_REFS
    assert readings["host:cpu.percent"] == 33.0
    assert readings["host:cpu.percent.1"] == 22.0
    assert readings["host:temp.cpu_thermal.0"] == 48.5
    assert readings["host:temp.coretemp.core_0"] == 51.0
    assert readings["host:mem.available_bytes"] == 6_000
    assert readings["host:net.drop_out"] == 8
    assert reader.stats.probe_failures == 0


def test_byte_counters_are_ints_and_percentages_are_floats():
    reader, _ = _reader()

    readings = dict(reader.read())

    for ref, value in readings.items():
        if ref == "host:clock_source":
            expected = str
        elif ref == "host:clock_stratum" or ref.endswith("_bytes") or ref.startswith("host:net."):
            expected = int
        else:
            expected = float
        assert isinstance(value, expected), ref


def test_cpu_percent_is_primed_so_the_first_poll_is_not_zero():
    _, fake = _reader()

    assert fake.calls == ["cpu_percent", "cpu_percent_percpu"]


def test_failing_probe_costs_only_its_own_group(caplog: pytest.LogCaptureFixture):
    fake = FakePsutil(raises={"virtual_memory": PermissionError("denied")})
    caplog.set_level(logging.WARNING, logger="collectors.host")
    reader, _ = _reader(fake)

    first = dict(reader.read())
    second = dict(reader.read())

    assert not any(ref.startswith(("host:mem.", "host:swap.")) for ref in first)
    assert "host:cpu.percent" in first and "host:net.bytes_sent" in first
    assert set(first) == set(second)
    assert reader.stats.probe_failures == 2
    # Each failing group is reported once, not on every poll.
    assert len(caplog.records) == 1
    assert "mem metrics unavailable" in caplog.records[0].message


def test_platform_without_sensors_or_loadavg_skips_those_metrics():
    class _Minimal(FakePsutil):
        getloadavg = None
        sensors_temperatures = None

    reader, _ = _reader(_Minimal())

    readings = dict(reader.read())

    assert not any(ref.startswith(("host:load.", "host:temp.")) for ref in readings)
    assert "host:cpu.percent" in readings
    assert reader.stats.probe_failures == 0


def test_absent_optional_counters_are_omitted_rather_than_faked():
    reader, _ = _reader(FakePsutil(freq=None, disk_io=None, net=None, temperatures={}))

    readings = dict(reader.read())

    assert "host:cpu.freq_mhz" not in readings
    assert "host:disk.read_bytes" not in readings
    assert not any(ref.startswith(("host:net.", "host:temp.")) for ref in readings)
    assert readings["host:disk.percent"] == 25.0


def test_sensor_labels_are_normalized_into_source_refs():
    fake = FakePsutil(
        temperatures={
            "ACPI Zone": [_Temp(label="Package id 0", current=40.0), _Temp(label="", current=41.0)]
        }
    )
    reader, _ = _reader(fake)

    readings = dict(reader.read())

    assert readings["host:temp.acpi_zone.package_id_0"] == 40.0
    assert readings["host:temp.acpi_zone.1"] == 41.0


def test_temperature_aliases_are_emitted_beside_the_raw_sensors():
    """`host.temperatures` puts a board-neutral name on a board's sensor."""
    fake = FakePsutil(
        temperatures={
            "soc_thermal": [_Temp(label="", current=48.5)],
            "nvme": [_Temp(label="Composite", current=39.0)],
        }
    )
    reader = HostMetricsReader(
        psutil_module=fake,
        chrony_runner=lambda: CHRONY_TRACKING,
        temperatures={"cpu": "soc_thermal.0", "nvme": "nvme.composite"},
    )

    readings = dict(reader.read())

    assert readings["host:temp.cpu"] == 48.5
    assert readings["host:temp.nvme"] == 39.0
    # The raw refs are still there for a profile that wants a specific sensor.
    assert readings["host:temp.soc_thermal.0"] == 48.5
    assert readings["host:temp.nvme.composite"] == 39.0


def test_an_alias_for_a_sensor_the_host_lacks_warns_once_with_the_real_names(
    caplog: pytest.LogCaptureFixture,
):
    """The warning is the fix: it lists what this board actually exposes."""
    fake = FakePsutil(temperatures={"soc_thermal": [_Temp(label="", current=48.5)]})
    reader = HostMetricsReader(
        psutil_module=fake,
        chrony_runner=lambda: CHRONY_TRACKING,
        temperatures={"cpu": "soc_thermal.0", "nvme": "nvme.composite"},
    )

    with caplog.at_level(logging.WARNING):
        first = dict(reader.read())
        second = dict(reader.read())

    assert first["host:temp.cpu"] == 48.5
    assert "host:temp.nvme" not in first
    assert "host:temp.nvme" not in second
    warnings = [r for r in caplog.records if "does not expose" in r.getMessage()]
    assert len(warnings) == 1
    assert "'nvme.composite'" in warnings[0].getMessage()
    assert "soc_thermal.0" in warnings[0].getMessage()
    # A mismatch between profile and board is not a probe failure.
    assert reader.stats.probe_failures == 0


def test_the_collector_hands_the_profile_s_aliases_to_its_reader(monkeypatch):
    config = HostConfig(enabled=True, interval="5s", temperatures={"cpu": "cpu_thermal.0"})
    fake = FakePsutil()
    monkeypatch.setattr("collectors.host.psutil", fake)
    monkeypatch.setattr("collectors.host._run_chronyc_tracking", lambda: CHRONY_TRACKING)
    emitted: list[Sample] = []

    HostCollector(config, emitted.append).poll()

    assert {s.source_ref: s.value for s in emitted}["host:temp.cpu"] == 48.5


def _fake_sysfs(
    tmp_path: Path,
    *,
    policies: dict[str, tuple[str, int]] | None = None,
    cooling: list[tuple[str, int, int]] | None = None,
) -> Path:
    """A sysfs shaped like the RK3576's: cpufreq policies and cooling devices."""
    root = tmp_path / "sys"
    for name, (related, khz) in (policies or {}).items():
        policy = root / "devices/system/cpu/cpufreq" / name
        policy.mkdir(parents=True)
        (policy / "related_cpus").write_text(related + "\n")
        (policy / "scaling_cur_freq").write_text(f"{khz}\n")
    for index, (kind, current, maximum) in enumerate(cooling or []):
        device = root / "class/thermal" / f"cooling_device{index}"
        device.mkdir(parents=True)
        (device / "type").write_text(kind + "\n")
        (device / "cur_state").write_text(f"{current}\n")
        (device / "max_state").write_text(f"{maximum}\n")
    return root


def _sysfs_reader(root: Path) -> HostMetricsReader:
    return HostMetricsReader(
        psutil_module=FakePsutil(), chrony_runner=lambda: CHRONY_TRACKING, sysfs_root=root
    )


def test_each_cpufreq_policy_is_a_clock_and_the_spread_is_board_neutral(tmp_path: Path):
    """big.LITTLE: two policies, named by first CPU, plus max/min across them."""
    root = _fake_sysfs(
        tmp_path, policies={"policy0": ("0 1 2 3", 1_008_000), "policy4": ("4 5 6 7", 2_208_000)}
    )

    readings = dict(_sysfs_reader(root).read())

    assert readings["host:cpu.freq_mhz.cpu0"] == 1008.0
    assert readings["host:cpu.freq_mhz.cpu4"] == 2208.0
    assert readings["host:cpu.freq_mhz.max"] == 2208.0
    assert readings["host:cpu.freq_mhz.min"] == 1008.0
    # psutil's average is still there, unchanged, for the existing channel.
    assert readings["host:cpu.freq_mhz"] == 1500.0


def test_a_policy_with_no_readable_clock_is_skipped_not_fatal(tmp_path: Path):
    root = _fake_sysfs(tmp_path, policies={"policy0": ("0", 800_000)})
    broken = root / "devices/system/cpu/cpufreq/policy4"
    broken.mkdir()
    (broken / "related_cpus").write_text("4\n")  # no scaling_cur_freq

    reader = _sysfs_reader(root)
    readings = dict(reader.read())

    assert readings["host:cpu.freq_mhz.cpu0"] == 800.0
    assert "host:cpu.freq_mhz.cpu4" not in readings
    assert readings["host:cpu.freq_mhz.max"] == 800.0
    assert reader.stats.probe_failures == 0


def test_throttling_is_normalised_per_device_per_class_and_overall(tmp_path: Path):
    """The RK3576's five cooling devices, with the big cluster and GPU held back."""
    root = _fake_sysfs(
        tmp_path,
        cooling=[
            ("cpufreq-cpu0", 0, 8),
            ("cpufreq-cpu4", 3, 9),
            ("devfreq-27800000.gpu", 3, 6),
            ("devfreq-dmc", 0, 3),
            ("devfreq-27700000.npu", 0, 7),
        ],
    )

    readings = dict(_sysfs_reader(root).read())

    assert readings["host:throttle.cpufreq_cpu4.percent"] == pytest.approx(100 * 3 / 9)
    assert readings["host:throttle.devfreq_27800000_gpu.percent"] == 50.0
    assert readings["host:throttle.cpu.percent"] == pytest.approx(100 * 3 / 9)
    assert readings["host:throttle.gpu.percent"] == 50.0
    assert readings["host:throttle.memory.percent"] == 0.0
    assert readings["host:throttle.npu.percent"] == 0.0
    assert readings["host:throttle.percent"] == 50.0


def test_x86_cooling_devices_land_in_the_cpu_class(tmp_path: Path):
    root = _fake_sysfs(tmp_path, cooling=[("Processor", 1, 4), ("intel_powerclamp", 0, 50)])

    readings = dict(_sysfs_reader(root).read())

    assert readings["host:throttle.cpu.percent"] == 25.0
    assert readings["host:throttle.percent"] == 25.0
    assert "host:throttle.gpu.percent" not in readings


def test_a_cooling_device_with_no_range_is_ignored(tmp_path: Path):
    """max_state 0 would divide by zero; it also means the device cannot throttle."""
    root = _fake_sysfs(tmp_path, cooling=[("cpufreq-cpu0", 0, 0)])

    readings = dict(_sysfs_reader(root).read())

    assert not [ref for ref in readings if ref.startswith("host:throttle.")]


def test_a_host_without_cpufreq_or_thermal_sysfs_emits_neither(tmp_path: Path):
    reader = _sysfs_reader(tmp_path / "empty")

    readings = dict(reader.read())

    assert not [ref for ref in readings if ref.startswith("host:throttle.")]
    assert "host:cpu.freq_mhz.max" not in readings
    assert reader.stats.probe_failures == 0


def test_poll_stamps_every_sample_from_one_snapshot():
    samples: list[Sample] = []
    reader, fake = _reader()
    collector = HostCollector(_host_config(), samples.append, reader=reader)

    before = time.monotonic_ns()
    count = collector.poll()
    after = time.monotonic_ns()

    assert count == len(samples) == len(EXPECTED_REFS)
    assert {sample.source_ref for sample in samples} == EXPECTED_REFS
    assert len({sample.t_mono_ns for sample in samples}) == 1
    assert before <= samples[0].t_mono_ns <= after
    assert len({sample.t_wall_ms for sample in samples}) == 1
    assert collector.stats.polls == 1
    assert collector.stats.samples == len(EXPECTED_REFS)
    assert collector.name == "host"
    assert fake.disk_paths == ["/"]


def test_poll_uses_the_supplied_wall_clock():
    samples: list[Sample] = []
    reader, _ = _reader()
    collector = HostCollector(
        _host_config(), samples.append, wall_clock=lambda t_mono_ns: 1_000.0, reader=reader
    )

    collector.poll()

    assert {sample.t_wall_ms for sample in samples} == {1_000.0}


def test_probe_failures_surface_on_the_collector():
    reader, _ = _reader(FakePsutil(raises={"net_io_counters": OSError("no netlink")}))
    collector = HostCollector(_host_config(), lambda sample: None, reader=reader)

    collector.poll()

    assert collector.stats.probe_failures == 1


def test_run_polls_on_its_interval_until_stopped():
    samples: list[Sample] = []
    reader, _ = _reader()
    stop = threading.Event()

    def _stop_after_three(sample: Sample) -> None:
        samples.append(sample)
        if collector.stats.polls >= 3:
            stop.set()

    collector = HostCollector(_host_config("10ms"), _stop_after_three, reader=reader)
    started = time.monotonic()
    collector.run(stop)
    elapsed = time.monotonic() - started

    assert collector.stats.polls >= 3
    # Three polls at 10 ms must not have run back-to-back...
    assert elapsed >= 0.02
    # ...nor blocked for anything like a full 5 s default.
    assert elapsed < 2.0


def test_start_and_stop_manage_the_collector_thread():
    samples: list[Sample] = []
    reader, _ = _reader()
    collector = HostCollector(_host_config("10ms"), samples.append, reader=reader)

    collector.start()
    try:
        deadline = time.monotonic() + 2.0
        while not samples and time.monotonic() < deadline:
            time.sleep(0.005)
        assert collector.is_running()
    finally:
        collector.stop(timeout=2.0)

    assert samples
    assert not collector.is_running()


def test_disabled_profile_never_polls():
    samples: list[Sample] = []
    reader, fake = _reader()
    collector = HostCollector(_host_config(enabled=False), samples.append, reader=reader)
    fake.calls.clear()

    collector.run(threading.Event())

    assert samples == []
    assert fake.calls == []


def test_thread_survives_a_reader_that_raises(caplog: pytest.LogCaptureFixture):
    class _Broken(HostMetricsReader):
        def read(self):
            raise RuntimeError("psutil exploded")

    caplog.set_level(logging.ERROR, logger="collectors.host")
    collector = HostCollector(
        _host_config("10ms"), lambda sample: None, reader=_Broken(psutil_module=FakePsutil())
    )

    collector.start()
    try:
        deadline = time.monotonic() + 2.0
        while collector.is_running() and time.monotonic() < deadline:
            time.sleep(0.005)
    finally:
        collector.stop(timeout=2.0)

    assert not collector.is_running()
    assert any("collector thread failed" in record.message for record in caplog.records)


def test_emitted_refs_map_through_a_catalog(tmp_path: Path):
    profile = tmp_path / "profile"
    (profile / "dbcs").mkdir(parents=True)
    (profile / "dbcs" / "ecu.dbc").write_text('VERSION ""\n', encoding="utf-8")
    (profile / "vehicle.yaml").write_text(
        """vehicle: {id: host-test}
buses:
  - name: can0
    interface: can0
    bitrate: 1000000
    dbcs: [{device: ecu, file: dbcs/ecu.dbc}]
serial: []
host: {enabled: true, interval: 5s}
""",
        encoding="utf-8",
    )
    (profile / "catalog.yaml").write_text(
        """channels:
  sys.host.cpu_percent: {from: "host:cpu.percent", units: "%"}
  sys.host.mem_percent: {from: "host:mem.percent", units: "%"}
  sys.host.cpu_temp: {from: "host:temp.cpu_thermal.0", units: "degC"}
apps: {}
""",
        encoding="utf-8",
    )
    runtime = build_runtime_catalog(profile, state_path=tmp_path / "state.json")
    reader, _ = _reader()

    mapped = {
        runtime.source_map[ref][1].name: value
        for ref, value in reader.read()
        if ref in runtime.source_map
    }

    assert mapped == {
        "sys.host.cpu_percent": 33.0,
        "sys.host.mem_percent": 25.0,
        "sys.host.cpu_temp": 48.5,
    }


def test_real_psutil_snapshot_is_sane():
    reader = HostMetricsReader()

    readings = dict(reader.read())

    assert 0.0 <= readings["host:cpu.percent"] <= 100.0
    assert 0.0 <= readings["host:mem.percent"] <= 100.0
    assert readings["host:mem.total_bytes"] > 0
    assert readings["host:disk.used_bytes"] >= 0
    # chronyc is optional on developer/CI hosts. Its independent probe may be
    # the one unavailable group; core host metrics must remain present.
    assert reader.stats.probe_failures <= 1
