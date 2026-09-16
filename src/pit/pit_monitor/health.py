"""Pit-local health surface for the pit-monitor.

The service that reports on everything else needs someone to report on it,
and `last_write_age_s` is that number: pit panels going flat means either
the pit is quiet or this collector stopped, and those are opposite
conclusions. Per-probe failure counts separate "chronyd is down" from
"the collector is down", which is the other question a flat panel raises.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)


class HealthState:
    """Counters the poll loop updates and the health endpoint reports."""

    def __init__(self) -> None:
        self.polls = 0
        self.rows_written = 0
        self.db_errors = 0
        self.probe_failures: dict[str, int] = {}
        self.last_error: str | None = None

        self._last_write_mono: float | None = None
        self._started = time.monotonic()

    def observe_write(self, rows: int) -> None:
        """Record one committed poll."""
        self.polls += 1
        self.rows_written += rows
        self._last_write_mono = time.monotonic()

    def observe_probe_failure(self, probe: str, exc: Exception) -> None:
        """Record one probe that could not be read this poll."""
        self.probe_failures[probe] = self.probe_failures.get(probe, 0) + 1
        self.last_error = f"{probe}: {exc}"

    def observe_db_error(self, exc: Exception) -> None:
        """Record one poll that could not be committed."""
        self.db_errors += 1
        self.last_error = f"database: {exc}"

    @property
    def last_write_age_s(self) -> float | None:
        """Seconds since the last committed poll; None before the first."""
        if self._last_write_mono is None:
            return None
        return round(time.monotonic() - self._last_write_mono, 1)

    def snapshot(self) -> dict[str, object]:
        """Everything `/health` reports."""
        return {
            "uptime_s": round(time.monotonic() - self._started, 1),
            "polls": self.polls,
            "rows_written": self.rows_written,
            "db_errors": self.db_errors,
            "probe_failures": dict(sorted(self.probe_failures.items())),
            "last_write_age_s": self.last_write_age_s,
            "last_error": self.last_error,
        }

    def log_line(self) -> str:
        """The periodic summary: the numbers worth watching scroll past."""
        failures = ",".join(
            f"{name}={count}" for name, count in sorted(self.probe_failures.items())
        )
        return (
            f"polls={self.polls} rows={self.rows_written} db_errors={self.db_errors} "
            f"last_write_age={self.last_write_age_s}s probe_failures={failures or 'none'}"
        )


class _HealthHandler(BaseHTTPRequestHandler):
    """Serves the service's snapshot; nothing else, no state of its own."""

    state: HealthState

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        if self.path.split("?")[0] != "/health":
            self.send_error(404)
            return
        payload = json.dumps(self.state.snapshot(), sort_keys=True).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        """Silence per-request stderr logging; the service owns the log."""


def serve_health(state: HealthState, port: int, host: str = "") -> ThreadingHTTPServer:
    """Serve `/health` on a daemon thread; returns the server for shutdown."""
    handler = type("HealthHandler", (_HealthHandler,), {"state": state})
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="health", daemon=True)
    thread.start()
    logger.info("health: serving /health on port %d", server.server_port)
    return server
