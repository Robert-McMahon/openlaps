"""Pit-local health surface for the ntrip-client.

A silently-dead correction stream is the failure this service must make
visible: the pit link to the vehicle can be perfectly healthy while the
pit's own backhaul to the caster is down, and nothing else in the system
would notice (ADR 0006). `last_byte_age_s` is the number an operator
actually needs — everything else is context around it. Never reports the
NTRIP password.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)


class HealthState:
    """Counters the service updates and the health endpoint reports."""

    def __init__(self) -> None:
        self.connected = False
        self.bytes_from_caster = 0
        self.publishes = 0
        self.reconnects = 0
        self.auth_errors = 0
        self.nats_reconnects = 0
        self.gga_sends = 0

        self.bytes_per_s = 0.0
        self.publishes_per_s = 0.0

        self._last_byte_mono: float | None = None
        self._window_start = time.monotonic()
        self._window_bytes = 0
        self._window_publishes = 0
        self._started = time.monotonic()

    def observe_chunk(self, size: int) -> None:
        """Record one chunk read from the caster and published on."""
        self.bytes_from_caster += size
        self.publishes += 1
        self._window_bytes += size
        self._window_publishes += 1
        self._last_byte_mono = time.monotonic()

    @property
    def last_byte_age_s(self) -> float | None:
        """Seconds since the last correction byte; None before the first."""
        if self._last_byte_mono is None:
            return None
        return round(time.monotonic() - self._last_byte_mono, 1)

    def roll(self) -> None:
        """Close the rate window; called once a second by the service."""
        elapsed = time.monotonic() - self._window_start
        if elapsed <= 0:
            return
        self.bytes_per_s = self._window_bytes / elapsed
        self.publishes_per_s = self._window_publishes / elapsed
        self._window_start = time.monotonic()
        self._window_bytes = 0
        self._window_publishes = 0

    def snapshot(self) -> dict[str, object]:
        """Everything `/health` reports, and what the 1 Hz log line summarises."""
        return {
            "uptime_s": round(time.monotonic() - self._started, 1),
            "connected": self.connected,
            "bytes_from_caster": self.bytes_from_caster,
            "bytes_per_s": round(self.bytes_per_s, 1),
            "publishes": self.publishes,
            "publishes_per_s": round(self.publishes_per_s, 1),
            "reconnects": self.reconnects,
            "auth_errors": self.auth_errors,
            "nats_reconnects": self.nats_reconnects,
            "last_byte_age_s": self.last_byte_age_s,
            "gga_sends": self.gga_sends,
        }

    def log_line(self) -> str:
        """The 1 Hz summary: the numbers worth watching scroll past."""
        return (
            f"connected={self.connected} bytes/s={self.bytes_per_s:.0f} "
            f"publishes/s={self.publishes_per_s:.1f} last_byte_age={self.last_byte_age_s}s "
            f"reconnects={self.reconnects} gga_sends={self.gga_sends}"
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
