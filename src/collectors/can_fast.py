"""The CAN ingest path run outside the interpreter (`openlaps_canfast`).

`collectors.can.CanCollector` reads, decodes and emits one Python object per
signal, and the pipeline thread then maps, filters and batches each of them.
On a busy bus that is most of what the agent does, and every thread doing it
waits on every other for the interpreter. The extension does all of it on a
thread of its own: it owns the bus socket, stamps and decodes frames, applies
the report-by-exception policies and serializes `SampleBatch` payloads.

Python keeps what it is good at. It parses the DBCs and the catalog once, at
start, and hands the extension a flat *plan*: for every wanted signal the bit
field, the scaling, the channel id, the wire encoding and the filter policy.
The extension never sees a DBC, so there is one definition of what a signal
is. `tests/test_can_fast.py` replays recorded buses through both paths and
requires byte-identical batches.

The Python collector remains the reference and the fallback: the agent uses
this one only when the extension is installed and the bus's signals are all
of a kind the plan can express.
"""

from __future__ import annotations

import json
import logging
import struct
import threading
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cantools.database.can import Message as DbcMessage
from cantools.database.can.signal import Signal as DbcSignal

from collectors.can import RECV_BUFFER_BYTES, CanDecodeStats, CanTransportStats, _load_dbc
from core.catalog import ChannelPolicy, RuntimeCatalog
from core.config import BusConfig
from core.samples import SampleValue

try:  # The extension is built separately (rust/canfast) and may be absent.
    import openlaps_canfast
except ImportError:  # pragma: no cover - exercised on hosts without the wheel
    openlaps_canfast = None

logger = logging.getLogger(__name__)

_TAP_RECORD = struct.Struct("<IBqd")
_MAX_SIGNAL_BITS = 64
_MAX_MESSAGE_BYTES = 64


class UnsupportedPlan(ValueError):
    """The bus uses a DBC feature the extension's plan cannot express."""


def available() -> bool:
    """Whether the compiled extension can be imported on this host."""
    return openlaps_canfast is not None


def _field(signal: DbcSignal) -> dict[str, Any]:
    if signal.conversion.is_float:
        raise UnsupportedPlan(f"{signal.name}: floating-point raw signals")
    if not 0 < signal.length <= _MAX_SIGNAL_BITS:
        raise UnsupportedPlan(f"{signal.name}: {signal.length}-bit signal")
    return {
        "start": signal.start,
        "length": signal.length,
        "big_endian": signal.byte_order == "big_endian",
        "signed": bool(signal.is_signed),
    }


def _signal(signal: DbcSignal, channel_id: int, policy: ChannelPolicy, tap: bool) -> dict[str, Any]:
    conversion = signal.conversion
    # cantools keeps a value integral when the DBC's scale and offset are
    # whole numbers, and the type survives into the wire encoding.
    integer = isinstance(conversion.raw_to_scaled(1, False), int)
    rbe = None
    if policy.rbe is not None:
        rbe = {
            "deadband": policy.rbe.deadband,
            "min_interval_ns": policy.rbe.min_interval_ns,
            "max_interval_ns": policy.rbe.max_interval_ns,
        }
    return {
        **_field(signal),
        "integer": integer,
        "scale": float(conversion.scale),
        "offset": float(conversion.offset),
        "channel_id": channel_id,
        "value_type": int(policy.value_type),
        "encode_scale": float(policy.scale),
        "encode_offset": float(policy.offset),
        "rbe": rbe,
        "tap": tap,
    }


