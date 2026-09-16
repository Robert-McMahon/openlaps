"""Where documents come from: a capture file, or the Natsoft TCP feed.

One interface -- ``async for document in source`` -- so the service, the
schema and every test are the same whichever is behind it. Replay is built
first (P7.10 source 1) because everything downstream needs data before any
live source works; the Natsoft client (source 2) is the same code for the
public feed and the timekeepers' local one, which differ only in address
(source 3).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pit.timing_feed.capture import read_capture
from pit.timing_feed.framing import PacketError, PacketFramer
from pit.timing_feed.health import HealthState

logger = logging.getLogger(__name__)

DEFAULT_HOST = "natsoft.com.au"
DEFAULT_PORT = 8889
_READ_CHUNK = 65536


@dataclass(frozen=True, slots=True)
class Document:
    """One raw document and when it arrived."""

    at: datetime
    text: str


class ReplaySource:
    """Documents from a capture file, at recorded pace or as fast as they read."""

    kind = "replay"

    def __init__(self, path: str | Path, *, paced: bool = True, speed: float = 1.0) -> None:
        self.path = Path(path)
        self.paced = paced
        self.speed = speed if speed > 0 else 1.0

    async def __aiter__(self) -> AsyncIterator[Document]:
        previous: datetime | None = None
        for at, text in read_capture(self.path):
            if self.paced and previous is not None:
                gap = (at - previous).total_seconds() / self.speed
                if gap > 0:
                    await asyncio.sleep(min(gap, 60.0))
            previous = at
            yield Document(at, text)


class NatsoftSource:
    """The feed client: frame, decode, yield; reconnect with backoff on anything."""

    kind = "natsoft"

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        health: HealthState | None = None,
        connect_timeout_s: float = 10.0,
        idle_timeout_s: float = 120.0,
        backoff_s: tuple[float, float] = (1.0, 30.0),
        stop: asyncio.Event | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.health = health or HealthState()
        self.connect_timeout_s = connect_timeout_s
        # The feed heartbeats every few seconds; silence this long is a dead
        # socket nobody told us about, and TCP will not tell us for minutes.
        self.idle_timeout_s = idle_timeout_s
        self.backoff_s = backoff_s
        self.stop = stop or asyncio.Event()
        self.connections = 0

    async def __aiter__(self) -> AsyncIterator[Document]:
        delay = self.backoff_s[0]
        while not self.stop.is_set():
            try:
                async for document in self._connection():
                    delay = self.backoff_s[0]
                    yield document
            except asyncio.CancelledError:
                raise
            except (OSError, PacketError, TimeoutError, EOFError) as exc:
                self.health.source_connected = False
                logger.warning(
                    "natsoft: %s:%d: %s -- reconnecting in %.0fs", self.host, self.port, exc, delay
                )
            if self.stop.is_set():
                break
            self.health.source_reconnects += 1
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=delay)
            except TimeoutError:
                pass
            delay = min(delay * 2, self.backoff_s[1])

    async def _connection(self) -> AsyncIterator[Document]:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=self.connect_timeout_s
        )
        self.connections += 1
        self.health.source_connected = True
        logger.info("natsoft: connected to %s:%d", self.host, self.port)
        framer = PacketFramer()
        try:
            while not self.stop.is_set():
                chunk = await asyncio.wait_for(
                    reader.read(_READ_CHUNK), timeout=self.idle_timeout_s
                )
                if not chunk:
                    raise EOFError("feed closed the connection")
                at = datetime.now(UTC)
                for text in framer.feed(chunk):
                    yield Document(at, text)
        finally:
            self.health.source_connected = False
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
