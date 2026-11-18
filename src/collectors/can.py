"""SocketCAN collector: one instance per configured bus.

The collector is deliberately dumb (``docs/AGENT_DESIGN.md`` -> Process
model): it decodes every signal its DBCs know about, stamps it, and hands it
to an ``emit`` callback. It owns no queue and applies no filtering -- the
catalog decides what is consumed, and unmapped source refs are dropped
cheaply downstream by the mapper.
"""

from __future__ import annotations

import logging
import select
import socket
import struct
import threading
import time
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import can
import cantools
from cantools.database.can import Message as DbcMessage

from collectors.clock import Emit, MonotonicWallClock, WallClock
from core.config import BusConfig
from core.samples import Sample, SampleValue

logger = logging.getLogger(__name__)

DEFAULT_RECV_TIMEOUT_S = 0.2
DEFAULT_BACKOFF_START_S = 0.5
DEFAULT_BACKOFF_MAX_S = 30.0
_MAX_TRACKED_UNKNOWN_IDS = 256
# A CAN frame costs the socket about 1 kB of receive buffer, so the kernel's
# default (about 200 kB) holds a tenth of a second of a busy 1 Mbit/s bus.
# The reader shares the interpreter with every other thread in the agent and
# is routinely kept waiting longer than that; whatever does not fit is dropped
# by the kernel without a trace in any interface counter. Ask for seconds.
# The kernel silently caps the request at net.core.rmem_max.
RECV_BUFFER_BYTES = 16 * 1024 * 1024
_SOCKET_POLL_FRAMES = 512
_SO_MEMINFO = 55
_SO_TIMESTAMPNS = 35  # also the cmsg type the stamp comes back under
_SOL_CAN_RAW = 101
_CAN_RAW_FD_FRAMES = 5
_CANFD_MTU = 72
_CAN_EFF_FLAG = 0x80000000
_CAN_RTR_FLAG = 0x40000000
_CAN_ERR_FLAG = 0x20000000
_CAN_EFF_MASK = 0x1FFFFFFF
_CAN_SFF_MASK = 0x000007FF
_FRAME_HEADER = struct.Struct("=IB")  # can_id, payload length
_TIMESPEC = struct.Struct("@ll")
# Frames taken per wake-up before the loop looks at the stop flag and the
# drop counter again: a second or so of a busy bus.
_DRAIN_MAX = 2048
# A realtime-minus-monotonic reading is trusted only if the two clocks were
# read this close together, and a change this large is a clock step.
_CLOCK_BRACKET_NS = 100_000
_CLOCK_STEP_NS = 250_000
_SK_MEMINFO_VARS = 9
_SK_MEMINFO_DROPS = 8

BusFactory = Callable[[BusConfig], can.BusABC]


class CanDecoderError(ValueError):
    """A DBC attached to a bus could not be loaded."""