def _node(
    codec: dict[str, Any],
    source_refs: dict[str, str],
    catalog: RuntimeCatalog,
    taps: Collection[int],
) -> tuple[dict[str, Any], int]:
    """One node of a message's multiplexer tree, and how many signals it wants."""
    by_name = {signal.name: signal for signal in codec["signals"]}
    signals = []
    for signal in codec["signals"]:
        mapping = catalog.source_map.get(source_refs.get(signal.name, ""))
        if mapping is None:
            continue
        channel_id, policy = mapping
        signals.append(_signal(signal, channel_id, policy, channel_id in taps))
    wanted = len(signals)
    muxes = []
    for selector_name, children in codec["multiplexers"].items():
        selector = by_name[selector_name]
        if selector.conversion.scale != 1 or selector.conversion.offset != 0:
            raise UnsupportedPlan(f"{selector_name}: scaled multiplexer selector")
        nodes = {}
        for mux_id, child in children.items():
            node, child_wanted = _node(child, source_refs, catalog, taps)
            nodes[str(int(mux_id))] = node
            wanted += child_wanted
        muxes.append({"selector": _field(selector), "nodes": nodes})
    return {"signals": signals, "muxes": muxes}, wanted


def build_plan(
    bus: BusConfig,
    profile_dir: str | Path,
    catalog: RuntimeCatalog,
    *,
    tick_ms: int,
    taps: Collection[int] = (),
) -> dict[str, Any]:
    """Everything the extension needs to turn this bus's frames into batches."""
    root = Path(profile_dir)
    messages = []
    planned: set[tuple[int, bool]] = set()
    defined: set[tuple[int, bool]] = set()
    for dbc in bus.dbcs:
        for message in _load_dbc(dbc.device, root / dbc.file).messages:
            key = (message.frame_id, bool(message.is_extended_frame))
            defined.add(key)
            node = _message_node(bus.name, dbc.device, message, catalog, taps)
            if node is None:
                continue
            planned.add(key)
            messages.append(
                {
                    "frame_id": message.frame_id,
                    "extended": key[1],
                    "length": message.length,
                    "root": node,
                }
            )
    return {
        "registry_seq": catalog.registry.registry_seq,
        "tick_ns": tick_ms * 1_000_000,
        "messages": messages,
        "unwanted": sorted(defined - planned),
    }


def _message_node(
    bus_name: str,
    device: str,
    message: DbcMessage,
    catalog: RuntimeCatalog,
    taps: Collection[int],
) -> dict[str, Any] | None:
    source_refs = {
        signal.name: f"{bus_name}:{device}.{message.name}.{signal.name}"
        for signal in message.signals
    }
    if not any(ref in catalog.source_map for ref in source_refs.values()):
        return None
    codec = message._codecs  # noqa: SLF001 - the decode tree cantools itself walks
    if message.is_container or codec is None:
        raise UnsupportedPlan(f"{message.name}: container message")
    if message.length > _MAX_MESSAGE_BYTES:
        raise UnsupportedPlan(f"{message.name}: {message.length}-byte message")
    node, wanted = _node(codec, source_refs, catalog, taps)
    return node if wanted else None


@dataclass(frozen=True, slots=True)
class Tap:
    """One pre-filter sample the pipeline's fusion or timing app asked for."""

    channel_id: int
    t_mono_ns: int
    value: SampleValue


@dataclass(frozen=True, slots=True)
class FastBatch:
    """A serialized `SampleBatch` for this bus's source class."""

    payload: bytes
    epoch_unix_ms: int
    epoch_mono_ns: int


