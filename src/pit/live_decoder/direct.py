"""The pit's end of the live link: ask the car for frames, hand them on.

`DirectLink` says HELLO to every address the car might answer on, about
once a second, naming the channels the live view selects and how often it
wants each. The car replies to wherever the HELLO came from with small
frames of whatever changed (`agent.live`, `core.live_wire`). Each frame is
an ordinary ``SampleBatch``, so the service decodes it exactly as it
decodes a batch from the stream.

`DirectSource` is what the service's consume loop reads in ``direct`` mode.
It prefers frames. It also keeps a consumer on the pit's own sourced copy,
for two reasons: registries only ever arrive that way, and a car that does
not answer HELLOs at all (an agent from before the live link, a blocked
port) should still drive the gauges the old way rather than leave them
blank. The copy's batches are used only after frames have been absent for
``fallback_after_s``, and dropped the moment frames return.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import struct
import time
from dataclasses import dataclass

from core import live_wire
from core.live_wire import Selection, WireError
from core.pb import telemetry_pb2 as pb
from pit.live_decoder.config import LiveConfig
from pit.live_decoder.health import HealthState
from pit.registry_cache import MSG_TYPE_HEADER, MSG_TYPE_REGISTRY

logger = logging.getLogger(__name__)

HELLO_INTERVAL_S = 1.0
_RESOLVE_INTERVAL_S = 30.0
_QUEUE_SIZE = 256
# With no resend configured a lost frame would never heal; repeat anyway,
# just rarely.
_REFRESH_FLOOR_MS = 5_000
_MAX_INTERVAL_MS = 60_000


def selection_for(config: LiveConfig, registry: pb.ChannelRegistry) -> Selection:
    """The live view's rules, resolved against one registry generation."""
    intervals: dict[int, int] = {}
    for channel in registry.channels:
        max_hz = config.max_hz_for(channel.name)
        if max_hz is None:
            continue
        intervals[channel.id] = 0 if max_hz == 0 else min(_MAX_INTERVAL_MS, round(1000 / max_hz))
    resend_ms = round(config.defaults.resend_s * 1000)
    return Selection(intervals, resend_ms if resend_ms > 0 else _REFRESH_FLOOR_MS)


def parse_car_addrs(text: str) -> tuple[tuple[str, int], ...]:
    """``host[:port],host[:port]`` -> address pairs; the port defaults to 7423."""
    addrs: list[tuple[str, int]] = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        host, sep, port_text = item.rpartition(":")
        if not sep:
            host, port_text = item, str(live_wire.DEFAULT_PORT)
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ValueError(f"bad live car address {item!r}") from exc
        if not host or not 1 <= port <= 65535:
            raise ValueError(f"bad live car address {item!r}")
        addrs.append((host, port))
    return tuple(addrs)


@dataclass(frozen=True, slots=True)
class LiveMessage:
    """Shaped like a NATS message, which is all the consume loop looks at."""

    data: bytes
    headers: dict[str, str] | None = None