class _FrameClock:
    """Projects socketCAN frame timestamps onto the monotonic timebase.

    SocketCAN stamps frames against the realtime clock. Anchoring the first
    frame of a connection to ``time.monotonic_ns()`` keeps the kernel's
    inter-frame resolution -- which is the point of using frame timestamps at
    all -- while leaving ordering immune to wall-clock steps.
    """

    __slots__ = ("_epoch_ref", "_mono_ref")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Drop the anchor so the next frame re-establishes it."""
        self._epoch_ref: float | None = None
        self._mono_ref = 0

    def to_mono_ns(self, timestamp: float) -> int:
        """Convert a frame timestamp; unstamped frames fall back to *now*."""
        now = time.monotonic_ns()
        if not timestamp:
            return now
        if self._epoch_ref is None:
            self._epoch_ref = timestamp
            self._mono_ref = now
        return self._mono_ref + round((timestamp - self._epoch_ref) * 1e9)


class _KernelStampClock:
    """Turns the kernel's realtime frame stamps into monotonic time.

    The kernel stamps a frame when the driver hands it over, long before this
    process reads it, so the stamp is the capture time however long the frame
    then sat in the socket buffer. It is a realtime stamp, though, and realtime
    gets stepped: a board that boots with a wrong clock is corrected by
    seconds, or months, once chrony has a source. Realtime and monotonic tick
    at the same disciplined rate, so their difference is constant *except*
    across a step. Holding that difference and replacing it when it jumps
    keeps every frame on the monotonic timebase to within the step-detection
    threshold, where anchoring once on the first frame would leave every later
    frame wrong by the size of the step.
    """

    __slots__ = ("_bus_name", "offset_ns", "steps")

    def __init__(self, bus_name: str) -> None:
        self._bus_name = bus_name
        self.offset_ns: int | None = None
        self.steps = 0

    def resync(self) -> None:
        """Re-read the clocks' difference; adopt it if the realtime clock stepped."""
        before = time.monotonic_ns()
        realtime = time.time_ns()
        after = time.monotonic_ns()
        if after - before > _CLOCK_BRACKET_NS:
            if self.offset_ns is None:
                self.offset_ns = realtime - after
            return  # read across a pre-emption: not a measurement
        offset = realtime - (before + after) // 2
        if self.offset_ns is None:
            self.offset_ns = offset
        elif abs(offset - self.offset_ns) > _CLOCK_STEP_NS:
            self.steps += 1
            logger.warning(
                "bus %s: system clock stepped by %.3f s; frame times follow it",
                self._bus_name,
                (offset - self.offset_ns) / 1e9,
            )
            self.offset_ns = offset

    def to_mono_ns(self, stamp_ns: int) -> int:
        """Monotonic time of a frame the kernel stamped at realtime ``stamp_ns``."""
        if self.offset_ns is None:
            self.resync()
        return stamp_ns - self.offset_ns  # type: ignore[operator]


@dataclass(slots=True)
class CanDecodeStats:
    """Decode-side counters, surfaced by the agent as ``sys.agent.*``."""

    frames: int = 0
    decoded_frames: int = 0
    unknown_frames: int = 0
    malformed_frames: int = 0
    # Frames a DBC defines but whose signals nothing asked for; never decoded.
    unwanted_frames: int = 0
    samples: int = 0


@dataclass(slots=True)
class CanTransportStats:
    """Transport-side counters for one bus."""

    open_failures: int = 0
    bus_errors: int = 0
    reconnects: int = 0
    # Frames the kernel discarded because this reader's socket buffer was full.
    socket_drops: int = 0
    recv_buffer_bytes: int = 0
    # How far behind the bus the reader is: the age of a frame when it is
    # decoded, relative to the first frame of the connection. It climbs while
    # the socket buffer is filling, well before anything is dropped.
    capture_lag_ms: float = 0.0
    capture_lag_max_ms: float = 0.0
    # Times the system clock was stepped under a live connection.
    clock_steps: int = 0


@dataclass(frozen=True, slots=True)
class _DeviceMessage:
    """One DBC message together with the device alias that owns it."""

    device: str
    message: DbcMessage
    source_refs: dict[str, str]


