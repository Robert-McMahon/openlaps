"""NTRIP v1/v2 caster handshake and byte stream over stdlib asyncio.

NTRIP v1 casters answer ``ICY 200 OK`` and are otherwise not HTTP-conformant
(no persistent-connection semantics, no chunked transfer), so this speaks
just enough of the request/response shape by hand rather than pulling in an
HTTP client library that assumes RFC-compliant responses
(`docs/plan/PHASE3.md` P3.5). No new dependency: `asyncio.open_connection`
is all a caster handshake plus a raw byte stream needs.
"""

from __future__ import annotations

import asyncio
import base64
import re

_STATUS_LINE_RE = re.compile(rb"^(?:ICY|HTTP/\d\.\d|SOURCETABLE)\s+(\d+)")
_READ_CHUNK_BYTES = 4096


class NtripError(Exception):
    """Base class for caster handshake failures."""


class NtripAuthError(NtripError):
    """The caster rejected the credentials (401). Retrying won't fix this."""


class NtripCasterError(NtripError):
    """Any other non-200 response, or a status line neither v1 nor v2 casters send."""


class NtripConnection:
    """One open, authenticated connection to an NTRIP caster."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    async def read_chunk(self, timeout_s: float) -> bytes:
        """One read of correction bytes, or ``b""`` on idle timeout or EOF."""
        try:
            return await asyncio.wait_for(self._reader.read(_READ_CHUNK_BYTES), timeout=timeout_s)
        except TimeoutError:
            return b""

    async def write_gga(self, sentence: bytes) -> None:
        """Send a synthesised ``$GPGGA`` sentence upstream to the caster."""
        self._writer.write(sentence)
        await self._writer.drain()

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def connect(
    host: str,
    port: int,
    mountpoint: str,
    username: str,
    password: str,
    *,
    user_agent: str = "NTRIP openlaps/0.1",
    connect_timeout_s: float = 10.0,
) -> NtripConnection:
    """Open a caster connection and complete the NTRIP handshake.

    Sends ``GET /<mountpoint>`` with ``Ntrip-Version: Ntrip/2.0`` and basic
    auth, and accepts both the v2 ``HTTP/1.1 200 OK`` and the v1 ``ICY 200
    OK`` status lines. Raises `NtripAuthError` on a 401 — wrong credentials
    won't fix themselves on retry — and `NtripCasterError` on any other
    non-200 response or an unrecognised status line.
    """
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=connect_timeout_s
    )
    try:
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        request = (
            f"GET /{mountpoint} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Ntrip-Version: Ntrip/2.0\r\n"
            f"User-Agent: {user_agent}\r\n"
            f"Authorization: Basic {credentials}\r\n"
            "\r\n"
        )
        writer.write(request.encode("ascii"))
        await writer.drain()

        status_line = await asyncio.wait_for(reader.readline(), timeout=connect_timeout_s)
        match = _STATUS_LINE_RE.match(status_line)
        if not match:
            raise NtripCasterError(f"unrecognised caster response: {status_line!r}")
        code = int(match.group(1))
        while True:
            header = await asyncio.wait_for(reader.readline(), timeout=connect_timeout_s)
            if header in (b"\r\n", b"\n", b""):
                break
        if code == 401:
            raise NtripAuthError(f"caster rejected credentials for mountpoint {mountpoint!r}")
        if code != 200:
            raise NtripCasterError(f"caster returned status {code} for mountpoint {mountpoint!r}")
    except BaseException:
        writer.close()
        raise
    return NtripConnection(reader, writer)