class FastCanCollector:
    """A bus read, decoded, filtered and batched by the extension.

    It presents what the agent's health loop reads off a `CanCollector`
    (`decode_stats`, `stats`, `is_running`, `take_capture_lag_max_ms`), but it
    emits nothing: the pipeline thread calls `drain` once a tick instead.
    """

    def __init__(
        self,
        config: BusConfig,
        profile_dir: str | Path,
        catalog: RuntimeCatalog,
        *,
        tick_ms: int,
        taps: Collection[int] = (),
    ) -> None:
        if openlaps_canfast is None:
            raise RuntimeError("the openlaps_canfast extension is not installed")
        self.config = config
        self.plan = build_plan(config, profile_dir, catalog, tick_ms=tick_ms, taps=taps)
        self._ingest = openlaps_canfast.CanIngest(json.dumps(self.plan))
        self.decoder = self  # the health loop reads `decode_stats` off either kind
        self._decode_stats = CanDecodeStats()
        self._stats = CanTransportStats()
        self._lock = threading.Lock()
        self._warned_buffer = False
        self._clock_steps = 0
        self._rbe_suppressed = 0
        self._encode_failures = 0

    @property
    def rbe_suppressed(self) -> int:
        """Samples the report-by-exception policies have held back."""
        self._refresh()
        return self._rbe_suppressed

    @property
    def encode_failures(self) -> int:
        """Tick windows discarded because a value could not be encoded."""
        self._refresh()
        return self._encode_failures

    @property
    def name(self) -> str:
        """The bus name used as the source-ref prefix."""
        return self.config.name

    @property
    def decode_stats(self) -> CanDecodeStats:
        """Decode counters for this bus, read from the extension."""
        self._refresh()
        return self._decode_stats

    @property
    def stats(self) -> CanTransportStats:
        """Transport counters for this bus, read from the extension."""
        self._refresh()
        return self._stats

    def _refresh(self) -> None:
        raw = self._ingest.stats()
        with self._lock:
            decode, transport = self._decode_stats, self._stats
            for name in (
                "frames",
                "decoded_frames",
                "unknown_frames",
                "malformed_frames",
                "unwanted_frames",
                "samples",
            ):
                setattr(decode, name, int(raw[name]))
            for name in (
                "open_failures",
                "bus_errors",
                "reconnects",
                "socket_drops",
                "recv_buffer_bytes",
                "clock_steps",
            ):
                setattr(transport, name, int(raw[name]))
            transport.capture_lag_ms = raw["capture_lag_ms"]
            self._rbe_suppressed = int(raw["rbe_suppressed"])
            self._encode_failures = int(raw["encode_failures"])
            granted = transport.recv_buffer_bytes
            if granted and granted < RECV_BUFFER_BYTES and not self._warned_buffer:
                self._warned_buffer = True
                logger.warning(
                    "bus %s: receive buffer is %d kB, not the %d kB asked for; "
                    "raise net.core.rmem_max or frames are dropped when the agent is busy",
                    self.name,
                    granted // 1024,
                    RECV_BUFFER_BYTES // 1024,
                )
            if transport.clock_steps > self._clock_steps:
                self._clock_steps = transport.clock_steps
                logger.warning("bus %s: system clock stepped; frame times follow it", self.name)

    def take_capture_lag_max_ms(self) -> float:
        """The worst capture lag since the last call, then start a new window."""
        return self._ingest.take_capture_lag_max_ms()

    def start(self) -> None:
        """Start the reader thread; it reopens the interface itself on errors."""
        logger.info(
            "bus %s: starting on %s (%d bit/s, %d DBC(s), compiled ingest, %d message(s))",
            self.name,
            self.config.interface,
            self.config.bitrate,
            len(self.config.dbcs),
            len(self.plan["messages"]),
        )
        self._ingest.start(self.config.interface, RECV_BUFFER_BYTES, f"can-{self.name}")

    def stop(self, timeout: float = 5.0) -> None:  # noqa: ARG002 - same call as CanCollector
        """Stop the reader thread and wait for it."""
        self._ingest.stop()
        logger.info("bus %s: stopped", self.name)

    def is_running(self) -> bool:
        """Whether the reader thread is alive."""
        return self._ingest.is_running()

    def feed(self, frame_id: int, is_extended: bool, data: bytes, t_mono_ns: int) -> None:
        """Run one frame through the engine without a socket (tests, replays)."""
        self._ingest.feed(frame_id, is_extended, data, t_mono_ns)

    def drain(
        self, realtime_ns: int | None = None, monotonic_ns: int | None = None
    ) -> tuple[list[Tap], list[FastBatch]]:
        """Tapped samples and finished batches since the last call."""
        batches, packed, unknown = self._ingest.drain(realtime_ns, monotonic_ns)
        for frame_id in unknown:
            logger.info("bus %s: no DBC defines frame ID 0x%X", self.name, frame_id)
        taps = [
            Tap(channel_id, t_mono_ns, value if is_float else int(value))
            for channel_id, is_float, t_mono_ns, value in _TAP_RECORD.iter_unpack(packed)
        ]
        return taps, [FastBatch(*batch) for batch in batches]
