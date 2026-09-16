"""The notifier: Grafana's only contact point, and the annunciator it serves.

One process, three threads: the HTTP server (Grafana's webhook, the page,
its server-sent-events feed, acknowledgements, /health), a one-second tick
that repeats unacknowledged alerts and retries failed deliveries, and the
main thread waiting to stop. State lives in the `AlertBook`; the ledger is
the only thing that touches the database and it never blocks a delivery.

The webhook is unauthenticated by design, like session-control's: the pit
compose network is the boundary, and a committed file must not carry the
operator key. What an unauthenticated caller can do is bounded by the
parser -- it can raise alerts on the page, which is what the endpoint is
for, and nothing else.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

from pit.notifier.alerts import SEVERITIES, AlertBook, Event, parse_notification
from pit.notifier.channels import (
    AnnunciatorChannel,
    Broadcaster,
    Channel,
    Dispatcher,
    LogChannel,
    drain,
)
from pit.notifier.config import ChannelConfig, NotifierConfig, NotifierSettings, load_config
from pit.notifier.ledger import AlertLedger
from pit.session_control.service import BoundedThreadingHTTPServer

logger = logging.getLogger(__name__)

_MAX_REQUEST_BODY_BYTES = 256 * 1024
_MAX_ACK_NAME_CHARS = 40
_MAX_ACK_NOTE_CHARS = 200
_SSE_KEEPALIVE_S = 15.0
_TICK_S = 1.0

_STATIC_FILES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


class LedgerLike:
    """What the service needs from a ledger; tests substitute a fake."""

    connected: bool
    errors: int
    written: int

    def record(self, events: list[Event]) -> int: ...
    def close(self) -> None: ...


def build_channels(config: NotifierConfig, broadcaster: Broadcaster) -> list[Channel]:
    """The configured channels, in order. Unknown types are P7.3's to add."""
    channels: list[Channel] = []
    for spec in config.channels:
        channels.append(_build_channel(spec, broadcaster))
    return channels


def _build_channel(spec: ChannelConfig, broadcaster: Broadcaster) -> Channel:
    if spec.type == "log":
        return LogChannel(spec.name, spec.min_severity)
    if spec.type == "annunciator":
        return AnnunciatorChannel(broadcaster, spec.name, spec.min_severity, spec.repeat_s)
    raise ValueError(f"channel type {spec.type!r} is not available yet (P7.3)")