class DirectLink(asyncio.DatagramProtocol):
    """One UDP socket: HELLOs out to the car, frames back in."""

    def __init__(
        self,
        car_addrs: tuple[tuple[str, int], ...],
        secret: str | None,
        health: HealthState,
    ) -> None:
        self._car_addrs = car_addrs
        self._key = live_wire.derive_key(secret)
        self._health = health
        self._transport: asyncio.DatagramTransport | None = None
        self._session = struct.unpack("!Q", os.urandom(8))[0]
        self._counter = 0
        self._registry_seq: int | None = None
        self._selection: Selection | None = None
        self._resolved: list[tuple[str, int]] = []
        self._resolved_at = float("-inf")
        self._boot: int | None = None
        self._seq = 0
        self.frames: asyncio.Queue[LiveMessage] = asyncio.Queue(_QUEUE_SIZE)
        self.last_frame_mono: float | None = None
        if self._key is None:
            logger.warning(
                "live-decoder: OPENLAPS_LIVE_KEY is not set; live frames are accepted unsigned"
            )

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(
            lambda: self, family=socket.AF_INET, local_addr=("0.0.0.0", 0)
        )

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def error_received(self, exc: Exception) -> None:
        # ICMP unreachable from a path that is down; the next HELLO retries.
        logger.debug("live-decoder: live link socket error: %s", exc)

    def select(self, registry_seq: int, selection: Selection) -> None:
        """What to ask the car for from the next HELLO on."""
        if registry_seq != self._registry_seq or selection != self._selection:
            logger.info(
                "live-decoder: asking the car for %d live channels of registry %d",
                len(selection.intervals),
                registry_seq,
            )
        self._registry_seq = registry_seq
        self._selection = selection

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            boot, seq, batch = live_wire.decode_frame(data, self._key)
        except WireError:
            self._health.live_frame_rejects += 1
            return
        if boot != self._boot:
            # The agent restarted: its frame counter started again.
            self._boot = boot
            self._seq = 0
        if seq <= self._seq:
            # The same frame by a second path, or one overtaken on the way;
            # either way a newer value has already been shown.
            self._health.live_frame_duplicates += 1
            return
        self._seq = seq
        self._health.live_frames += 1
        self.last_frame_mono = time.monotonic()
        if self.frames.full():
            self.frames.get_nowait()
        self.frames.put_nowait(LiveMessage(batch))

    async def run(self, stop: asyncio.Event) -> None:
        """Say HELLO until stopped."""
        while not stop.is_set():
            await self._hello()
            try:
                await asyncio.wait_for(stop.wait(), timeout=HELLO_INTERVAL_S)
            except TimeoutError:
                pass

    async def _hello(self) -> None:
        if self._transport is None or self._selection is None or self._registry_seq is None:
            return
        now = time.monotonic()
        if now - self._resolved_at >= _RESOLVE_INTERVAL_S:
            self._resolved_at = now
            self._resolved = await self._resolve()
            self._health.live_car_addrs = [f"{host}:{port}" for host, port in self._resolved]
        self._counter += 1
        try:
            datagrams = live_wire.encode_hello(
                session=self._session,
                counter=self._counter,
                registry_seq=self._registry_seq,
                selection=self._selection,
                key=self._key,
            )
        except WireError as exc:
            logger.error("live-decoder: %s", exc)
            return
        for addr in self._resolved:
            for datagram in datagrams:
                self._transport.sendto(datagram, addr)

    async def _resolve(self) -> list[tuple[str, int]]:
        loop = asyncio.get_running_loop()
        resolved: list[tuple[str, int]] = []
        for host, port in self._car_addrs:
            try:
                infos = await loop.getaddrinfo(
                    host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM
                )
            except OSError as exc:
                logger.warning("live-decoder: cannot resolve live car address %s: %s", host, exc)
                continue
            addr = infos[0][4][:2]
            if addr not in resolved:
                resolved.append(addr)
        return resolved


class DirectSource:
    """Frames first; the pit's sourced copy when the car sends none."""

    def __init__(
        self,
        link: DirectLink,
        fallback: object,
        health: HealthState,
        *,
        fallback_after_s: float,
    ) -> None:
        self._link = link
        self._fallback = fallback
        self._health = health
        self._fallback_after_s = fallback_after_s
        self._started = time.monotonic()
        self._tasks: list[asyncio.Task[None]] = []

    def start(self, stop: asyncio.Event) -> None:
        self._tasks = [
            asyncio.create_task(self._link.run(stop)),
            asyncio.create_task(self._pump(stop)),
        ]

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._link.close()

    def _in_fallback(self, now: float) -> bool:
        last = self._link.last_frame_mono
        return now - (self._started if last is None else last) >= self._fallback_after_s

    async def _pump(self, stop: asyncio.Event) -> None:
        """Feed registries always, and the copy's batches only in fallback."""
        while not stop.is_set():
            try:
                message = await self._fallback.next_msg(timeout=1.0)
            except TimeoutError:
                message = None
            except Exception as exc:  # noqa: BLE001 - ordered consumer repairs itself
                logger.warning("live-decoder: NATS consume error: %s", exc)
                await asyncio.sleep(0.5)
                continue
            fallback = self._in_fallback(time.monotonic())
            if fallback != self._health.live_fallback:
                self._health.live_fallback = fallback
                if fallback:
                    logger.warning(
                        "live-decoder: no live frame from the car for %.0fs; "
                        "showing the pit's sourced copy instead",
                        self._fallback_after_s,
                    )
                else:
                    logger.info("live-decoder: live frames are arriving from the car")
            if message is None:
                continue
            kind = (message.headers or {}).get(MSG_TYPE_HEADER, "batch")
            if kind != MSG_TYPE_REGISTRY and not fallback:
                continue
            frames = self._link.frames
            if frames.full():
                frames.get_nowait()
            frames.put_nowait(LiveMessage(message.data, dict(message.headers or {})))

    async def next_msg(self, timeout: float) -> LiveMessage:  # noqa: ASYNC109 - mirrors nats-py
        return await asyncio.wait_for(self._link.frames.get(), timeout=timeout)
