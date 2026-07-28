"""ntrip-client unit tests: GGA synthesis, caster handshake, settings.

The handshake tests run a scripted fake caster over a real TCP loopback
socket (`asyncio.start_server`), asserting the request line, the
`Ntrip-Version` header, and the basic-auth encoding byte-for-byte, plus both
accepted status lines and the 401 fatal-error path. No docker required —
these do not touch NATS.
"""

from __future__ import annotations

import asyncio
import base64
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest

from pit.ntrip_client.gga import build_gga
from pit.ntrip_client.health import HealthState, serve_health
from pit.ntrip_client.ntrip import NtripAuthError, NtripCasterError, connect
from pit.ntrip_client.service import NtripSettings

VEHICLE = "example-club-racer"


# --- GGA synthesis ------------------------------------------------------------


def test_build_gga_encodes_known_position_with_correct_checksum():
    when = datetime(2026, 1, 1, 12, 34, 56, 780_000, tzinfo=UTC)
    sentence = build_gga(-31.8, 115.8, when=when)

    assert sentence.startswith(b"$GPGGA,123456.78,3148.0000,S,11548.0000,E,")
    assert sentence.endswith(b"\r\n")
    body, checksum = sentence[1:-2].split(b"*")
    expected = 0
    for byte in body:
        expected ^= byte
    assert checksum == f"{expected:02X}".encode()


def test_build_gga_handles_all_four_hemispheres():
    north_east = build_gga(1.5, 1.5)
    south_west = build_gga(-1.5, -1.5)
    assert b",N," in north_east and b",E," in north_east
    assert b",S," in south_west and b",W," in south_west


# --- NTRIP handshake -----------------------------------------------------------


class _FakeCaster:
    """A scripted NTRIP caster on a real loopback socket."""

    def __init__(self, status_line: bytes, *, body: list[bytes] | None = None) -> None:
        self.status_line = status_line
        self.body = body or []
        self.received: bytes = b""
        self.received_after: bytes = b""
        self._server: asyncio.base_events.Server | None = None
        self.port = 0

    async def __aenter__(self) -> _FakeCaster:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.received = await reader.readuntil(b"\r\n\r\n")
        writer.write(self.status_line + b"\r\n\r\n")
        for chunk in self.body:
            writer.write(chunk)
        await writer.drain()
        # Keep reading so anything the client writes back (a GGA sentence)
        # is captured; ends when the client closes its side.
        try:
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                if not chunk:
                    break
                self.received_after += chunk
        except TimeoutError:
            pass


def test_connect_sends_the_request_line_version_header_and_basic_auth():
    async def scenario():
        async with _FakeCaster(b"HTTP/1.1 200 OK") as caster:
            connection = await connect(
                "127.0.0.1", caster.port, "MOUNT1", "alice", "s3cret", connect_timeout_s=5.0
            )
            await connection.close()
        return caster.received

    received = asyncio.run(scenario())
    lines = received.split(b"\r\n")
    assert lines[0] == b"GET /MOUNT1 HTTP/1.1"
    assert b"Ntrip-Version: Ntrip/2.0" in lines
    expected_auth = base64.b64encode(b"alice:s3cret").decode("ascii")
    assert f"Authorization: Basic {expected_auth}".encode() in lines


@pytest.mark.parametrize("status_line", [b"HTTP/1.1 200 OK", b"ICY 200 OK"])
def test_connect_accepts_both_v2_and_v1_status_lines(status_line):
    async def scenario():
        async with _FakeCaster(status_line, body=[b"\xd3\x00\x03rtcm-frame"]) as caster:
            connection = await connect(
                "127.0.0.1", caster.port, "MOUNT1", "alice", "s3cret", connect_timeout_s=5.0
            )
            chunk = await connection.read_chunk(timeout_s=5.0)
            await connection.close()
            return chunk

    assert asyncio.run(scenario()) == b"\xd3\x00\x03rtcm-frame"


def test_connect_raises_auth_error_on_401_without_reconnect_loop():
    async def scenario():
        async with _FakeCaster(b"HTTP/1.1 401 Unauthorized") as caster:
            with pytest.raises(NtripAuthError):
                await connect(
                    "127.0.0.1", caster.port, "MOUNT1", "alice", "wrong", connect_timeout_s=5.0
                )

    asyncio.run(scenario())