class NotifierService:
    def __init__(
        self,
        settings: NotifierSettings,
        *,
        config: NotifierConfig | None = None,
        ledger: LedgerLike | None = None,
        channels: list[Channel] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.config = config if config is not None else load_config(settings.config_path)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.broadcaster = Broadcaster()
        self.book = AlertBook(
            heartbeat_rule=self.config.heartbeat_rule,
            heartbeat_expected_s=self.config.heartbeat_expected_s,
            heartbeat_missed_intervals=self.config.heartbeat_missed_intervals,
        )
        self.dispatcher = Dispatcher(
            channels if channels is not None else build_channels(self.config, self.broadcaster),
            retry_deadline_s=self.config.retry_deadline_s,
        )
        if ledger is not None:
            self.ledger: LedgerLike | None = ledger
        elif settings.dsn:
            self.ledger = AlertLedger(settings.dsn)
        else:
            self.ledger = None
            logger.warning("notifier: no TIMESCALE_* configured; running without a ledger")
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self.server: BoundedThreadingHTTPServer | None = None

    # -- the three things a request can do ----------------------------------

    def receive(self, payload: Any) -> int:
        now = self.clock()
        alerts = parse_notification(payload, now)
        with self._lock:
            events = self.book.receive(alerts, now)
            for event in events:
                self.dispatcher.announce(event, self.book)
        self._after(events)
        return len(alerts)

    def ack(self, fingerprint: str, by: str, note: str | None) -> Event | None:
        now = self.clock()
        with self._lock:
            event = self.book.ack(fingerprint, by, note, now)
            if event is not None:
                self.dispatcher.announce(event, self.book)
        self._after([event] if event else [])
        return event

    def raise_test(self, severity: str) -> list[Event]:
        now = self.clock()
        with self._lock:
            events = self.book.raise_test(severity, now)
            for event in events:
                self.dispatcher.announce(event, self.book)
        self._after(events)
        return events

    def _after(self, events: list[Event]) -> None:
        if self.ledger is not None and events:
            self.ledger.record(events)
        self._push_snapshot()

    def _push_snapshot(self) -> None:
        self.broadcaster.publish({"type": "snapshot", **self.book.snapshot(self.clock())})

    # -- the tick --------------------------------------------------------------

    def tick(self) -> None:
        now = self.clock()
        with self._lock:
            repeated = self.dispatcher.repeat_due(self.book, now)
            retried = self.dispatcher.pump()
        if repeated or retried:
            self._push_snapshot()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return self.book.snapshot(self.clock())

    def health(self) -> dict[str, object]:
        now = self.clock()
        with self._lock:
            heartbeat_ok = self.book.heartbeat_ok(now)
            payload: dict[str, object] = {
                "status": "ok" if heartbeat_ok is not False else "degraded",
                "uptime_s": round(time.monotonic() - self._started, 1),
                "active": len(self.book.active),
                "unacknowledged": len(self.book.unacknowledged()),
                "notifications": self.book.notifications,
                "heartbeat": {
                    "seen_at": (
                        self.book.heartbeat_seen.isoformat() if self.book.heartbeat_seen else None
                    ),
                    "age_s": self.book.heartbeat_age_s(now),
                    "ok": heartbeat_ok,
                    "count": self.book.heartbeats,
                },
                "queue": self.dispatcher.snapshot(),
                "ledger": {
                    "configured": self.ledger is not None,
                    "connected": bool(self.ledger and self.ledger.connected),
                    "errors": int(self.ledger.errors) if self.ledger else 0,
                    "written": int(self.ledger.written) if self.ledger else 0,
                },
                "sse_clients": self.broadcaster.client_count,
                "channels": [
                    {"name": c.name, "min_severity": c.min_severity, "repeat_s": c.repeat_s}
                    for c in self.dispatcher.channels
                ],
            }
        return payload

    # -- lifecycle -------------------------------------------------------------

    def serve(self, host: str | None = None, port: int | None = None) -> BoundedThreadingHTTPServer:
        handler = type("NotifierHandler", (_Handler,), {"service": self})
        bind_host = self.settings.host if host is None else host
        bind_port = self.settings.port if port is None else port
        self.server = BoundedThreadingHTTPServer((bind_host, bind_port), handler)
        threading.Thread(
            target=self.server.serve_forever, name="notifier-http", daemon=True
        ).start()
        logger.info("notifier: listening on %s:%s", *self.server.server_address[:2])
        return self.server

    def run(self, stop: threading.Event) -> None:
        self.serve()
        try:
            while not stop.is_set():
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 - the tick must outlive any one bug
                    logger.exception("notifier: tick failed")
                stop.wait(_TICK_S)
        finally:
            if self.server is not None:
                self.server.shutdown()
                self.server.server_close()
            if self.ledger is not None:
                self.ledger.close()


class _Handler(BaseHTTPRequestHandler):
    service: NotifierService
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        static = _STATIC_FILES.get(path)
        if static is not None:
            self._send_static(*static)
        elif path == "/health":
            self._send(200, self.service.health())
        elif path == "/alerts":
            self._send(200, self.service.snapshot())
        elif path == "/events":
            self._stream_events()
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if not self._origin_permitted():
            return
        body = self._read_json_body()
        if body is None:
            return
        if path == "/grafana-alerts":
            self._send(200, {"received": self.service.receive(body)})
        elif path == "/ack":
            fingerprint = body.get("fingerprint")
            by = body.get("by")
            note = body.get("note")
            if not isinstance(fingerprint, str) or not isinstance(by, str) or not by.strip():
                self._send(400, {"error": "fingerprint and by are required"})
                return
            note_text = str(note)[:_MAX_ACK_NOTE_CHARS] if isinstance(note, str) and note else None
            event = self.service.ack(fingerprint, by.strip()[:_MAX_ACK_NAME_CHARS], note_text)
            if event is None:
                self._send(404, {"error": "no active alert with that fingerprint"})
                return
            self._send(200, event.as_dict())
        elif path == "/test":
            severity = body.get("severity", "critical")
            if severity not in SEVERITIES or severity == "none":
                self._send(400, {"error": "severity must be warning or critical"})
                return
            events = self.service.raise_test(severity)
            self._send(200, {"raised": [event.as_dict() for event in events]})
        else:
            self._send(404, {"error": "not found"})

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_error(405)

    # -- server-sent events ------------------------------------------------------

    def _stream_events(self) -> None:
        client = self.service.broadcaster.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self._write_event({"type": "snapshot", **self.service.snapshot()})
            while True:
                payload = drain(client, _SSE_KEEPALIVE_S)
                if payload is None:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self._write_event(payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.service.broadcaster.unsubscribe(client)
            self.close_connection = True

    def _write_event(self, payload: dict[str, object]) -> None:
        data = json.dumps(payload, default=str)
        self.wfile.write(f"event: {payload.get('type', 'message')}\ndata: {data}\n\n".encode())
        self.wfile.flush()

    # -- plumbing, in session-control's shape ------------------------------------

    def _send(self, status: int, value: object) -> None:
        body = json.dumps(value, sort_keys=True, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, filename: str, content_type: str) -> None:
        try:
            body = (resources.files("pit.notifier") / "static" / filename).read_bytes()
        except OSError:
            logger.exception("notifier: cannot read static asset %s", filename)
            self._send(500, {"error": "internal server error"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _origin_permitted(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        host = self.headers.get("Host", "")
        if origin.lower() == f"http://{host}".lower():
            return True
        self._send(403, {"error": "cross-origin request denied"})
        return False

    def _read_json_body(self) -> dict | None:
        if self.headers.get("Transfer-Encoding"):
            self._send(400, {"error": "Transfer-Encoding is not supported"})
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError("negative Content-Length")
            if length > _MAX_REQUEST_BODY_BYTES:
                self.close_connection = True
                self._send(413, {"error": "request body too large"})
                return None
            if length and self.headers.get_content_type() != "application/json":
                self._send(415, {"error": "Content-Type must be application/json"})
                return None
            decoded = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(decoded, dict):
                raise TypeError
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            message = (
                "invalid Content-Length" if "Content-Length" in str(exc) else "invalid JSON body"
            )
            self._send(400, {"error": message})
            return None
        return decoded

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug(format, *args)
