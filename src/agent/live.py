"""The car's end of the live link: newest values, by datagram, to whoever asks.

Every batch the agent hands its JetStream publisher is also shown to a
`LiveSampler`, which keeps only the newest value of each channel. A few
times a second it packs the selected channels whose value has changed since
it last sent them into one small ``SampleBatch`` and the `LiveSender` posts
that to the pit as a datagram (`core.live_wire`).

Nothing here is queued and nothing is retried. A frame that is lost is
simply followed by the next one, and a value that has not changed is
repeated only once per refresh period, so a steady coolant temperature
costs one sample a second instead of ten. That is the whole point: when the
radio carries less than the car produces, the gauges keep showing *now*
while the ordered, gap-free stream falls behind and catches up later.

The sampler reads batches *after* the pipeline's report-by-exception
filter, so "changed" means what the catalog already says it means for each
channel (its deadband), and one decode of the serialized batch serves the
Python and the compiled ingest paths alike.

The car is told what to send, and where, by the pit's HELLO datagrams. It
streams to the address a valid HELLO last came from and stops a few seconds
after they stop, so it never sprays a network that is not listening.
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from google.protobuf.message import DecodeError

from core import live_wire
from core.live_wire import Hello, Selection, WireError
from core.pb import telemetry_pb2 as pb

logger = logging.getLogger(__name__)

DEFAULT_TICK_MS = 100
# A subscriber that has not said HELLO for this long is gone.
HELLO_TIMEOUT_S = 5.0
_MAX_SUBSCRIBER_ADDRS = 4
_MAX_PENDING_BATCHES = 4_096
# One sample's worst case on the wire: submessage framing, a 5-byte channel
# id, a 5-byte offset and a double.
_SAMPLE_BOUND = 24
_BATCH_HEADER_BOUND = 32
_MAX_OFFSET_US = 0xFFFF_FFFF
_STATS_INTERVAL_S = 60.0


@dataclass(slots=True)
class _Latest:
    capture_us: int
    kind: str
    value: object


@dataclass(slots=True)
class _Sent:
    key: object
    at_ms: float


def _key(kind: str, value: object) -> object:
    # NaN never equals itself, which would make a channel stuck at NaN look
    # changed in every frame.
    if kind in ("d", "f") and value != value:  # noqa: PLR0124 - NaN test
        return (kind, "nan")
    return (kind, value)


class LiveSampler:
    """Newest value per channel, and which of them are due to be sent.

    Pure and single-threaded: the caller feeds it serialized batches and a
    clock, and takes serialized frames out.
    """

    def __init__(self, registry_seq: int, *, max_batch_bytes: int = 1_100) -> None:
        self.registry_seq = registry_seq
        self._max_batch_bytes = max_batch_bytes
        self._latest: dict[int, _Latest] = {}
        self._sent: dict[int, _Sent] = {}
        self._selection = Selection()
        self.malformed_batches = 0
        self.samples_sent = 0

    @property
    def selection(self) -> Selection:
        return self._selection

    def select(self, selection: Selection) -> None:
        """Adopt the pit's selection; everything in it is due at once."""
        self._selection = selection
        self._sent.clear()

    def forget_sent(self) -> None:
        """Treat every channel as unsent: a listener has just (re)appeared."""
        self._sent.clear()

    def observe(self, payload: bytes) -> None:
        """Note the values in one serialized SampleBatch of this registry."""
        batch = pb.SampleBatch()
        try:
            batch.ParseFromString(payload)
        except DecodeError:
            self.malformed_batches += 1
            return
        if batch.registry_seq != self.registry_seq:
            return
        base_us = batch.batch_epoch_unix_ms * 1000
        latest = self._latest
        for sample in batch.samples:
            kind = sample.WhichOneof("value")
            if kind is None:
                continue
            capture_us = base_us + sample.t_offset_us
            held = latest.get(sample.channel_id)
            if held is None:
                latest[sample.channel_id] = _Latest(capture_us, kind, getattr(sample, kind))
            elif capture_us >= held.capture_us:
                held.capture_us = capture_us
                held.kind = kind
                held.value = getattr(sample, kind)

    def frames(self, now_ms: float) -> list[bytes]:
        """Serialized SampleBatches for everything due at ``now_ms``.

        A selected channel is due when its value differs from the one last
        sent and its interval has passed, or when it has gone a whole
        refresh period unsent. Usually that is one batch; a first send, or a
        selection wider than one datagram, makes several.
        """
        selection = self._selection
        refresh_ms = selection.refresh_ms
        due: list[tuple[int, _Latest]] = []
        for channel_id, interval_ms in selection.intervals.items():
            held = self._latest.get(channel_id)
            if held is None:
                continue
            sent = self._sent.get(channel_id)
            key = _key(held.kind, held.value)
            if sent is None:
                # Spread the first sends' refresh phases, or every quiet
                # channel would come due in the same frame once a period.
                phase = (channel_id * 2654435761 % 2**32) % refresh_ms if refresh_ms else 0
                self._sent[channel_id] = _Sent(key, now_ms - phase)
            else:
                elapsed = now_ms - sent.at_ms
                changed = key != sent.key and elapsed >= interval_ms
                if not changed and not (refresh_ms and elapsed >= refresh_ms):
                    continue
                sent.key = key
                sent.at_ms = now_ms
            due.append((channel_id, held))
        if not due:
            return []
        due.sort(key=lambda item: item[1].capture_us)
        self.samples_sent += len(due)
        return self._pack(due)

    def _pack(self, due: list[tuple[int, _Latest]]) -> list[bytes]:
        batches: list[bytes] = []
        batch: pb.SampleBatch | None = None
        epoch_us = 0
        size = 0
        for channel_id, held in due:
            cost = _SAMPLE_BOUND + (len(held.value) if held.kind == "s" else 0)
            if (
                batch is None
                or size + cost > self._max_batch_bytes
                or held.capture_us - epoch_us > _MAX_OFFSET_US
            ):
                if batch is not None:
                    batches.append(batch.SerializeToString())
                epoch_ms = held.capture_us // 1000
                epoch_us = epoch_ms * 1000
                batch = pb.SampleBatch(
                    registry_seq=self.registry_seq,
                    batch_epoch_unix_ms=epoch_ms,
                    format_version=1,
                )
                size = _BATCH_HEADER_BOUND
            sample = batch.samples.add(
                channel_id=channel_id, t_offset_us=held.capture_us - epoch_us
            )
            setattr(sample, held.kind, held.value)
            size += cost
        if batch is not None:
            batches.append(batch.SerializeToString())
        return batches