def test_connect_raises_caster_error_on_other_non_200():
    async def scenario():
        async with _FakeCaster(b"HTTP/1.1 404 Not Found") as caster:
            with pytest.raises(NtripCasterError):
                await connect(
                    "127.0.0.1",
                    caster.port,
                    "NOSUCHMOUNT",
                    "alice",
                    "s3cret",
                    connect_timeout_s=5.0,
                )

    asyncio.run(scenario())


def test_read_chunk_returns_empty_on_idle_timeout():
    async def scenario():
        async with _FakeCaster(b"HTTP/1.1 200 OK") as caster:
            connection = await connect(
                "127.0.0.1", caster.port, "MOUNT1", "alice", "s3cret", connect_timeout_s=5.0
            )
            chunk = await connection.read_chunk(timeout_s=0.2)
            await connection.close()
            return chunk

    assert asyncio.run(scenario()) == b""


def test_write_gga_reaches_the_caster_verbatim():
    sentence = build_gga(-31.8, 115.8)

    async def scenario():
        async with _FakeCaster(b"HTTP/1.1 200 OK") as caster:
            connection = await connect(
                "127.0.0.1", caster.port, "MOUNT1", "alice", "s3cret", connect_timeout_s=5.0
            )
            await connection.write_gga(sentence)
            deadline = asyncio.get_event_loop().time() + 2.0
            while caster.received_after != sentence and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.01)
            await connection.close()
            return caster.received_after

    assert asyncio.run(scenario()) == sentence


# --- settings -------------------------------------------------------------------


def test_settings_from_env_reads_the_documented_variables():
    settings = NtripSettings.from_env(
        {
            "OPENLAPS_VEHICLE_ID": VEHICLE,
            "OPENLAPS_NATS_URL": "nats://pit:4222",
            "NTRIP_HOST": "caster.example.com",
            "NTRIP_PORT": "2102",
            "NTRIP_MOUNTPOINT": "VRS1",
            "NTRIP_USER": "alice",
            "NTRIP_PASSWORD": "s3cret",
            "NTRIP_ENABLE_GGA": "1",
            "NTRIP_GGA_INTERVAL_S": "5",
        }
    )
    assert settings.vehicle_id == VEHICLE
    assert settings.host == "caster.example.com"
    assert settings.port == 2102
    assert settings.mountpoint == "VRS1"
    assert settings.enable_gga is True
    assert settings.gga_interval_s == 5.0
    assert settings.health_port == 8083


def test_settings_default_port_and_gga_disabled():
    settings = NtripSettings.from_env(
        {
            "OPENLAPS_VEHICLE_ID": VEHICLE,
            "NTRIP_HOST": "caster.example.com",
            "NTRIP_MOUNTPOINT": "MOUNT1",
        }
    )
    assert settings.port == 2101
    assert settings.enable_gga is False


def test_settings_require_a_vehicle_id():
    with pytest.raises(ValueError, match="OPENLAPS_VEHICLE_ID"):
        NtripSettings.from_env({"NTRIP_HOST": "x", "NTRIP_MOUNTPOINT": "y"})


def test_settings_require_a_host():
    with pytest.raises(ValueError, match="NTRIP_HOST"):
        NtripSettings.from_env({"OPENLAPS_VEHICLE_ID": VEHICLE, "NTRIP_MOUNTPOINT": "y"})


def test_settings_require_a_mountpoint():
    with pytest.raises(ValueError, match="NTRIP_MOUNTPOINT"):
        NtripSettings.from_env({"OPENLAPS_VEHICLE_ID": VEHICLE, "NTRIP_HOST": "x"})


def test_settings_never_carry_the_password_in_repr():
    settings = NtripSettings.from_env(
        {
            "OPENLAPS_VEHICLE_ID": VEHICLE,
            "NTRIP_HOST": "caster.example.com",
            "NTRIP_MOUNTPOINT": "MOUNT1",
            "NTRIP_PASSWORD": "s3cret-value",
        }
    )
    # The password is a real field (the handshake needs it) -- this just
    # guards against ever routing settings.password through logging/health.
    assert settings.password == "s3cret-value"


# --- health ---------------------------------------------------------------------


def test_health_endpoint_serves_the_snapshot_and_never_the_password():
    state = HealthState()
    state.observe_chunk(128)
    state.roll()
    server = serve_health(state, 0, host="127.0.0.1")
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(f"{base}/health", timeout=5) as response:
            payload = json.loads(response.read())
        assert payload["bytes_from_caster"] == 128
        assert "password" not in json.dumps(payload).lower()
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"{base}/", timeout=5)
    finally:
        server.shutdown()
