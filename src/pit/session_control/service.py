"""Session-control orchestration, persistence, roster, and HTTP API."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

from pit.db.dsn import dsn_from_env
from pit.session_control.database import SessionDatabase
from pit.session_control.publisher import LatestSessionPublisher
from pit.session_control.state import SESSION_TYPES, SessionError, SessionState

logger = logging.getLogger(__name__)

_MAX_REQUEST_BODY_BYTES = 64 * 1024
_REQUEST_TIMEOUT_S = 5.0
_MAX_HTTP_WORKERS = 32

# The operator UI (P5.6), served by this process from package data. A strict
# filename allowlist, never a request path joined to a directory: path
# traversal on a hand-rolled BaseHTTPRequestHandler is the classic way to
# serve /etc/passwd from a telemetry box. Anything not in this dict is 404.
_STATIC_FILES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class SessionPersistenceError(RuntimeError):
    """A transition could not be made durable and was rolled back."""


@dataclass(frozen=True, slots=True)
class SessionControlSettings:
    """Deploy-time wiring for session-control; see ``example.env``."""

    nats_url: str
    vehicle_id: str
    dsn: str
    vehicle_js_domain: str | None = "veh"
    creds_path: str | None = None
    state_file: Path = Path("/data/session-state.json")
    roster_file: Path = Path("/config/roster.json")
    default_track: str = ""
    http_host: str = "127.0.0.1"
    http_port: int = 8080
    api_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SessionControlSettings:
        env = os.environ if env is None else env
        vehicle_id = env.get("OPENLAPS_VEHICLE_ID", "").strip()
        if not vehicle_id:
            raise ValueError("OPENLAPS_VEHICLE_ID is required (see example.env)")
        domain = env.get("OPENLAPS_VEHICLE_JS_DOMAIN", "veh").strip()
        http_host = env.get("OPENLAPS_SESSION_HOST", "127.0.0.1").strip() or "127.0.0.1"
        api_key = env.get("OPENLAPS_SESSION_API_KEY", "").strip() or None
        if not _is_loopback_host(http_host) and api_key is None:
            raise ValueError("OPENLAPS_SESSION_API_KEY is required for non-loopback HTTP binding")
        port = int(env.get("OPENLAPS_SESSION_PORT", "8080"))
        if not 0 < port < 65_536:
            raise ValueError("OPENLAPS_SESSION_PORT must be between 1 and 65535")
        return cls(
            nats_url=env.get("OPENLAPS_NATS_URL", "nats://127.0.0.1:4222").strip(),
            vehicle_id=vehicle_id,
            dsn=dsn_from_env(env),
            vehicle_js_domain=domain or None,
            creds_path=env.get("OPENLAPS_NATS_CREDS", "").strip() or None,
            state_file=Path(env.get("OPENLAPS_SESSION_STATE_FILE", "/data/session-state.json")),
            roster_file=Path(env.get("OPENLAPS_SESSION_ROSTER", "/config/roster.json")),
            default_track=env.get("OPENLAPS_SESSION_DEFAULT_TRACK", "").strip(),
            http_host=http_host,
            http_port=port,
            api_key=api_key,
        )


class DatabaseRecorder(Protocol):
    async def record(self, state: dict[str, object]) -> bool:
        """Write now or queue for retry; return whether it landed immediately."""
        ...


class StatePublisher(Protocol):
    def submit(self, payload: dict[str, object]) -> None:
        """Queue the latest session payload for publication."""
        ...


class StateFile:
    """Atomic on-disk persistence for restart continuity."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> SessionState:
        try:
            decoded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(decoded, dict):
                raise TypeError("session state must be a JSON object")
            return SessionState.from_dict(decoded)
        except FileNotFoundError:
            return SessionState()
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("session-control: ignoring state file %s: %s", self.path, exc)
            return SessionState()

    def save(self, state: SessionState) -> bool:
        payload = json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n"
        temporary_name: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as temporary:
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_name = temporary.name
            os.replace(temporary_name, self.path)
            return True
        except OSError as exc:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
            logger.warning("session-control: cannot persist state to %s: %s", self.path, exc)
            return False


