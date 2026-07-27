"""SocketCAN collector: one instance per configured bus.

The collector is deliberately dumb (``docs/AGENT_DESIGN.md`` -> Process
model): it decodes every signal its DBCs know about, stamps it, and hands it
to an ``emit`` callback. It owns no queue and applies no filtering -- the
catalog decides what is consumed, and unmapped source refs are dropped
cheaply downstream by the mapper.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import can
import cantools
from cantools.database.can import Message as DbcMessage

from core.config import BusConfig
from core.samples import Sample, SampleValue

logger = logging.getLogger(__name__)

DEFAULT_RECV_TIMEOUT_S = 0.2
DEFAULT_BACKOFF_START_S = 0.5
DEFAULT_BACKOFF_MAX_S = 30.0
_MAX_TRACKED_UNKNOWN_IDS = 256

WallClock = Callable[[int], float]
"""Maps ``t_mono_ns`` onto wall-clock milliseconds."""

Emit = Callable[[Sample], None]
BusFactory = Callable[[BusConfig], can.BusABC]


class CanDecoderError(ValueError):
    """A DBC attached to a bus could not be loaded."""


class MonotonicWallClock:
    """Fixed-offset monotonic-to-wall mapping, anchored at construction.

    Collectors only need *a* mapping; the agent supplies its GNSS-steered one
    (``docs/AGENT_DESIGN.md`` -> Clock discipline) in production.
    """

    __slots__ = ("_offset_ms",)

    def __init__(self) -> None:
        self._offset_ms = time.time() * 1000.0 - time.monotonic_ns() / 1e6

    def __call__(self, t_mono_ns: int) -> float:
        """Return the wall-clock milliseconds corresponding to ``t_mono_ns``."""
        return self._offset_ms + t_mono_ns / 1e6


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


@dataclass(slots=True)
class CanDecodeStats:
    """Decode-side counters, surfaced by the agent as ``sys.agent.*``."""

    frames: int = 0
    decoded_frames: int = 0
    unknown_frames: int = 0
    malformed_frames: int = 0
    samples: int = 0


@dataclass(slots=True)
class CanTransportStats:
    """Transport-side counters for one bus."""

    open_failures: int = 0
    bus_errors: int = 0
    reconnects: int = 0


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

    def __init__(self, bus_name: str, dbcs: Sequence[tuple[str, str | Path]]) -> None:
        """Build the frame index for ``bus_name`` from ``(device, dbc path)`` pairs."""
        self.bus_name = bus_name
        self.stats = CanDecodeStats()
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
                self._by_frame.setdefault(key, []).append(
                    _DeviceMessage(device=device, message=message, source_refs=source_refs)
                )

    def decode(self, message: can.Message) -> list[tuple[str, SampleValue]]:
        """Decode one frame into ``(source_ref, value)`` pairs.

        Unknown IDs and undecodable payloads are counted and skipped: a noisy
        or half-understood bus must never interrupt capture.
        """
        stats = self.stats
        stats.frames += 1
        if message.is_error_frame or message.is_remote_frame:
            stats.malformed_frames += 1
            return []
        key = (message.arbitration_id, bool(message.is_extended_id))
        candidates = self._by_frame.get(key)
        if candidates is None:
            stats.unknown_frames += 1
            self._report_unknown(key)
            return []

        data = message.data
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
        recv_timeout_s: float = DEFAULT_RECV_TIMEOUT_S,
        backoff_start_s: float = DEFAULT_BACKOFF_START_S,
        backoff_max_s: float = DEFAULT_BACKOFF_MAX_S,
    ) -> None:
        """Build a collector for ``config``, resolving DBC paths under ``profile_dir``."""
        root = Path(profile_dir)
        self.config = config
        self.decoder = CanDecoder(
            config.name, [(dbc.device, root / dbc.file) for dbc in config.dbcs]
        )
        self.stats = CanTransportStats()
        self._emit = emit
        self._wall_clock = wall_clock if wall_clock is not None else MonotonicWallClock()
        self._bus_factory = bus_factory if bus_factory is not None else _default_bus_factory
        self._recv_timeout_s = recv_timeout_s
        self._backoff_start_s = backoff_start_s
        self._backoff_max_s = backoff_max_s
        self._frame_clock = _FrameClock()
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
        t_wall_ms = self._wall_clock(t_mono_ns)
        decoded = self.decoder.decode(message)
        for source_ref, value in decoded:
            self._emit(Sample(source_ref, t_mono_ns, t_wall_ms, value))
        return len(decoded)

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
        while not stop.is_set():
            try:
                message = bus.recv(timeout=self._recv_timeout_s)
            except (can.CanError, OSError) as exc:
                self.stats.bus_errors += 1
                logger.warning("bus %s: read failed: %s", self.config.name, exc)
                return received
            if message is None:
                continue
            received = True
            self.handle_message(message)
        return received


def _default_bus_factory(config: BusConfig) -> can.BusABC:
    return can.Bus(channel=config.interface, interface="socketcan")


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
