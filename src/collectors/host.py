"""Host metrics collector: a psutil poll loop emitting ``host:*`` samples.

Replaces the predecessor's psmqtt sidecar (``docs/ARCHITECTURE.md`` -> What
replaced what): host health is ordinary telemetry, captured by the agent and
mapped through the catalog like any other source. Source refs are
``host:<metric>`` (``docs/CATALOG.md``); a profile that maps none of them
simply drops them at the mapper.

Metric names are a stable interface -- a profile's ``from:`` refs and the
Timescale history behind them both key off these strings, so rename only with
a catalog migration.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass

import psutil

from collectors.clock import Emit, MonotonicWallClock, WallClock
from core.config import HostConfig
from core.samples import Sample, SampleValue

logger = logging.getLogger(__name__)

DEFAULT_DISK_PATH = "/"

Reading = tuple[str, SampleValue]
Probe = Callable[[], Iterator[Reading]]
ChronyRunner = Callable[[], str]


def _run_chronyc_tracking() -> str:
    completed = subprocess.run(
        ["chronyc", "-n", "tracking"],
        check=True,
        capture_output=True,
        text=True,
        timeout=2.0,
    )
    return completed.stdout


def parse_chronyc_tracking(output: str) -> dict[str, SampleValue]:
    """Parse the stable labelled fields needed from ``chronyc tracking``."""
    fields: dict[str, str] = {}
    for line in output.splitlines():
        label, separator, value = line.partition(":")
        if separator:
            fields[label.strip().lower()] = value.strip()
    required = ("reference id", "stratum", "system time", "root dispersion")
    if any(field not in fields for field in required):
        raise ValueError("chronyc tracking output is missing required fields")

    reference = fields["reference id"]
    parenthesized = re.search(r"\(([^()]+)\)\s*$", reference)
    source = parenthesized.group(1) if parenthesized else reference.split()[0]
    system_time = fields["system time"].split()
    if len(system_time) < 3 or system_time[2] not in {"fast", "slow"}:
        raise ValueError("chronyc tracking has an invalid System time field")
    offset = float(system_time[0])
    if system_time[2] == "slow":
        offset = -offset
    return {
        "host:clock_offset_s": offset,
        "host:clock_source": source,
        "host:clock_stratum": int(fields["stratum"]),
        "host:clock_root_dispersion_s": float(fields["root dispersion"].split()[0]),
    }


@dataclass(slots=True)
class HostStats:
    """Counters for one host collector, surfaced by the agent as ``sys.agent.*``."""

    polls: int = 0
    samples: int = 0
    probe_failures: int = 0


def _sanitize(name: str) -> str:
    """Normalize a sensor/label string into a source-ref-safe fragment."""
    cleaned = "".join(char if char.isalnum() else "_" for char in name.strip().lower())
    return cleaned.strip("_")


class HostMetricsReader:
    """Reads one snapshot of host metrics from psutil.

    Every metric group is probed independently: a kernel that exposes no
    thermal zones, an unreadable mount point, or a psutil call that raises on
    one platform must cost only that group's readings, never the whole poll.
    """

    def __init__(
        self,
        *,
        psutil_module: object | None = None,
        disk_path: str = DEFAULT_DISK_PATH,
        chrony_runner: ChronyRunner | None = None,
        temperatures: Mapping[str, str] | None = None,
    ) -> None:
        """Build a reader over ``psutil_module`` (injectable for tests).

        ``temperatures`` is the profile's ``host.temperatures`` mapping:
        ``<alias>: <chip>.<label>``, each emitted as ``host:temp.<alias>``.
        """
        self._psutil = psutil if psutil_module is None else psutil_module
        self._disk_path = disk_path
        self._temperatures = dict(temperatures or {})
        self._chrony_runner = _run_chronyc_tracking if chrony_runner is None else chrony_runner
        self.stats = HostStats()
        self._reported_failures: set[str] = set()
        self.prime()

    def prime(self) -> None:
        """Establish the CPU-percent baseline; the first reading is otherwise 0."""
        cpu_percent = getattr(self._psutil, "cpu_percent", None)
        if cpu_percent is None:  # pragma: no cover - psutil always provides it
            return
        try:
            cpu_percent(interval=None)
            cpu_percent(interval=None, percpu=True)
        except Exception as exc:
            self._note_failure("prime", exc)

    def read(self) -> list[Reading]:
        """Return every readable metric as ``(source_ref, value)`` pairs."""
        readings: list[Reading] = []
        for name, probe in (
            ("cpu", self._read_cpu),
            ("load", self._read_load),
            ("temp", self._read_temperatures),
            ("mem", self._read_memory),
            ("disk", self._read_disk),
            ("net", self._read_network),
            ("clock", self._read_clock),
        ):
            try:
                # Broad: psutil raises platform-specific errors (missing
                # sysfs nodes, permissions, transient /proc reads) that are
                # all "this metric is unavailable right now", not fatal.
                readings.extend(probe())
            except Exception as exc:
                self.stats.probe_failures += 1
                self._note_failure(name, exc)
        return readings

    def _read_cpu(self) -> Iterator[Reading]:
        yield "host:cpu.percent", float(self._psutil.cpu_percent(interval=None))
        for index, percent in enumerate(self._psutil.cpu_percent(interval=None, percpu=True)):
            yield f"host:cpu.percent.{index}", float(percent)
        freq = self._psutil.cpu_freq()
        if freq is not None:
            yield "host:cpu.freq_mhz", float(freq.current)

    def _read_load(self) -> Iterator[Reading]:
        getloadavg = getattr(self._psutil, "getloadavg", None)
        if getloadavg is None:  # pragma: no cover - POSIX always provides it
            return
        one, five, fifteen = getloadavg()
        yield "host:load.avg_1m", float(one)
        yield "host:load.avg_5m", float(five)
        yield "host:load.avg_15m", float(fifteen)

    def _read_temperatures(self) -> Iterator[Reading]:
        sensors = getattr(self._psutil, "sensors_temperatures", None)
        if sensors is None:  # pragma: no cover - not available on every platform
            return
        found: dict[str, float] = {}
        for chip, entries in (sensors() or {}).items():
            chip_name = _sanitize(chip) or "unknown"
            for index, entry in enumerate(entries):
                label = _sanitize(entry.label or "") or str(index)
                found[f"{chip_name}.{label}"] = float(entry.current)
        for sensor, value in found.items():
            yield f"host:temp.{sensor}", value
        # The board-neutral names a catalog maps. A raw sensor ref always
        # carries a dot and an alias never does, so the two cannot collide.
        for alias, sensor in self._temperatures.items():
            value = found.get(sensor)
            if value is None:
                self._note_missing_sensor(alias, sensor, found)
                continue
            yield f"host:temp.{alias}", value

    def _read_memory(self) -> Iterator[Reading]:
        memory = self._psutil.virtual_memory()
        yield "host:mem.percent", float(memory.percent)
        yield "host:mem.used_bytes", int(memory.used)
        yield "host:mem.available_bytes", int(memory.available)
        yield "host:mem.total_bytes", int(memory.total)
        swap = self._psutil.swap_memory()
        yield "host:swap.percent", float(swap.percent)
        yield "host:swap.used_bytes", int(swap.used)

    def _read_disk(self) -> Iterator[Reading]:
        usage = self._psutil.disk_usage(self._disk_path)
        yield "host:disk.percent", float(usage.percent)
        yield "host:disk.used_bytes", int(usage.used)
        yield "host:disk.free_bytes", int(usage.free)
        io = self._psutil.disk_io_counters()
        if io is not None:
            yield "host:disk.read_bytes", int(io.read_bytes)
            yield "host:disk.write_bytes", int(io.write_bytes)

    def _read_network(self) -> Iterator[Reading]:
        net = self._psutil.net_io_counters()
        if net is None:
            return
        yield "host:net.bytes_sent", int(net.bytes_sent)
        yield "host:net.bytes_recv", int(net.bytes_recv)
        yield "host:net.packets_sent", int(net.packets_sent)
        yield "host:net.packets_recv", int(net.packets_recv)
        yield "host:net.err_in", int(net.errin)
        yield "host:net.err_out", int(net.errout)
        yield "host:net.drop_in", int(net.dropin)
        yield "host:net.drop_out", int(net.dropout)

    def _read_clock(self) -> Iterator[Reading]:
        yield from parse_chronyc_tracking(self._chrony_runner()).items()

    def _note_missing_sensor(self, alias: str, sensor: str, found: Mapping[str, float]) -> None:
        """Say once which sensor an alias wanted and which ones exist instead.

        Not a probe failure: the probe worked, and the mismatch is between
        the profile's ``host.temperatures`` and this board. The message
        carries the board's actual sensor names so the fix is a copy-paste
        into the target's ``hardware.yaml``.
        """
        key = f"temp.{alias}"
        if key in self._reported_failures:
            return
        self._reported_failures.add(key)
        logger.warning(
            "host: temperature %r maps to sensor %r, which this host does not expose"
            " (it exposes: %s)",
            alias,
            sensor,
            ", ".join(sorted(found)) or "none",
        )

    def _note_failure(self, group: str, exc: Exception) -> None:
        if group in self._reported_failures:
            return
        self._reported_failures.add(group)
        logger.warning("host: %s metrics unavailable: %s", group, exc)


class HostCollector:
    """Polls host metrics at the profile's interval and emits one sample each.

    Like the other collectors it owns no queue and applies no filtering
    (``docs/AGENT_DESIGN.md`` -> Process model): it stamps each poll and hands
    the readings to ``emit``.
    """

    def __init__(
        self,
        config: HostConfig,
        emit: Emit,
        *,
        wall_clock: WallClock | None = None,
        reader: HostMetricsReader | None = None,
        disk_path: str = DEFAULT_DISK_PATH,
    ) -> None:
        """Build a collector for ``config``, reading through ``reader``."""
        self.config = config
        self.stats = HostStats()
        if reader is None:
            reader = HostMetricsReader(disk_path=disk_path, temperatures=config.temperatures)
        self.reader = reader
        self._emit = emit
        self._wall_clock = wall_clock if wall_clock is not None else MonotonicWallClock()
        self._interval_s = config.interval_ns / 1e9
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def name(self) -> str:
        """The source-ref prefix this collector emits under."""
        return "host"

    def poll(self) -> int:
        """Take one snapshot and emit it; returns the number of samples emitted."""
        t_mono_ns = time.monotonic_ns()
        t_wall_ms = self._wall_clock(t_mono_ns)
        readings = self.reader.read()
        for source_ref, value in readings:
            self._emit(Sample(source_ref, t_mono_ns, t_wall_ms, value))
        self.stats.polls += 1
        self.stats.samples += len(readings)
        self.stats.probe_failures = self.reader.stats.probe_failures
        return len(readings)

    def run(self, stop: threading.Event | None = None) -> None:
        """Poll on a fixed cadence until ``stop`` is set."""
        stop = self._stop if stop is None else stop
        if not self.config.enabled:
            logger.info("host: metrics disabled by profile, collector not running")
            return
        logger.info("host: starting, polling every %.3gs", self._interval_s)
        next_poll = time.monotonic()
        while not stop.is_set():
            self.poll()
            next_poll += self._interval_s
            delay = next_poll - time.monotonic()
            if delay <= 0:
                # A poll overran its slot (or the host was suspended): resync
                # rather than burn a burst of catch-up polls.
                next_poll = time.monotonic()
                delay = 0.0
            if stop.wait(delay):
                break
        logger.info("host: stopped")

    def start(self) -> None:
        """Run the poll loop on a daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("host: collector already running")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, name="host", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the poll loop to finish and join its thread."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)

    def is_running(self) -> bool:
        """Whether the collector thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def _thread_main(self) -> None:
        try:
            self.run(self._stop)
        except Exception:
            # The agent's health loop supervises restarts; host metrics going
            # away must never take capture down with them.
            logger.exception("host: collector thread failed")
