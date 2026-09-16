"""The service's HTTP surface: ``/health``, ``POST /ingest/snapshot``, ``WS /ingest/t71``.

Unauthenticated on the compose network, like session-control's Grafana
webhook, and bounded the same way: request bodies are capped, the worker
count is capped, a WebSocket message is capped, and a malformed body is a
400 that changes nothing. What an unauthenticated caller can do is put a
standings row into a table whose job is to hold standings rows.

Cross-origin posts are accepted on purpose. The browser relay runs as a
userscript on a timing provider's page and posts from that origin; refusing
it would refuse the one caller the endpoint exists for. The body may be
``text/plain`` for the same reason -- a ``no-cors`` fetch cannot send
``application/json`` -- and is parsed as JSON either way.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from pit.timing_feed import websocket
from pit.timing_feed.health import HealthState
from pit.timing_feed.model import Snapshot
from pit.timing_feed.shapes import ShapeError, T71Translator, snapshot_from_json

logger = logging.getLogger(__name__)

MAX_REQUEST_BODY_BYTES = 256 * 1024
MAX_HTTP_WORKERS = 16

Sink = Callable[[Snapshot], None]


class _IngestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30.0
    state: HealthState
    sink: Sink
    loop: asyncio.AbstractEventLoop

    # -- GET: health, and the WebSocket upgrade

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", maxsplit=1)[0]
        if path == "/health":
            self._send(200, self.state.snapshot())
            return
        if path == "/ingest/t71":
            self._t71()
            return
        self._send(404, {"error": "not found"})

    def _t71(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        upgrade = (self.headers.get("Upgrade") or "").lower()
        if upgrade != "websocket" or not key:
            self._send(426, {"error": "this endpoint speaks WebSocket"})
            return
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", websocket.accept_key(key))
        self.end_headers()
        self.close_connection = True
        # A Timing71 service sends state every second for hours; no idle limit.
        self.connection.settimeout(None)
        translator = T71Translator()
        logger.info("ingest: t71 client connected from %s", self.client_address[0])
        try:
            for text in websocket.messages(self.rfile, self.wfile):
                self._t71_message(translator, text)
        except (websocket.WebSocketError, OSError) as exc:
            logger.warning("ingest: t71 client %s dropped: %s", self.client_address[0], exc)
        else:
            logger.info("ingest: t71 client %s closed", self.client_address[0])

    def _t71_message(self, translator: T71Translator, text: str) -> None:
        try:
            message = json.loads(text)
            if not isinstance(message, dict):
                raise ShapeError("message is not an object")
            kind = message.get("type")
            if kind == "MANIFEST_UPDATE":
                manifest = message.get("manifest")
                if not isinstance(manifest, dict):
                    raise ShapeError("manifest must be an object")
                translator.manifest(manifest)
            elif kind == "STATE_UPDATE":
                state = message.get("state")
                if not isinstance(state, dict):
                    raise ShapeError("state must be an object")
                snapshot = translator.state(state, datetime.now(UTC))
                self.loop.call_soon_threadsafe(self.sink, snapshot)
            else:
                # ANALYSIS_STATE and anything else: not an ingest shape.
                return
            self.state.ingest_t71_messages += 1
        except (ValueError, TypeError) as exc:
            self.state.ingest_rejected += 1
            logger.warning("ingest: t71 message rejected: %s", str(exc)[:200])

    # -- POST: one snapshot

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", maxsplit=1)[0]
        if path != "/ingest/snapshot":
            self._send(404, {"error": "not found"})
            return
        body = self._read_json_body()
        if body is None:
            return
        try:
            snapshot = snapshot_from_json(body, datetime.now(UTC), "relay")
        except ShapeError as exc:
            self.state.ingest_rejected += 1
            self._send(400, {"error": str(exc)})
            return
        self.loop.call_soon_threadsafe(self.sink, snapshot)
        self.state.ingest_snapshots += 1
        self._send(200, {"accepted": len(snapshot.cars)})

    def do_OPTIONS(self) -> None:  # noqa: N802
        # A relay using a plain fetch with application/json would preflight;
        # answer it so that path works too, on the LAN the service trusts.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- plumbing

    def _send(self, status: int, value: object) -> None:
        body = json.dumps(value, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict | None:
        if self.headers.get("Transfer-Encoding"):
            self._send(400, {"error": "Transfer-Encoding is not supported"})
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError("negative Content-Length")
            if length > MAX_REQUEST_BODY_BYTES:
                self.close_connection = True
                self._send(413, {"error": "request body too large"})
                return None
            content_type = self.headers.get_content_type()
            if length and content_type not in ("application/json", "text/plain"):
                self._send(415, {"error": "Content-Type must be application/json or text/plain"})
                return None
            decoded = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(decoded, dict):
                raise TypeError
        except TimeoutError:
            self.close_connection = True
            self._send(408, {"error": "request body timeout"})
            return None
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            message = (
                "invalid Content-Length" if "Content-Length" in str(exc) else "invalid JSON body"
            )
            self._send(400, {"error": message})
            return None
        return decoded

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug(format, *args)


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server with a hard cap on concurrent request workers."""

    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        max_workers: int = MAX_HTTP_WORKERS,
    ) -> None:
        self.max_workers = max_workers
        self._worker_slots = threading.BoundedSemaphore(max_workers)
        super().__init__(server_address, handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._worker_slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Connection: close\r\nContent-Length: 0\r\n\r\n"
                )
            finally:
                self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()


def serve_ingest(
    loop: asyncio.AbstractEventLoop,
    sink: Sink,
    state: HealthState,
    *,
    port: int,
    host: str = "",
) -> BoundedThreadingHTTPServer:
    """Serve the ingest endpoints and `/health` on a daemon thread."""
    attributes = {"state": state, "sink": staticmethod(sink), "loop": loop}
    handler = type("IngestHandler", (_IngestHandler,), attributes)
    server = BoundedThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="ingest", daemon=True)
    thread.start()
    logger.info(
        "ingest: serving /health, /ingest/snapshot and /ingest/t71 on port %d",
        server.server_port,
    )
    return server
