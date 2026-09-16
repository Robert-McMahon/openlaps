"""Health state and /health endpoint for the timing extrapolator."""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pit.timing_extrapolator.engine import ClockValue


class HealthState:
    def __init__(self, stream: str, subject_filter: str) -> None:
        self.stream = stream
        self.subject_filter = subject_filter
        self.mqtt_connected = False
        self.mqtt_drops = 0
        self.mqtt_reconnects = 0
        self.nats_reconnects = 0
        self.registries = 0
        self.unknown_seq_batches = 0
        self.bad_version_batches = 0
        self.malformed_payloads = 0
        self.malformed_events = 0
        self.published: dict[str, int] = defaultdict(int)
        self.status: dict[str, str] = {}
        self.reason: dict[str, str | None] = {}
        self._started = time.monotonic()

    def note_publish(self, channel: str, value: ClockValue) -> None:
        self.published[channel] += 1
        self.status[channel] = value.status
        self.reason[channel] = value.reason

    def snapshot(self, now: float | None = None) -> dict[str, object]:
        current = time.monotonic() if now is None else now
        return {
            "uptime_s": round(max(0.0, current - self._started), 1),
            "stream": self.stream,
            "subject_filter": self.subject_filter,
            "mqtt_connected": self.mqtt_connected,
            "channels": {
                channel: {
                    "published": count,
                    "status": self.status.get(channel),
                    "reason": self.reason.get(channel),
                }
                for channel, count in sorted(self.published.items())
            },
            "mqtt_drops": self.mqtt_drops,
            "mqtt_reconnects": self.mqtt_reconnects,
            "nats_reconnects": self.nats_reconnects,
            "registries": self.registries,
            "unknown_seq_batches": self.unknown_seq_batches,
            "bad_version_batches": self.bad_version_batches,
            "malformed_payloads": self.malformed_payloads,
            "malformed_events": self.malformed_events,
        }


class _Handler(BaseHTTPRequestHandler):
    state: HealthState

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/health":
            self.send_error(404)
            return
        payload = json.dumps(self.state.snapshot(), sort_keys=True).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


def serve_health(state: HealthState, port: int, host: str = "") -> ThreadingHTTPServer:
    handler = type("TimingHealthHandler", (_Handler,), {"state": state})
    server = ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=server.serve_forever, name="timing-health", daemon=True).start()
    return server
