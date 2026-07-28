"""Pit-local health counters and JSON endpoint for the live-decoder."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)


class HealthState:
    """Mutable service counters exposed through `/health`."""

    def __init__(self) -> None:
        self.published: dict[str, int] = defaultdict(int)
        self.suppressed: dict[str, int] = defaultdict(int)
        self.aggregate_sheds = 0
        self.mqtt_drops = 0
        self.mqtt_reconnects = 0
        self.nats_reconnects = 0
        self.registries = 0
        self.bad_version_batches = 0
        self.unknown_seq_batches = 0
        self.malformed_payloads = 0
        self.invalid_values = 0
        self.config_reloads = 0
        self.config_mtime_ns = 0
        self.unmatched_rules: list[str] = []
        self.publish_rate = 0.0
        self._started = time.monotonic()
        self._window_publishes = 0
        self._window_start = self._started

    def note_publish(self, channel: str) -> None:
        self.published[channel] += 1
        self._window_publishes += 1

    def note_suppressed(self, channel: str, count: int = 1) -> None:
        self.suppressed[channel] += count

    def roll(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        elapsed = current - self._window_start
        if elapsed <= 0:
            return
        self.publish_rate = self._window_publishes / elapsed
        self._window_publishes = 0
        self._window_start = current

    def snapshot(self, now: float | None = None) -> dict[str, object]:
        current = time.monotonic() if now is None else now
        channels = sorted(set(self.published) | set(self.suppressed))
        return {
            "uptime_s": round(max(0.0, current - self._started), 1),
            "publish_rate": round(self.publish_rate, 1),
            "channels": {
                channel: {
                    "published": self.published[channel],
                    "suppressed": self.suppressed[channel],
                }
                for channel in channels
            },
            "aggregate_sheds": self.aggregate_sheds,
            "mqtt_drops": self.mqtt_drops,
            "mqtt_reconnects": self.mqtt_reconnects,
            "nats_reconnects": self.nats_reconnects,
            "registries": self.registries,
            "unknown_seq_batches": self.unknown_seq_batches,
            "bad_version_batches": self.bad_version_batches,
            "malformed_payloads": self.malformed_payloads,
            "invalid_values": self.invalid_values,
            "config_reloads": self.config_reloads,
            "config_mtime_ns": self.config_mtime_ns,
            "unmatched_rules": list(self.unmatched_rules),
        }


class _HealthHandler(BaseHTTPRequestHandler):
    state: HealthState

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/health":
            self.send_error(404)
            return
        payload = json.dumps(self.state.snapshot(), sort_keys=True).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Silence request logging; the service owns its log output."""


def serve_health(state: HealthState, port: int, host: str = "") -> ThreadingHTTPServer:
    handler = type("HealthHandler", (_HealthHandler,), {"state": state})
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="live-health", daemon=True)
    thread.start()
    logger.info("live-decoder: serving /health on port %d", server.server_port)
    return server
