"""Session-control HTTP API tests against the real stdlib server."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path

from pit.session_control.service import SessionController, StateFile, serve_http


class Database:
    connected = True
    pending_count = 0
    errors = 0

    async def record(self, state: dict[str, object]) -> bool:
        return True


class Publisher:
    connected = True
    pending_count = 0
    published = 0
    errors = 0

    def submit(self, payload: dict[str, object]) -> None:
        self.published += 1


def test_http_session_roster_and_health_endpoints(tmp_path: Path):
    async def exercise() -> None:
        database = Database()
        publisher = Publisher()
        controller = SessionController(StateFile(tmp_path / "session.json"), database, publisher)
        roster = tmp_path / "roster.json"
        roster.write_text(
            json.dumps(
                {
                    "drivers": ["Driver A", "Driver B"],
                    "session_types": ["race", "test"],
                }
            ),
            encoding="utf-8",
        )
        server = serve_http(
            asyncio.get_running_loop(),
            controller,
            roster,
            database,
            publisher,
            port=0,
            host="127.0.0.1",
        )
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            code, body = await asyncio.to_thread(_request, f"{base}/roster")
            assert code == 200
            assert body["drivers"] == ["Driver A", "Driver B"]

            code, body = await asyncio.to_thread(
                _request,
                f"{base}/session/start",
                {"session_type": "race", "driver": "Driver A"},
            )
            assert code == 200
            assert body["status"] == "active"

            code, body = await asyncio.to_thread(_request, f"{base}/session")
            assert code == 200
            assert body["driver"] == "Driver A"

            code, body = await asyncio.to_thread(_request, f"{base}/health")
            assert code == 200
            assert body == {
                "status": "ok",
                "database": {"connected": True, "errors": 0, "pending": 0},
                "nats": {"connected": True, "errors": 0, "pending": 0, "published": 1},
            }
        finally:
            server.shutdown()
            server.server_close()

    asyncio.run(exercise())


def test_http_rejects_invalid_json_and_invalid_transition(tmp_path: Path):
    async def exercise() -> None:
        database = Database()
        publisher = Publisher()
        server = serve_http(
            asyncio.get_running_loop(),
            SessionController(StateFile(tmp_path / "session.json"), database, publisher),
            tmp_path / "missing-roster.json",
            database,
            publisher,
            port=0,
            host="127.0.0.1",
        )
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            code, body = await asyncio.to_thread(_raw_request, f"{base}/session/start", b"{broken")
            assert code == 400
            assert body == {"error": "invalid JSON body"}

            code, body = await asyncio.to_thread(_request, f"{base}/session/end", {})
            assert code == 409
            assert "no active session" in body["error"]
        finally:
            server.shutdown()
            server.server_close()

    asyncio.run(exercise())


def _request(url: str, body: dict[str, object] | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    return _raw_request(url, data)


def _raw_request(url: str, data: bytes | None) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    try:
        response = urllib.request.urlopen(request, timeout=2)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, json.loads(response.read())
