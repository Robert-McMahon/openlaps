"""Session-control orchestration, persistence, roster, and HTTP API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol

from pit.db.dsn import dsn_from_env
from pit.session_control.database import SessionDatabase
from pit.session_control.publisher import LatestSessionPublisher
from pit.session_control.state import SESSION_TYPES, SessionError, SessionState

logger = logging.getLogger(__name__)


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
    http_port: int = 8080

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SessionControlSettings:
        env = os.environ if env is None else env
        vehicle_id = env.get("OPENLAPS_VEHICLE_ID", "").strip()
        if not vehicle_id:
            raise ValueError("OPENLAPS_VEHICLE_ID is required (see example.env)")
        domain = env.get("OPENLAPS_VEHICLE_JS_DOMAIN", "veh").strip()
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
            http_port=port,
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
    """Load the operator roster, falling back to an empty safe roster."""
    roster_path = Path(path)
    try:
        decoded = json.loads(roster_path.read_text(encoding="utf-8"))
        if not isinstance(decoded, dict):
            raise TypeError("roster must be a JSON object")
        drivers = decoded.get("drivers")
        session_types = decoded.get("session_types")
        if not isinstance(drivers, list) or not all(isinstance(item, str) for item in drivers):
            raise TypeError("roster.drivers must be a list of strings")
        if not isinstance(session_types, list) or not all(
            isinstance(item, str) and item in SESSION_TYPES for item in session_types
        ):
            raise TypeError(f"roster.session_types must contain only {SESSION_TYPES}")
        return {"drivers": drivers, "session_types": session_types}
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("session-control: cannot load roster %s: %s", roster_path, exc)
        return {"drivers": [], "session_types": list(SESSION_TYPES)}


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
            self.state_file.save(self.state)
            await self.database.record(self.state.to_dict())
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
    )
    try:
        await stop.wait()
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        await asyncio.gather(database_task, publisher_task)


class _SessionHandler(BaseHTTPRequestHandler):
    loop: asyncio.AbstractEventLoop
    controller: SessionController
    roster_path: Path
    database: object
    publisher: object

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(200, {})

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", maxsplit=1)[0]
        if path == "/session":
            future = asyncio.run_coroutine_threadsafe(self.controller.current(), self.loop)
            try:
                self._send(200, future.result(timeout=5.0))
            except TimeoutError:
                future.cancel()
                self._send(503, {"error": "service busy"})
        elif path == "/roster":
            self._send(200, load_roster(self.roster_path))
        elif path == "/health":
            self._send(200, _health_payload(self.database, self.publisher))
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", maxsplit=1)[0]
        parts = path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "session":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            decoded = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(decoded, dict):
                raise TypeError
        except (TypeError, ValueError, json.JSONDecodeError):
            self._send(400, {"error": "invalid JSON body"})
            return
        future = asyncio.run_coroutine_threadsafe(self.controller.act(parts[1], decoded), self.loop)
        try:
            self._send(200, future.result(timeout=5.0))
        except SessionError as exc:
            self._send(409, {"error": str(exc)})
        except TimeoutError:
            future.cancel()
            self._send(503, {"error": "service busy"})
        except Exception as exc:  # noqa: BLE001 - HTTP boundary logs unexpected failures
            logger.exception("session-control: action failed")
            self._send(500, {"error": str(exc)})

    def _send(self, status: int, value: object) -> None:
        body = json.dumps(value, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug(format, *args)


def serve_http(
    loop: asyncio.AbstractEventLoop,
    controller: SessionController,
    roster_path: str | Path,
    database: object,
    publisher: object,
    port: int,
    host: str = "",
) -> ThreadingHTTPServer:
    """Start the six-endpoint operator API on a daemon thread."""
    handler = type(
        "SessionHandler",
        (_SessionHandler,),
        {
            "loop": loop,
            "controller": controller,
            "roster_path": Path(roster_path),
            "database": database,
            "publisher": publisher,
        },
    )
    server = ThreadingHTTPServer((host, port), handler)
    import threading

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
