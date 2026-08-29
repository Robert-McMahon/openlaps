"""Session-control HTTP API tests against the real stdlib server."""

from __future__ import annotations

import asyncio
import http.client
import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

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


def test_http_returns_unavailable_when_state_cannot_be_persisted(tmp_path: Path, monkeypatch):
    async def exercise() -> None:
        database = Database()
        publisher = Publisher()
        state_file = StateFile(tmp_path / "session.json")
        monkeypatch.setattr(state_file, "save", lambda _state: False)
        server = serve_http(
            asyncio.get_running_loop(),
            SessionController(state_file, database, publisher),
            tmp_path / "missing-roster.json",
            database,
            publisher,
            port=0,
            host="127.0.0.1",
        )
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            code, body = await asyncio.to_thread(
                _request,
                f"{base}/session/start",
                {"session_type": "race", "driver": "Driver A"},
            )
            assert code == 503
            assert "persist" in body["error"]
            assert publisher.published == 0
        finally:
            server.shutdown()
            server.server_close()

    asyncio.run(exercise())


def test_http_rejects_unsafe_request_bodies_and_cross_origin(tmp_path: Path):
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
            code, body = await asyncio.to_thread(
                _length_only_request,
                f"{base}/session/start",
                65_537,
            )
            assert code == 413
            assert "too large" in body["error"]

            code, body = await asyncio.to_thread(
                _length_only_request,
                f"{base}/session/start",
                -1,
            )
            assert code == 400
            assert "Content-Length" in body["error"]

            request = urllib.request.Request(
                f"{base}/session/start",
                data=json.dumps({"session_type": "race", "driver": "Driver A"}).encode(),
                headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
                method="POST",
            )
            code, body = await asyncio.to_thread(_url_request, request)
            assert code == 403
            assert body == {"error": "cross-origin request denied"}
            assert publisher.published == 0
        finally:
            server.shutdown()
            server.server_close()

    asyncio.run(exercise())


def test_health_is_reachable_without_the_bearer_token(tmp_path: Path):
    """The compose healthcheck curls /health with no credentials.

    A container binds 0.0.0.0, which makes OPENLAPS_SESSION_API_KEY mandatory,
    and deploy/pit-compose.yaml's healthcheck sends no Authorization header.
    When /health sat behind the token gate every curl came back 401 and the
    container was reported unhealthy for as long as it ran, while the service
    behind it was connected and serving. The other three pit services expose
    /health unauthenticated; this pins that session-control matches them.
    """

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
            api_key="operator-secret",
        )
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            code, body = await asyncio.to_thread(_request, f"{base}/health")
            assert code == 200
            assert body["status"] == "ok"

            # The gate is still shut on everything else.
            code, _ = await asyncio.to_thread(_request, f"{base}/session")
            assert code == 401
            code, _ = await asyncio.to_thread(_request, f"{base}/roster")
            assert code == 401
        finally:
            server.shutdown()
            server.server_close()

    asyncio.run(exercise())


def test_grafana_alerts_is_reachable_without_the_bearer_token(tmp_path: Path):
    """Grafana's provisioned contact point posts here with no credentials.

    Authenticating it would put the operator API key into a committed
    provisioning file, or hand it to the grafana container, which is given a
    deliberately minimal environment. The endpoint changes no state -- it
    logs a bounded summary and returns a count -- so it is left open like
    /health, and this pins that.
    """

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
            api_key="operator-secret",
        )
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            notification = {
                "status": "firing",
                "alerts": [
                    {"status": "firing", "labels": {"alertname": "Oil pressure low"}},
                    {"status": "resolved", "labels": {"alertname": "Coolant temperature high"}},
                ],
            }
            code, body = await asyncio.to_thread(_request, f"{base}/grafana-alerts", notification)
            assert code == 200
            assert body["received"] == 2

            # A payload that is not shaped like a notification is counted as
            # nothing rather than raising.
            code, body = await asyncio.to_thread(_request, f"{base}/grafana-alerts", {})
            assert code == 200
            assert body["received"] == 0

            # The gate is still shut on the routes that actually do something.
            code, _ = await asyncio.to_thread(_request, f"{base}/session/start", {})
            assert code == 401
        finally:
            server.shutdown()
            server.server_close()

    asyncio.run(exercise())