class Subscribers:
    """Who asked for live frames, and what they asked for.

    One pit at a time: the session that is being served keeps the link until
    it has been silent for `HELLO_TIMEOUT_S`, so a second listener cannot
    flap the selection. Its HELLOs may arrive by more than one path (the
    tunnel and the plain radio network, say); every address that is still
    saying HELLO gets every frame, which makes a path change cost nothing.
    """

    def __init__(self, registry_seq: int) -> None:
        self._registry_seq = registry_seq
        self._session: int | None = None
        self._counter = -1
        self._addrs: dict[tuple[str, int], float] = {}
        self._parts: dict[int, dict[int, int]] = {}
        self._parts_version: int | None = None
        self._applied_version: int | None = None
        self.rejected = 0
        self.registry_mismatches = 0

    def hello(self, hello: Hello, addr: tuple[str, int], now: float) -> Selection | None:
        """Note one authenticated HELLO; return a selection when it changes."""
        self.expire(now)
        if self._session is not None and hello.session != self._session:
            self.rejected += 1
            return None
        if hello.session == self._session and hello.counter < self._counter:
            self.rejected += 1
            return None
        if hello.registry_seq != self._registry_seq:
            # The pit has not seen this car's catalog generation yet (it
            # arrives over the stream); its channel ids would mean nothing.
            self.registry_mismatches += 1
            return None
        if self._session is None:
            self._session = hello.session
            self._applied_version = None
            logger.info("live: serving pit session %016x at %s:%d", hello.session, *addr)
        self._counter = hello.counter
        if addr not in self._addrs and len(self._addrs) >= _MAX_SUBSCRIBER_ADDRS:
            self.rejected += 1
            return None
        self._addrs[addr] = now

        if hello.selection_version == self._applied_version:
            return None
        if hello.selection_version != self._parts_version:
            self._parts_version = hello.selection_version
            self._parts = {}
        self._parts[hello.part] = hello.intervals
        if len(self._parts) < hello.parts:
            return None
        intervals: dict[int, int] = {}
        for part in self._parts.values():
            intervals.update(part)
        self._applied_version = hello.selection_version
        self._parts = {}
        return Selection(intervals, hello.refresh_ms)

    def expire(self, now: float) -> None:
        for addr in [a for a, seen in self._addrs.items() if now - seen > HELLO_TIMEOUT_S]:
            del self._addrs[addr]
        if not self._addrs and self._session is not None:
            logger.info("live: pit session %016x went quiet", self._session)
            self._session = None
            self._counter = -1

    @property
    def addrs(self) -> list[tuple[str, int]]:
        return list(self._addrs)


