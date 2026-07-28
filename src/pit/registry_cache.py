"""Registry-generation cache: the decode half every pit consumer shares.

`Channel.id` is only meaningful within one `registry_seq`
(`proto/telemetry.proto`), so decoding a `SampleBatch` means holding the
exact `ChannelRegistry` generation it was encoded against. This class holds
every generation seen and enforces the three consumer obligations from
`docs/WIRE_FORMAT.md` -> Registry lifecycle:

- **hard `registry_seq` equality** — a batch whose generation is unknown is
  not decoded, ever, not even best-effort;
- **`scale`/`offset` applied** when non-zero (the fixed-point convention);
- **unknown `format_version` rejected loudly** rather than guessed at.

Promoted from `tools/decode.py`, which was written as the forerunner of the
pit services and still uses it. The ingest-writer (P3.2) and live-decoder
(P3.3) import it from here — there is deliberately only one copy of this
logic in the tree.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.pb import telemetry_pb2 as pb

logger = logging.getLogger(__name__)

# A producer that omits format_version means 1 (proto3 default), so 0 and 1
# are the same shape — see docs/WIRE_FORMAT.md -> format_version.
SUPPORTED_FORMAT_VERSIONS = frozenset({0, 1})

# Both message kinds share the tele.<vehicle>.<source-class> subject; the
# producer tags which is which so consumers never guess (docs/WIRE_FORMAT.md
# -> Subject hierarchy).
MSG_TYPE_HEADER = "Openlaps-Msg-Type"
MSG_TYPE_REGISTRY = "registry"
MSG_TYPE_BATCH = "batch"
REGISTRY_SOURCE_CLASS = "catalog"

# Why a batch could not be decoded. The two cases need opposite handling:
# an unknown generation is transient and worth waiting for, an unknown
# payload shape is permanent and must not wedge the consumer.
REJECT_UNKNOWN_SEQ = "unknown_registry_seq"
REJECT_BAD_VERSION = "bad_format_version"


@dataclass(frozen=True, slots=True)
class DecodedSample:
    """One sample resolved to its channel and physical value."""

    capture_unix_ms: float
    channel: pb.Channel
    value: object


@dataclass(frozen=True, slots=True)
class DecodedBatch:
    """One `SampleBatch` resolved against its own registry generation."""

    registry_seq: int
    epoch_unix_ms: int
    epoch_mono_ns: int
    samples: list[DecodedSample]


class RegistryCache:
    """Every ChannelRegistry generation seen on the stream, keyed by seq."""

    def __init__(self) -> None:
        self._registries: dict[int, pb.ChannelRegistry] = {}
        self._channels: dict[int, dict[int, pb.Channel]] = {}
        self.unknown_seqs: set[int] = set()
        self.bad_versions: set[int] = set()
        self.unknown_seq_batches = 0
        self.bad_version_batches = 0

    def add(self, payload: bytes) -> int:
        """Parse and retain one ChannelRegistry; returns its generation."""
        registry = pb.ChannelRegistry()
        registry.ParseFromString(payload)
        return self.add_registry(registry)

    def add_registry(self, registry: pb.ChannelRegistry) -> int:
        """Retain an already-parsed ChannelRegistry; returns its generation."""
        self._registries[registry.registry_seq] = registry
        self._channels[registry.registry_seq] = {
            channel.id: channel for channel in registry.channels
        }
        self.unknown_seqs.discard(registry.registry_seq)
        return registry.registry_seq

    def known(self, registry_seq: int) -> bool:
        """Whether this generation can be decoded against."""
        return registry_seq in self._channels

    def registry(self, registry_seq: int) -> pb.ChannelRegistry | None:
        """The retained generation, or None if it has never been seen."""
        return self._registries.get(registry_seq)

    def generations(self) -> list[int]:
        """Every generation held, oldest first."""
        return sorted(self._registries)

    def decode(self, payload: bytes) -> DecodedBatch | None:
        """Decode one batch, or None if it must not be decoded."""
        return self.decode_or_reason(payload)[0]

    def decode_or_reason(self, payload: bytes) -> tuple[DecodedBatch | None, str | None]:
        """Decode one batch, or explain why it must not be decoded.

        Refusing is a consumer MUST from docs/WIRE_FORMAT.md, not a soft
        failure: an unknown `registry_seq` means the caller has to go find
        that generation before this batch means anything, and an unknown
        `format_version` means the payload shape is not the one this code
        understands. Callers that must act differently on the two — waiting
        out the first, dropping the second — read the returned reason.
        """
        batch = pb.SampleBatch()
        batch.ParseFromString(payload)
        if batch.format_version not in SUPPORTED_FORMAT_VERSIONS:
            self.bad_version_batches += 1
            if batch.format_version not in self.bad_versions:
                self.bad_versions.add(batch.format_version)
                logger.error("rejecting batch with unknown format_version=%d", batch.format_version)
            return None, REJECT_BAD_VERSION
        channels = self._channels.get(batch.registry_seq)
        if channels is None:
            self.unknown_seq_batches += 1
            if batch.registry_seq not in self.unknown_seqs:
                self.unknown_seqs.add(batch.registry_seq)
                logger.warning(
                    "batch references unknown registry_seq=%d; "
                    "waiting for its registry to appear on the stream",
                    batch.registry_seq,
                )
            return None, REJECT_UNKNOWN_SEQ
        samples = []
        for sample in batch.samples:
            channel = channels.get(sample.channel_id)
            if channel is None:
                continue
            kind = sample.WhichOneof("value")
            value: object = getattr(sample, kind) if kind else None
            if channel.scale and isinstance(value, (int, float)) and not isinstance(value, bool):
                # Fixed-point convention: physical = wire * scale + offset.
                value = value * channel.scale + channel.offset
            samples.append(
                DecodedSample(
                    capture_unix_ms=batch.batch_epoch_unix_ms + sample.t_offset_us / 1000.0,
                    channel=channel,
                    value=value,
                )
            )
        return (
            DecodedBatch(
                registry_seq=batch.registry_seq,
                epoch_unix_ms=batch.batch_epoch_unix_ms,
                epoch_mono_ns=batch.batch_epoch_mono_ns,
                samples=samples,
            ),
            None,
        )