def load_roster(path: str | Path) -> dict[str, list[str]]:
    """Load the operator roster, falling back to an empty safe roster.

    ``tracks`` is optional so an old roster file keeps working, and unknown
    keys are ignored so the roster can keep growing without a lockstep
    deploy (docs/plan/PHASE5.md -> P5.6).
    """
    roster_path = Path(path)
    try:
        decoded = json.loads(roster_path.read_text(encoding="utf-8"))
        if not isinstance(decoded, dict):
            raise TypeError("roster must be a JSON object")
        drivers = decoded.get("drivers")
        session_types = decoded.get("session_types")
        tracks = decoded.get("tracks", [])
        if not isinstance(drivers, list) or not all(isinstance(item, str) for item in drivers):
            raise TypeError("roster.drivers must be a list of strings")
        if not isinstance(session_types, list) or not all(
            isinstance(item, str) and item in SESSION_TYPES for item in session_types
        ):
            raise TypeError(f"roster.session_types must contain only {SESSION_TYPES}")
        if not isinstance(tracks, list) or not all(isinstance(item, str) for item in tracks):
            raise TypeError("roster.tracks must be a list of strings")
        return {"drivers": drivers, "session_types": session_types, "tracks": tracks}
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("session-control: cannot load roster %s: %s", roster_path, exc)
        return {"drivers": [], "session_types": list(SESSION_TYPES), "tracks": []}


class SessionController:
    """Serialise state transitions and coordinate disk, DB, then NATS."""

    def __init__(
        self,
        state_file: StateFile,
        database: DatabaseRecorder,
        publisher: StatePublisher,
        *,
        default_track: str = "",
    ) -> None:
        self.state_file = state_file
        self.database = database
        self.publisher = publisher
        self.default_track = default_track
        self.state = state_file.load()
        self._lock = asyncio.Lock()

    async def act(
        self,
        action: str,
        body: Mapping[str, object],
        *,
        now_ms: int | None = None,
    ) -> dict[str, object]:
        """Apply one API action; DB precedes publish whenever it is reachable."""
        async with self._lock:
            prior_state = self.state.to_dict()
            # Buttons get pressed late: an optional `at` (epoch ms) backdates
            # a driver change or an end to when it actually happened. It must
            # not be in the future (server clock), and the state machine
            # already refuses anything before the active stint started. Start
            # is not backdatable — nothing has happened yet to be late about.
            at = body.get("at")
            if at is not None:
                if isinstance(at, bool) or not isinstance(at, int):
                    raise SessionError("at must be epoch milliseconds")
                if action == "start":
                    raise SessionError("start cannot be backdated")
                if at > int(time.time() * 1000):
                    raise SessionError("at cannot be in the future")
                now_ms = at
            if action == "start":
                payload = self.state.start_session(
                    _text(body.get("session_type")),
                    _text(body.get("driver")),
                    _text(body.get("track_name")) or self.default_track,
                    _text(body.get("car")),
                    now_ms=now_ms,
                )
            elif action == "driver":
                payload = self.state.change_driver(_text(body.get("driver")), now_ms=now_ms)
            elif action == "end":
                payload = self.state.end_session(now_ms=now_ms)
            else:
                raise SessionError(f"unknown action {action!r}")
            if not self.state_file.save(self.state):
                self.state = SessionState.from_dict(prior_state)
                raise SessionPersistenceError("failed to persist session state")
            try:
                await self.database.record(self.state.to_dict())
            except Exception as exc:
                self.state = SessionState.from_dict(prior_state)
                if not self.state_file.save(self.state):
                    raise RuntimeError(
                        "failed to roll back session after retry-queue failure"
                    ) from exc
                if isinstance(exc, OSError):
                    raise SessionPersistenceError("failed to persist database retry queue") from exc
                raise
            self.publisher.submit(payload)
            return payload

    async def current(self) -> dict[str, object]:
        async with self._lock:
            return self.state.to_dict()

    def republish_current(self) -> None:
        """Re-assert persisted state after a service or broker restart."""
        if self.state.status != "none":
            self.publisher.submit(self.state.payload())


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