class CanDecoder:
    """Decodes frames against every DBC attached to one bus.

    Each DBC keeps its own database rather than being merged, so two devices
    may legitimately define the same message name or frame ID: a frame is
    decoded once per device that knows its ID and every resulting signal is
    namespaced ``<bus>:<device>.<MESSAGE>.<SIGNAL>``.
    """

    def __init__(
        self,
        bus_name: str,
        dbcs: Sequence[tuple[str, str | Path]],
        *,
        wanted: Collection[str] | None = None,
    ) -> None:
        """Build the frame index for ``bus_name`` from ``(device, dbc path)`` pairs.

        ``wanted`` is the set of source refs somebody consumes. Given it, a
        frame none of whose signals are wanted is counted and skipped before
        any decoding, and unwanted signals of the rest are not emitted. On the
        example car that is a third of the frames and two thirds of the
        samples, all of which would otherwise be built, queued and discarded
        by the mapper while the reader holds the interpreter.
        """
        self.bus_name = bus_name
        self.stats = CanDecodeStats()
        self._filtered = wanted is not None
        self._unwanted: set[tuple[int, bool]] = set()
        self._by_frame: dict[tuple[int, bool], list[_DeviceMessage]] = {}
        self._reported_unknown: set[tuple[int, bool]] = set()
        self._reported_malformed: set[tuple[int, str]] = set()
        for device, path in dbcs:
            for message in _load_dbc(device, path).messages:
                key = (message.frame_id, bool(message.is_extended_frame))
                source_refs = {
                    signal.name: f"{bus_name}:{device}.{message.name}.{signal.name}"
                    for signal in message.signals
                }
                if wanted is not None:
                    source_refs = {name: ref for name, ref in source_refs.items() if ref in wanted}
                    if not source_refs:
                        self._unwanted.add(key)
                        continue
                self._by_frame.setdefault(key, []).append(
                    _DeviceMessage(device=device, message=message, source_refs=source_refs)
                )
        self._unwanted -= set(self._by_frame)

    def decode(self, message: can.Message) -> list[tuple[str, SampleValue]]:
        """Decode one frame into ``(source_ref, value)`` pairs.

        Unknown IDs and undecodable payloads are counted and skipped: a noisy
        or half-understood bus must never interrupt capture.
        """
        if message.is_error_frame or message.is_remote_frame:
            self.stats.frames += 1
            self.stats.malformed_frames += 1
            return []
        return self.decode_frame(message.arbitration_id, bool(message.is_extended_id), message.data)

    def decode_frame(
        self, arbitration_id: int, is_extended: bool, data: bytes | bytearray
    ) -> list[tuple[str, SampleValue]]:
        """Decode one data frame given as its bare id and payload."""
        stats = self.stats
        stats.frames += 1
        key = (arbitration_id, is_extended)
        candidates = self._by_frame.get(key)
        if candidates is None:
            if key in self._unwanted:
                stats.unwanted_frames += 1
                return []
            stats.unknown_frames += 1
            self._report_unknown(key)
            return []

        decoded_values: list[tuple[str, SampleValue]] = []
        failures = 0
        for candidate in candidates:
            try:
                # Broad: any DBC/bitstruct decode failure is a data problem,
                # never a reason to lose the read loop.
                decoded = candidate.message.decode(
                    data, decode_choices=False, allow_truncated=False
                )
            except Exception as exc:
                failures += 1
                self._report_malformed(candidate, exc)
                continue
            source_refs = candidate.source_refs
            for name, value in decoded.items():
                source_ref = source_refs.get(name)
                if source_ref is None and self._filtered:
                    continue
                if source_ref is None:  # pragma: no cover - container/extended frames
                    source_ref = (
                        f"{self.bus_name}:{candidate.device}.{candidate.message.name}.{name}"
                    )
                decoded_values.append((source_ref, value))

        if failures == len(candidates):
            stats.malformed_frames += 1
        else:
            stats.decoded_frames += 1
        stats.samples += len(decoded_values)
        return decoded_values

    def _report_unknown(self, key: tuple[int, bool]) -> None:
        if key in self._reported_unknown or len(self._reported_unknown) >= _MAX_TRACKED_UNKNOWN_IDS:
            return
        self._reported_unknown.add(key)
        logger.info("bus %s: no DBC defines frame ID 0x%X", self.bus_name, key[0])

    def _report_malformed(self, candidate: _DeviceMessage, exc: Exception) -> None:
        key = (candidate.message.frame_id, candidate.device)
        if key in self._reported_malformed:
            return
        self._reported_malformed.add(key)
        logger.warning(
            "bus %s: cannot decode %s.%s (0x%X): %s",
            self.bus_name,
            candidate.device,
            candidate.message.name,
            candidate.message.frame_id,
            exc,
        )