def test_grafana_alerts_caps_what_one_notification_can_write(tmp_path: Path):
    """An unauthenticated caller must not be able to flood the log."""

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
            api_key="operator-secret",
        )
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            flood = {
                "alerts": [
                    {"status": "firing", "labels": {"alertname": f"alert {index}"}}
                    for index in range(200)
                ]
            }
            code, body = await asyncio.to_thread(_request, f"{base}/grafana-alerts", flood)
            assert code == 200
            assert body["received"] == 20
        finally:
            server.shutdown()
            server.server_close()

    asyncio.run(exercise())


def test_http_requires_bearer_token_when_configured(tmp_path: Path):
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
            api_key="operator-secret",
        )
        base = f"http://127.0.0.1:{server.server_port}"
        body = json.dumps({"session_type": "race", "driver": "Driver A"}).encode()
        try:
            code, _ = await asyncio.to_thread(_request, f"{base}/session")
            assert code == 401

            unauthorized = urllib.request.Request(
                f"{base}/session/start",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            code, _ = await asyncio.to_thread(_url_request, unauthorized)
            assert code == 401

            authorized = urllib.request.Request(
                f"{base}/session/start",
                data=body,
                headers={
                    "Authorization": "Bearer operator-secret",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            code, response = await asyncio.to_thread(_url_request, authorized)
            assert code == 200
            assert response["status"] == "active"

            authorized_get = urllib.request.Request(
                f"{base}/session",
                headers={"Authorization": "Bearer operator-secret"},
                method="GET",
            )
            code, response = await asyncio.to_thread(_url_request, authorized_get)
            assert code == 200
            assert response["status"] == "active"
        finally:
            server.shutdown()
            server.server_close()

    asyncio.run(exercise())


def test_static_ui_is_served_outside_the_bearer_gate(tmp_path: Path):
    """The page is where the key gets entered, so it cannot sit behind it.

    Every API call the page makes is still gated: an unknown path with no
    credential is 401, proving the gate is intact around the allowlist.
    """

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
            api_key="operator-secret",
        )
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            code, content_type, body = await asyncio.to_thread(_get_asset, f"{base}/")
            assert code == 200
            assert content_type.startswith("text/html")
            assert b"Session control" in body

            for path, expected_type in (
                ("/app.js", "text/javascript"),
                ("/style.css", "text/css"),
            ):
                code, content_type, body = await asyncio.to_thread(_get_asset, base + path)
                assert code == 200
                assert content_type.startswith(expected_type)
                assert body

            code, _ = await asyncio.to_thread(_request, f"{base}/session")
            assert code == 401
        finally:
            server.shutdown()
            server.server_close()

    asyncio.run(exercise())


def test_static_serving_is_an_allowlist_not_a_directory(tmp_path: Path):
    """A traversal attempt is refused: nothing off the allowlist is a file.

    The handler never joins the request path to a directory, so these are
    dictionary misses (404), not filesystem lookups.
    """

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
            for path in (
                "/../pyproject.toml",
                "/static/../service.py",
                "/%2e%2e/etc/passwd",
                "/etc/passwd",
                "/index.html",
            ):
                code = await asyncio.to_thread(_raw_get_status, base, path)
                assert code == 404, path
        finally:
            server.shutdown()
            server.server_close()

    asyncio.run(exercise())


def test_same_origin_post_is_accepted_and_mismatched_origin_refused(tmp_path: Path):
    """Browsers send Origin on every non-GET request, same-origin included.

    The page this service serves must get through; any other origin — here a
    different port on the same host — keeps the existing 403. The
    absent-Origin path (curl, the compose healthcheck) is pinned by every
    other POST test in this file.
    """

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
        payload = json.dumps({"session_type": "race", "driver": "Driver A"}).encode()
        try:
            mismatched = urllib.request.Request(
                f"{base}/session/start",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Origin": f"http://127.0.0.1:{server.server_port + 1}",
                },
                method="POST",
            )
            code, body = await asyncio.to_thread(_url_request, mismatched)
            assert code == 403
            assert body == {"error": "cross-origin request denied"}
            assert publisher.published == 0

            same_origin = urllib.request.Request(
                f"{base}/session/start",
                data=payload,
                headers={"Content-Type": "application/json", "Origin": base},
                method="POST",
            )
            code, body = await asyncio.to_thread(_url_request, same_origin)
            assert code == 200
            assert body["status"] == "active"
            assert publisher.published == 1
        finally:
            server.shutdown()
            server.server_close()

    asyncio.run(exercise())


def test_backdated_transitions_over_http(tmp_path: Path):
    """`at` backdates a driver change or an end; everything invalid is 409."""

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
            code, body = await asyncio.to_thread(
                _request,
                f"{base}/session/start",
                {"session_type": "race", "driver": "Driver A", "at": 12345},
            )
            assert code == 409
            assert "start cannot be backdated" in body["error"]

            code, body = await asyncio.to_thread(
                _request,
                f"{base}/session/start",
                {"session_type": "race", "driver": "Driver A"},
            )
            assert code == 200
            session_start = body["session_start"]

            code, body = await asyncio.to_thread(
                _request,
                f"{base}/session/driver",
                {"driver": "Driver B", "at": "yesterday"},
            )
            assert code == 409
            assert "epoch milliseconds" in body["error"]

            code, body = await asyncio.to_thread(
                _request,
                f"{base}/session/driver",
                {"driver": "Driver B", "at": session_start + 3_600_000_000},
            )
            assert code == 409
            assert "future" in body["error"]

            code, body = await asyncio.to_thread(
                _request,
                f"{base}/session/driver",
                {"driver": "Driver B", "at": session_start - 60_000},
            )
            assert code == 409
            assert "before the active stint" in body["error"]

            code, body = await asyncio.to_thread(
                _request,
                f"{base}/session/driver",
                {"driver": "Driver B", "at": session_start + 1},
            )
            assert code == 200
            assert body["driver"] == "Driver B"
            assert body["stint_number"] == 2
            assert body["stint_start"] == session_start + 1

            # Re-stating the in-car driver with `at` corrects the boundary
            # just recorded; without `at` it stays the 409 it always was.
            code, body = await asyncio.to_thread(
                _request,
                f"{base}/session/driver",
                {"driver": "Driver B", "at": session_start + 5},
            )
            assert code == 200
            assert body["driver"] == "Driver B"
            assert body["stint_number"] == 2
            assert body["stint_start"] == session_start + 5

            code, body = await asyncio.to_thread(
                _request,
                f"{base}/session/driver",
                {"driver": "Driver B"},
            )
            assert code == 409
            assert "already the active driver" in body["error"]

            code, body = await asyncio.to_thread(
                _request,
                f"{base}/session/end",
                {"at": session_start},
            )
            assert code == 409
            assert "before the active stint" in body["error"]

            code, body = await asyncio.to_thread(
                _request,
                f"{base}/session/end",
                {"at": session_start + 6},
            )
            assert code == 200
            assert body["status"] == "ended"
            assert body["timestamp"] == session_start + 6
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
        return exc.code, json.loads(exc.read())
    with response:
        return response.status, json.loads(response.read())


def _url_request(request: urllib.request.Request) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(request, timeout=3.0) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _get_asset(url: str) -> tuple[int, str, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=3.0) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def _raw_get_status(base: str, path: str) -> int:
    """GET an exact, unnormalised path — urllib would clean `..` client-side."""
    parsed = urlsplit(base)
    assert parsed.hostname is not None
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3.0)
    try:
        connection.putrequest("GET", path, skip_host=True)
        connection.putheader("Host", f"{parsed.hostname}:{parsed.port}")
        connection.endheaders()
        return connection.getresponse().status
    finally:
        connection.close()


def _length_only_request(url: str, length: int) -> tuple[int, dict]:
    parsed = urlsplit(url)
    assert parsed.hostname is not None
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3.0)
    try:
        connection.putrequest("POST", parsed.path)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(length))
        connection.putheader("Connection", "close")
        connection.endheaders()
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()