async def _wait_for_stop_or_task_failure(
    stop: asyncio.Event,
    tasks: tuple[asyncio.Task[None], ...],
) -> None:
    """Return on requested shutdown; propagate unexpected worker exits."""
    stop_task = asyncio.create_task(stop.wait(), name="session-control-stop")
    try:
        done, _ = await asyncio.wait(
            {stop_task, *tasks},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done:
            return
        worker = next(task for task in tasks if task in done)
        if worker.cancelled():
            raise RuntimeError(f"{worker.get_name()} was unexpectedly cancelled")
        error = worker.exception()
        if error is not None:
            raise RuntimeError(f"{worker.get_name()} failed") from error
        raise RuntimeError(f"{worker.get_name()} exited unexpectedly")
    finally:
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)


async def run_service(settings: SessionControlSettings, stop: asyncio.Event) -> None:
    """Run DB retry, latest-value publisher, and operator HTTP API."""
    database = SessionDatabase(
        settings.dsn,
        settings.vehicle_id,
        queue_path=settings.state_file.with_name(
            f"{settings.state_file.stem}-db-queue{settings.state_file.suffix}"
        ),
    )
    publisher = LatestSessionPublisher(
        settings.nats_url,
        settings.vehicle_id,
        domain=settings.vehicle_js_domain,
        creds_path=settings.creds_path,
    )
    controller = SessionController(
        StateFile(settings.state_file),
        database,
        publisher,
        default_track=settings.default_track,
    )

    # A bounded probe gives the happy path its required DB-before-NATS order.
    # Failure is not fatal: record() queues the persisted snapshot and the two
    # background loops heal independently.
    await database.connect_once()
    if controller.state.status != "none":
        await database.record(controller.state.to_dict())
        controller.republish_current()

    database_task = asyncio.create_task(database.run(stop), name="session-database")
    publisher_task = asyncio.create_task(publisher.run(stop), name="session-publisher")
    server = serve_http(
        asyncio.get_running_loop(),
        controller,
        settings.roster_file,
        database,
        publisher,
        settings.http_port,
        host=settings.http_host,
        api_key=settings.api_key,
    )
    try:
        await _wait_for_stop_or_task_failure(stop, (database_task, publisher_task))
    finally:
        stop.set()
        try:
            await asyncio.to_thread(server.shutdown)
        finally:
            server.server_close()
            await asyncio.gather(database_task, publisher_task, return_exceptions=True)