class CanCollector:
    """Reads one socketCAN interface and emits a `Sample` per decoded signal.

    The interface itself (bring-up, bitrate) is the platform's job; this class
    only opens it, and treats an absent or bus-off interface as a transient
    condition to retry with backoff rather than a fatal error.
    """

    def __init__(
        self,
        config: BusConfig,
        profile_dir: str | Path,
        emit: Emit,
        *,
        wall_clock: WallClock | None = None,
        bus_factory: BusFactory | None = None,
        wanted: Collection[str] | None = None,
        recv_timeout_s: float = DEFAULT_RECV_TIMEOUT_S,
        backoff_start_s: float = DEFAULT_BACKOFF_START_S,
        backoff_max_s: float = DEFAULT_BACKOFF_MAX_S,
    ) -> None:
        """Build a collector for ``config``, resolving DBC paths under ``profile_dir``."""
        root = Path(profile_dir)
        self.config = config
        self.decoder = CanDecoder(
            config.name, [(dbc.device, root / dbc.file) for dbc in config.dbcs], wanted=wanted
        )
        self.stats = CanTransportStats()
        self._emit = emit
        self._wall_clock = wall_clock if wall_clock is not None else MonotonicWallClock()
        self._bus_factory = bus_factory if bus_factory is not None else _default_bus_factory
        self._recv_timeout_s = recv_timeout_s
        self._backoff_start_s = backoff_start_s
        self._backoff_max_s = backoff_max_s
        self._frame_clock = _FrameClock()
        self._stamp_clock = _KernelStampClock(config.name)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def name(self) -> str:
        """The bus name used as the source-ref prefix."""
        return self.config.name

    @property
    def decode_stats(self) -> CanDecodeStats:
        """Decode counters for this bus."""
        return self.decoder.stats

    def handle_message(self, message: can.Message) -> int:
        """Decode and emit one frame; returns the number of samples emitted."""
        t_mono_ns = self._frame_clock.to_mono_ns(message.timestamp)
        self._note_lag(t_mono_ns)
        t_wall_ms = self._wall_clock(t_mono_ns)
        decoded = self.decoder.decode(message)
        for source_ref, value in decoded:
            self._emit(Sample(source_ref, t_mono_ns, t_wall_ms, value))
        return len(decoded)

    def _emit_frame(
        self, arbitration_id: int, is_extended: bool, data: bytes, t_mono_ns: int
    ) -> None:
        self._note_lag(t_mono_ns)
        decoded = self.decoder.decode_frame(arbitration_id, is_extended, data)
        if not decoded:
            return
        t_wall_ms = self._wall_clock(t_mono_ns)
        emit = self._emit
        for source_ref, value in decoded:
            emit(Sample(source_ref, t_mono_ns, t_wall_ms, value))

    def _note_lag(self, t_mono_ns: int) -> None:
        lag_ms = (time.monotonic_ns() - t_mono_ns) / 1e6
        stats = self.stats
        stats.capture_lag_ms = lag_ms
        if lag_ms > stats.capture_lag_max_ms:
            stats.capture_lag_max_ms = lag_ms

    def _read_socket_until_error(
        self, sock: socket.socket, stop: threading.Event, drops_before: int
    ) -> bool:
        """Read the bus socket directly; True if any frame arrived.

        python-can's ``recv`` costs a ``select``, a ``recvmsg`` and a
        ``Message`` with a dozen fields for every frame, which on a bus at
        1600 frames/s was most of what this thread did. Here one ``poll``
        wakes the loop and it then takes frames until the socket is empty,
        unpacking only the id, the payload and the kernel's stamp.
        """
        name = self.config.name
        try:
            sock.setsockopt(socket.SOL_SOCKET, _SO_TIMESTAMPNS, 1)
            sock.setblocking(False)
        except OSError as exc:
            self.stats.bus_errors += 1
            logger.warning("bus %s: cannot prepare the socket: %s", name, exc)
            return False
        try:
            sock.setsockopt(_SOL_CAN_RAW, _CAN_RAW_FD_FRAMES, 1)
        except OSError:
            pass  # classic frames only: the read size below still fits them
        poller = select.poll()
        poller.register(sock, select.POLLIN)
        timeout_ms = max(1, round(self._recv_timeout_s * 1000))
        recvmsg = sock.recvmsg
        clock = self._stamp_clock
        clock.offset_ns = None
        stats = self.stats
        unpack_header = _FRAME_HEADER.unpack_from
        received = False
        while not stop.is_set():
            try:
                ready = poller.poll(timeout_ms)
                stats.socket_drops = drops_before + _socket_drops(sock)
                if not ready:
                    continue
                clock.resync()
                stats.clock_steps = clock.steps
                for _ in range(_DRAIN_MAX):
                    try:
                        frame, ancdata, _flags, _address = recvmsg(_CANFD_MTU, 64)
                    except BlockingIOError:
                        break
                    if len(frame) < 8:
                        continue
                    can_id, length = unpack_header(frame)
                    stamp_ns = _frame_stamp_ns(ancdata)
                    t_mono_ns = clock.to_mono_ns(stamp_ns) if stamp_ns else time.monotonic_ns()
                    received = True
                    if can_id & (_CAN_ERR_FLAG | _CAN_RTR_FLAG):
                        self.decoder.stats.frames += 1
                        self.decoder.stats.malformed_frames += 1
                        continue
                    extended = bool(can_id & _CAN_EFF_FLAG)
                    self._emit_frame(
                        can_id & (_CAN_EFF_MASK if extended else _CAN_SFF_MASK),
                        extended,
                        frame[8 : 8 + length],
                        t_mono_ns,
                    )
            except OSError as exc:
                stats.bus_errors += 1
                logger.warning("bus %s: read failed: %s", name, exc)
                return received
        return received

    def take_capture_lag_max_ms(self) -> float:
        """The worst capture lag since the last call, then start a new window."""
        worst = self.stats.capture_lag_max_ms
        self.stats.capture_lag_max_ms = self.stats.capture_lag_ms
        return worst

    def replay(self, messages: Iterable[can.Message]) -> int:
        """Feed pre-captured frames through the collector; returns sample count."""
        return sum(self.handle_message(message) for message in messages)

    def run(self, stop: threading.Event | None = None) -> None:
        """Read the bus until ``stop`` is set, reconnecting with backoff."""
        stop = self._stop if stop is None else stop
        backoff = self._backoff_start_s
        logger.info(
            "bus %s: starting on %s (%d bit/s, %d DBC(s))",
            self.config.name,
            self.config.interface,
            self.config.bitrate,
            len(self.config.dbcs),
        )
        while not stop.is_set():
            bus = self._open()
            if bus is not None:
                self._frame_clock.reset()
                try:
                    if self._read_until_error(bus, stop):
                        backoff = self._backoff_start_s
                finally:
                    _shutdown(bus, self.config.name)
                if stop.is_set():
                    break
                self.stats.reconnects += 1
            stop.wait(backoff)
            backoff = min(backoff * 2, self._backoff_max_s)
        logger.info("bus %s: stopped", self.config.name)

    def start(self) -> None:
        """Run the read loop on a daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("bus %s: collector already running", self.config.name)
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._thread_main, name=f"can-{self.config.name}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the read loop to finish and join its thread."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)

    def is_running(self) -> bool:
        """Whether the collector thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def _thread_main(self) -> None:
        try:
            self.run(self._stop)
        except Exception:
            # The agent's health loop supervises restarts; one bad bus must
            # not take the process down with it.
            logger.exception("bus %s: collector thread failed", self.config.name)

    def _open(self) -> can.BusABC | None:
        try:
            return self._bus_factory(self.config)
        except (can.CanError, OSError) as exc:
            self.stats.open_failures += 1
            logger.warning(
                "bus %s: cannot open interface %s: %s", self.config.name, self.config.interface, exc
            )
            return None

    def _read_until_error(self, bus: can.BusABC, stop: threading.Event) -> bool:
        """Pump frames until ``stop`` or a bus error; True if any frame arrived."""
        received = False
        sock = getattr(bus, "socket", None)
        self.stats.recv_buffer_bytes = _grow_recv_buffer(sock, self.config.name)
        drops_before = self.stats.socket_drops
        if isinstance(sock, socket.socket) and sock.family == socket.AF_CAN:
            return self._read_socket_until_error(sock, stop, drops_before)
        since_poll = 0
        while not stop.is_set():
            try:
                message = bus.recv(timeout=self._recv_timeout_s)
            except (can.CanError, OSError) as exc:
                self.stats.bus_errors += 1
                logger.warning("bus %s: read failed: %s", self.config.name, exc)
                return received
            if message is None or since_poll >= _SOCKET_POLL_FRAMES:
                since_poll = 0
                self.stats.socket_drops = drops_before + _socket_drops(sock)
            if message is None:
                continue
            since_poll += 1
            received = True
            self.handle_message(message)
        return received


