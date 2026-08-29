"""Pit-side health collector: the pit's own metrics into `pit_metrics`.

The `system-status` dashboard could see the vehicle's clock, agent and host
in detail and the pit's not at all. Everything it knew about the car arrived
through the catalog -> NATS -> ingest-writer path; the pit's own chrony, host
resources, ntrip-client and NATS server had no collector of any kind, and
three of the four existed only in an HTTP endpoint that showed "now" with no
history behind it.

This service closes that: poll the four probes on a fixed cadence, write one
transaction per poll into `pit_metrics`, and let Grafana read it back through
`v_pit_metrics` on the Timescale datasource it already has. No new datasource
plugin, and the same trend/history the vehicle rows get.

Synchronous by design, unlike the other pit services. They are asyncio
because they hold concurrent network loops -- a JetStream consumer alongside
a flush timer, a caster socket alongside a position feed. This one polls,
writes, and sleeps. An event loop would arrange nothing.

It never publishes to NATS or MQTT and never reads the vehicle: pit-local
data, pit-local storage.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from pit.db.dsn import dsn_from_env
from pit.pit_monitor.health import HealthState, serve_health
from pit.pit_monitor.probes import HostProbe, NatsProbe, NtripProbe, Probe, Reading
from pit.pit_monitor.store import PitMetricStore, to_rows

logger = logging.getLogger(__name__)

_REPORT_INTERVAL_S = 60.0


@dataclass(frozen=True, slots=True)
class PitMonitorSettings:
    """Deploy-time wiring for the pit-monitor."""

    dsn: str
    interval_s: float = 5.0
    health_port: int = 8085
    disk_path: str = "/"
    http_timeout_s: float = 3.0
    ntrip_health_url: str | None = "http://127.0.0.1:8083/health"
    nats_monitor_url: str | None = "http://127.0.0.1:8222"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> PitMonitorSettings:
        """Build settings from the environment; see `example.env`."""
        env = os.environ if env is None else env
        interval_s = float(env.get("OPENLAPS_PIT_MONITOR_INTERVAL_S", "5") or "5")
        if interval_s <= 0:
            raise ValueError("OPENLAPS_PIT_MONITOR_INTERVAL_S must be positive")
        return cls(
            dsn=dsn_from_env(env),
            interval_s=interval_s,
            health_port=int(env.get("OPENLAPS_PIT_MONITOR_HEALTH_PORT", "8085") or "8085"),
            disk_path=env.get("OPENLAPS_PIT_MONITOR_DISK_PATH", "/").strip() or "/",
            http_timeout_s=float(env.get("OPENLAPS_PIT_MONITOR_HTTP_TIMEOUT_S", "3") or "3"),
            # Empty disables the probe outright rather than logging a failure
            # every poll: a pit not running RTK stops the ntrip-client on
            # purpose, and that is a configuration, not a fault to report.
            ntrip_health_url=env.get(
                "OPENLAPS_PIT_MONITOR_NTRIP_URL", "http://127.0.0.1:8083/health"
            ).strip()
            or None,
            nats_monitor_url=env.get(
                "OPENLAPS_PIT_MONITOR_NATS_URL", "http://127.0.0.1:8222"
            ).strip()
            or None,
        )


class PitMonitorService:
    """Polls the pit's probes and appends each poll to `pit_metrics`."""

    def __init__(
        self,
        settings: PitMonitorSettings,
        *,
        store: PitMetricStore | None = None,
        probes: Mapping[str, Probe] | None = None,
    ) -> None:
        """Build the service; ``store`` and ``probes`` are injectable for tests."""
        self.settings = settings
        self.health = HealthState()
        self._store = PitMetricStore(settings.dsn) if store is None else store
        self._probes = dict(self._build_probes()) if probes is None else dict(probes)
        self._last_report = 0.0

    def _build_probes(self) -> Iterator[tuple[str, Probe]]:
        yield "host", HostProbe(disk_path=self.settings.disk_path)
        if self.settings.ntrip_health_url:
            yield (
                "ntrip",
                NtripProbe(self.settings.ntrip_health_url, timeout_s=self.settings.http_timeout_s),
            )
        else:
            logger.info("pit-monitor: ntrip probe disabled by configuration")
        if self.settings.nats_monitor_url:
            yield (
                "nats",
                NatsProbe(self.settings.nats_monitor_url, timeout_s=self.settings.http_timeout_s),
            )
        else:
            logger.info("pit-monitor: nats probe disabled by configuration")

    def collect(self) -> list[Reading]:
        """Read every probe, keeping what the working ones returned.

        A probe raising costs only its own readings. Partial output from a
        generator that raises part-way is kept for the same reason: half of
        NATS's numbers beats none of them while jsz is briefly unavailable.
        """
        readings: list[Reading] = []
        for name, probe in self._probes.items():
            try:
                # Broad: a probe failure is an HTTP error, a dead chronyd, a
                # malformed response or a psutil quirk -- all "unavailable
                # right now", none of them fatal to the other three.
                readings.extend(probe.read())
            except Exception as exc:
                self.health.observe_probe_failure(name, exc)
                logger.warning("pit-monitor: %s probe failed: %s", name, exc)
        return readings

    def poll(self) -> int:
        """Take one snapshot and commit it; returns the rows written."""
        stamp = datetime.now(UTC)
        rows = to_rows(self.collect(), stamp)
        try:
            written = self._store.write(rows)
        except Exception as exc:
            # Broad: psycopg raises a family of errors for "the database went
            # away", and the answer to every one of them is the same -- count
            # it, keep polling, redial on the next poll.
            self.health.observe_db_error(exc)
            logger.warning("pit-monitor: write failed: %s", exc)
            return 0
        self.health.observe_write(written)
        return written

    def run(self, stop: threading.Event) -> None:
        """Poll on a fixed cadence until ``stop`` is set."""
        logger.info(
            "pit-monitor: starting, polling %s every %.3gs",
            ", ".join(sorted(self._probes)) or "nothing",
            self.settings.interval_s,
        )
        next_poll = time.monotonic()
        try:
            while not stop.is_set():
                self.poll()
                self._report()
                next_poll += self.settings.interval_s
                delay = next_poll - time.monotonic()
                if delay <= 0:
                    # A poll overran its slot (a probe sat on its HTTP
                    # timeout): resync rather than burn a burst of catch-up
                    # polls, the same rule the host collector applies.
                    next_poll = time.monotonic()
                    delay = 0.0
                if stop.wait(delay):
                    break
        finally:
            self._store.close()
            logger.info("pit-monitor: stopped")

    def _report(self) -> None:
        now = time.monotonic()
        if now - self._last_report < _REPORT_INTERVAL_S:
            return
        self._last_report = now
        logger.info("pit-monitor: %s", self.health.log_line())


def run_service(settings: PitMonitorSettings, stop: threading.Event) -> None:
    """Run one pit-monitor with its health endpoint until ``stop`` is set."""
    service = PitMonitorService(settings)
    server = serve_health(service.health, settings.health_port)
    try:
        service.run(stop)
    finally:
        server.shutdown()