class _SessionHandler(BaseHTTPRequestHandler):
    loop: asyncio.AbstractEventLoop
    controller: SessionController
    roster_path: Path
    database: object
    publisher: object
    api_key: str | None

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(_REQUEST_TIMEOUT_S)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(405, {"error": "cross-origin requests are not allowed"})

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", maxsplit=1)[0]
        # /health is deliberately outside the bearer-token gate, matching the
        # other three pit services. A container has to bind 0.0.0.0, which
        # makes the API key mandatory (see SessionControlSettings.from_env),
        # and deploy/pit-compose.yaml's healthcheck curls this endpoint with
        # no credentials -- so gating it meant the container was reported
        # unhealthy forever while the service itself was fine. The payload is
        # liveness counters only: no session, roster or credential data.
        if path == "/health":
            self._send(200, _health_payload(self.database, self.publisher))
            return
        # The operator UI is also outside the bearer gate: it is where the
        # key gets entered, and it is public-repository HTML with no data in
        # it — every API call the page makes is still gated.
        static = _STATIC_FILES.get(path)
        if static is not None:
            self._send_static(*static)
            return
        if not self._authorized():
            return
        if path == "/session":
            future = asyncio.run_coroutine_threadsafe(self.controller.current(), self.loop)
            try:
                self._send(200, future.result(timeout=5.0))
            except TimeoutError:
                future.cancel()
                self._send(503, {"error": "service busy"})
        elif path == "/roster":
            self._send(200, load_roster(self.roster_path))
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", maxsplit=1)[0]
        parts = path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "session":
            self._send(404, {"error": "not found"})
            return
        if not self._authorized():
            return
        # Browsers send Origin on every request whose method is not GET/HEAD,
        # including same-origin ones — so the operator UI this process serves
        # must be let through, and only it. This server speaks plain HTTP and
        # never terminates TLS, so the one acceptable Origin is exactly
        # http://<Host header>. Anything else is refused, and a request with
        # no Origin at all (curl, the compose healthcheck) keeps working.
        # do_OPTIONS stays 405: a same-origin UI needs no preflight, and not
        # implementing CORS is the point.
        origin = self.headers.get("Origin")
        if origin is not None and not self._same_origin(origin):
            self._send(403, {"error": "cross-origin request denied"})
            return
        if self.headers.get("Transfer-Encoding"):
            self._send(400, {"error": "Transfer-Encoding is not supported"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError("negative Content-Length")
            if length > _MAX_REQUEST_BODY_BYTES:
                self.close_connection = True
                self._send(413, {"error": "request body too large"})
                return
            content_type = self.headers.get_content_type()
            if length and content_type != "application/json":
                self._send(415, {"error": "Content-Type must be application/json"})
                return
            decoded = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(decoded, dict):
                raise TypeError
        except TimeoutError:
            self.close_connection = True
            self._send(408, {"error": "request body timeout"})
            return
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            message = (
                "invalid Content-Length" if "Content-Length" in str(exc) else "invalid JSON body"
            )
            self._send(400, {"error": message})
            return
        future = asyncio.run_coroutine_threadsafe(self.controller.act(parts[1], decoded), self.loop)
        try:
            self._send(200, future.result(timeout=5.0))
        except SessionPersistenceError as exc:
            self._send(503, {"error": str(exc)})
        except SessionError as exc:
            self._send(409, {"error": str(exc)})
        except TimeoutError:
            future.cancel()
            self._send(503, {"error": "service busy"})
        except Exception:  # noqa: BLE001 - HTTP boundary logs unexpected failures
            logger.exception("session-control: action failed")
            self._send(500, {"error": "internal server error"})

    def _send(self, status: int, value: object) -> None:
        body = json.dumps(value, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _same_origin(self, origin: str) -> bool:
        host = self.headers.get("Host", "").strip()
        return bool(host) and origin.strip().lower() == f"http://{host.lower()}"

    def _send_static(self, filename: str, content_type: str) -> None:
        try:
            body = (resources.files("pit.session_control") / "static" / filename).read_bytes()
        except OSError:
            logger.exception("session-control: cannot read static asset %s", filename)
            self._send(500, {"error": "internal server error"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The page is edited in the repository and redeployed; a stale cached
        # copy against a newer API is a confusing failure for zero benefit.
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.api_key is None or secrets.compare_digest(
            self.headers.get("Authorization", ""), f"Bearer {self.api_key}"
        ):
            return True
        self._send(401, {"error": "authentication required"})
        return False

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
        max_workers: int = _MAX_HTTP_WORKERS,
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


def serve_http(
    loop: asyncio.AbstractEventLoop,
    controller: SessionController,
    roster_path: str | Path,
    database: object,
    publisher: object,
    port: int,
    host: str = "127.0.0.1",
    api_key: str | None = None,
) -> BoundedThreadingHTTPServer:
    """Start the operator API and its static UI on a daemon thread."""
    if not _is_loopback_host(host) and api_key is None:
        raise ValueError("api_key is required for non-loopback HTTP binding")
    handler = type(
        "SessionHandler",
        (_SessionHandler,),
        {
            "loop": loop,
            "controller": controller,
            "roster_path": Path(roster_path),
            "database": database,
            "publisher": publisher,
            "api_key": api_key,
        },
    )
    server = BoundedThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="session-control-http", daemon=True)
    thread.start()
    logger.info("session-control: HTTP API listening on port %d", server.server_port)
    return server


def _health_payload(database: object, publisher: object) -> dict[str, object]:
    return {
        "status": "ok",
        "database": {
            "connected": bool(getattr(database, "connected", False)),
            "errors": int(getattr(database, "errors", 0)),
            "pending": int(getattr(database, "pending_count", 0)),
        },
        "nats": {
            "connected": bool(getattr(publisher, "connected", False)),
            "errors": int(getattr(publisher, "errors", 0)),
            "pending": int(getattr(publisher, "pending_count", 0)),
            "published": int(getattr(publisher, "published", 0)),
        },
    }