def _frame_stamp_ns(ancdata: list[tuple[int, int, bytes]]) -> int:
    """The kernel's receive stamp from a ``recvmsg`` ancillary list, or 0."""
    for level, kind, payload in ancdata:
        if level == socket.SOL_SOCKET and kind == _SO_TIMESTAMPNS:
            seconds, nanoseconds = _TIMESPEC.unpack_from(payload)
            return seconds * 1_000_000_000 + nanoseconds
    return 0


def _default_bus_factory(config: BusConfig) -> can.BusABC:
    return can.Bus(channel=config.interface, interface="socketcan")


def _grow_recv_buffer(sock: object, bus_name: str) -> int:
    """Ask for `RECV_BUFFER_BYTES` on the bus socket; returns what was granted."""
    if not isinstance(sock, socket.socket):
        return 0
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECV_BUFFER_BYTES)
        granted = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    except OSError as exc:
        logger.warning("bus %s: cannot size the receive buffer: %s", bus_name, exc)
        return 0
    # The kernel reports twice what it accepted, to cover its own bookkeeping.
    if granted < RECV_BUFFER_BYTES:
        logger.warning(
            "bus %s: receive buffer is %d kB, not the %d kB asked for; "
            "raise net.core.rmem_max or frames are dropped when the agent is busy",
            bus_name,
            granted // 1024,
            RECV_BUFFER_BYTES // 1024,
        )
    else:
        logger.info("bus %s: receive buffer %d kB", bus_name, granted // 1024)
    return granted


def _socket_drops(sock: object) -> int:
    """Frames the kernel has discarded on this socket for want of buffer."""
    if not isinstance(sock, socket.socket):
        return 0
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, _SO_MEMINFO, 4 * _SK_MEMINFO_VARS)
    except OSError:
        return 0
    if len(raw) < 4 * _SK_MEMINFO_VARS:
        return 0
    return struct.unpack(f"{_SK_MEMINFO_VARS}I", raw[: 4 * _SK_MEMINFO_VARS])[_SK_MEMINFO_DROPS]


def _shutdown(bus: can.BusABC, bus_name: str) -> None:
    try:
        bus.shutdown()
    except (can.CanError, OSError) as exc:
        logger.warning("bus %s: error closing interface: %s", bus_name, exc)


def _load_dbc(device: str, path: str | Path) -> cantools.database.can.Database:
    try:
        database = cantools.database.load_file(str(path))
    except Exception as exc:
        raise CanDecoderError(f"{path}: cannot load DBC for device {device!r}: {exc}") from exc
    if not isinstance(database, cantools.database.can.Database):
        raise CanDecoderError(f"{path}: device {device!r} is not a CAN database")
    return database
