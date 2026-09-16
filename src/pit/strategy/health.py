"""Pit-local health surface for the strategy service.

The number that matters is ``last_evaluation_age_s``: the fuel dashboard's
stat panels going flat means either there is no session or this service
stopped, and those are opposite conclusions. ``session_id`` alongside it
says which.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)


class HealthState:
    """Counters the evaluation loop updates and the health endpoint reports."""

    def __init__(self) -> None:
        self.evaluations = 0
        self.db_errors = 0
        self.mqtt_connected = False
        self.mqtt_reconnects = 0
        self.mqtt_published = 0
        self.session_id: str | None = None
        self.last_trigger: str | None = None
        self.findings_open = 0
        self.last_error: str | None = None
        self._last_evaluation_mono: float | None = None
        self._started = time.monotonic()

    def observe_evaluation(self, session_id: str | None, trigger: str, findings_open: int) -> None:
        """Record one committed evaluation."""
        self.evaluations += 1
        self.session_id = session_id
        self.last_trigger = trigger
        self.findings_open = findings_open
        self._last_evaluation_mono = time.monotonic()

    def observe_db_error(self, exc: Exception) -> None:
        """Record one read or write that failed."""
        self.db_errors += 1
        self.last_error = f"database: {exc}"

    @property
    def last_evaluation_age_s(self) -> float | None:
        """Seconds since the last committed evaluation; None before the first."""
        if self._last_evaluation_mono is None:
            return None
        return round(time.monotonic() - self._last_evaluation_mono, 1)

    def snapshot(self) -> dict[str, object]:
        """Everything `/health` reports."""
        return {
            "uptime_s": round(time.monotonic() - self._started, 1),
            "evaluations": self.evaluations,
            "db_errors": self.db_errors,
            "mqtt_connected": self.mqtt_connected,
            "mqtt_reconnects": self.mqtt_reconnects,
            "mqtt_published": self.mqtt_published,
            "session_id": self.session_id,
            "last_trigger": self.last_trigger,
            "findings_open": self.findings_open,
            "last_evaluation_age_s": self.last_evaluation_age_s,
            "last_error": self.last_error,
        }

    def log_line(self) -> str:
        """The periodic summary."""
        return (
            f"evaluations={self.evaluations} session={self.session_id} "
            f"findings_open={self.findings_open} db_errors={self.db_errors} "
            f"mqtt={'up' if self.mqtt_connected else 'down'} "
            f"last_evaluation_age={self.last_evaluation_age_s}s"
        )


class _HealthHandler(BaseHTTPRequestHandler):
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