class LiveSender:
    """Run the sampler and the datagram socket on a thread of their own."""

    def __init__(
        self,
        *,
        registry_seq: int,
        port: int,
        secret: str | None = None,
        tick_ms: int = DEFAULT_TICK_MS,
        bind: str = "0.0.0.0",  # noqa: S104 - the pit reaches the car by any of its addresses
    ) -> None:
        if not 10 <= tick_ms <= 1000:
            raise ValueError("live tick must be between 10 and 1000 ms")
        self._key = live_wire.derive_key(secret)
        self._tick_s = tick_ms / 1000.0
        self._bind = (bind, port)
        self._sampler = LiveSampler(
            registry_seq,
            max_batch_bytes=live_wire.MAX_DATAGRAM - live_wire.frame_overhead(self._key),
        )
        self._subscribers = Subscribers(registry_seq)
        self._pending: deque[bytes] = deque(maxlen=_MAX_PENDING_BATCHES)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        # Tells the pit one agent process from the next, so its duplicate
        # filter starts again when the frame counter does.
        self._boot = struct.unpack("!I", os.urandom(4))[0]
        self._seq = 0
        self.frames_sent = 0
        self.bytes_sent = 0
        self.bad_datagrams = 0
        self.send_errors = 0
        if self._key is None:
            logger.warning(
                "live: OPENLAPS_LIVE_KEY is not set; live frames are unsigned and "
                "anyone who can reach UDP port %d can ask for them",
                port,
            )

    @property
    def port(self) -> int:
        """The bound port (useful when constructed with port 0)."""
        return self._sock.getsockname()[1] if self._sock is not None else self._bind[1]

    def submit_many(self, payloads: Iterable[bytes]) -> None:
        """Hand over serialized batches; never blocks, oldest dropped if stalled."""
        self._pending.extend(payloads)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(self._bind)
        sock.setblocking(False)
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="live", daemon=True)
        self._thread.start()
        logger.info("live: listening for pit HELLOs on UDP %s:%d", self._bind[0], self.port)

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _run(self) -> None:
        next_tick = time.monotonic() + self._tick_s
        next_stats = time.monotonic() + _STATS_INTERVAL_S
        while not self._stop.wait(max(0.0, next_tick - time.monotonic())):
            now = time.monotonic()
            next_tick = max(next_tick + self._tick_s, now)
            try:
                self.step(now)
            except Exception:  # noqa: BLE001 - gauges must never take the agent down
                logger.exception("live: tick failed")
            if now >= next_stats:
                next_stats = now + _STATS_INTERVAL_S
                logger.info(
                    "live: %d frames %d bytes sent, %d listeners, %d bad datagrams",
                    self.frames_sent,
                    self.bytes_sent,
                    len(self._subscribers.addrs),
                    self.bad_datagrams,
                )

    def step(self, now: float) -> None:
        """One tick: read HELLOs, fold in new batches, send what is due."""
        self._read_hellos(now)
        pending = self._pending
        while pending:
            self._sampler.observe(pending.popleft())
        self._subscribers.expire(now)
        addrs = self._subscribers.addrs
        if not addrs or self._sock is None:
            return
        for batch in self._sampler.frames(now * 1000.0):
            self._seq += 1
            datagram = live_wire.encode_frame(self._boot, self._seq, batch, self._key)
            for addr in addrs:
                try:
                    self._sock.sendto(datagram, addr)
                except OSError:
                    # A full socket buffer or an unreachable path: this frame
                    # is lost on that path and the next one supersedes it.
                    self.send_errors += 1
                    continue
                self.bytes_sent += len(datagram)
            self.frames_sent += 1

    def _read_hellos(self, now: float) -> None:
        sock = self._sock
        if sock is None:
            return
        had_listener = bool(self._subscribers.addrs)
        while True:
            try:
                datagram, addr = sock.recvfrom(2048)
            except BlockingIOError:
                break
            except OSError:
                break
            try:
                hello = live_wire.decode_hello(datagram, self._key)
            except WireError:
                self.bad_datagrams += 1
                continue
            known = addr in self._subscribers.addrs
            selection = self._subscribers.hello(hello, addr, now)
            if selection is not None:
                self._sampler.select(selection)
                logger.info(
                    "live: pit selected %d channels, refresh %d ms",
                    len(selection.intervals),
                    selection.refresh_ms,
                )
            elif not known and addr in self._subscribers.addrs:
                # A new listener or a new path: give it the full picture
                # rather than only what changes from here on.
                self._sampler.forget_sent()
        if not had_listener and self._subscribers.addrs:
            self._sampler.forget_sent()
