"""Datagram formats of the car-to-pit live link (docs/WIRE_FORMAT.md -> Live link).

The gap-free copy of the car's telemetry crosses the radio as a JetStream
sourced stream: ordered, acknowledged, and therefore exactly as late as the
link is slow. Gauges want the opposite trade -- the newest value now, and
never mind the ones that did not make it -- so they ride these datagrams
instead, beside the stream rather than inside it.

Two datagram types, both small enough for one unfragmented packet:

- ``HELLO`` (pit -> car, about once a second): "send live frames to the
  address this came from", plus which channels the pit wants and how often.
  The car needs no address for the pit and no channel selection of its own;
  it answers whoever asks, on whatever path the question arrived by.
- ``FRAME`` (car -> pit): one ordinary ``SampleBatch`` holding the values
  that changed since the car last sent them, so the pit decodes it with the
  code that decodes every other batch.

With a shared key configured on both ends every datagram carries a keyed
BLAKE2b tag and anything unsigned is dropped. That is authenticity, not
secrecy: it stops a stranger on the network redirecting or faking the
gauges, and it does not hide the values (a tunnel does, when one is in use).
"""

from __future__ import annotations

import hashlib
import hmac
import struct
import zlib
from dataclasses import dataclass, field

MAGIC = b"OL"
VERSION = 1
TYPE_HELLO = 1
TYPE_FRAME = 2
DEFAULT_PORT = 7423
# Stays under the 1280-byte tunnel MTU with room for IP, UDP and the tag.
MAX_DATAGRAM = 1200
MAC_BYTES = 16

_PREFIX = struct.Struct("!2sBB")
_HELLO = struct.Struct("!QIIIIBB")
_FRAME = struct.Struct("!II")


class WireError(ValueError):
    """A datagram that is not ours, is malformed, or fails its tag."""


def derive_key(secret: str | None) -> bytes | None:
    """The MAC key for a configured secret; None leaves the link unsigned."""
    if not secret:
        return None
    return hashlib.blake2b(secret.encode("utf-8"), digest_size=32).digest()


def _seal(body: bytes, key: bytes | None) -> bytes:
    if key is None:
        return body
    return body + hashlib.blake2b(body, key=key, digest_size=MAC_BYTES).digest()


def _open(datagram: bytes, key: bytes | None, kind: int) -> memoryview:
    """Check prefix and tag; return the body after the 4-byte prefix."""
    if len(datagram) < _PREFIX.size:
        raise WireError("short datagram")
    magic, got_kind, version = _PREFIX.unpack_from(datagram)
    if magic != MAGIC or got_kind != kind:
        raise WireError("not a live-link datagram of this type")
    if version != VERSION:
        raise WireError(f"unknown live-link version {version}")
    end = len(datagram)
    if key is not None:
        end -= MAC_BYTES
        if end < _PREFIX.size:
            raise WireError("short datagram")
        expected = hashlib.blake2b(datagram[:end], key=key, digest_size=MAC_BYTES).digest()
        if not hmac.compare_digest(expected, datagram[end:]):
            raise WireError("bad tag")
    return memoryview(datagram)[_PREFIX.size : end]


def _put_varint(out: bytearray, value: int) -> None:
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


def _get_varint(view: memoryview, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(view) or shift > 35:
            raise WireError("truncated varint")
        byte = view[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


@dataclass(frozen=True, slots=True)
class Selection:
    """What the pit wants: channel id -> shortest interval between sends, ms.

    An interval of 0 means every frame. ``refresh_ms`` is how long a channel
    may stay unsent before its current value is repeated even though it has
    not changed -- what heals a lost datagram and fills a gauge that has
    only just started listening.
    """

    intervals: dict[int, int] = field(default_factory=dict)
    refresh_ms: int = 1000

    @property
    def version(self) -> int:
        """Identity of this selection; equal selections share a version."""
        out = bytearray(struct.pack("!I", self.refresh_ms))
        for channel_id in sorted(self.intervals):
            _put_varint(out, channel_id)
            _put_varint(out, self.intervals[channel_id])
        return zlib.crc32(out)


@dataclass(frozen=True, slots=True)
class Hello:
    """One decoded HELLO datagram (one part of a selection)."""

    session: int
    counter: int
    registry_seq: int
    selection_version: int
    refresh_ms: int
    part: int
    parts: int
    intervals: dict[int, int]


def encode_hello(
    *,
    session: int,
    counter: int,
    registry_seq: int,
    selection: Selection,
    key: bytes | None,
) -> list[bytes]:
    """The HELLO datagrams for one cycle; more than one for a big selection."""
    budget = MAX_DATAGRAM - _PREFIX.size - _HELLO.size - (MAC_BYTES if key else 0)
    bodies: list[bytearray] = [bytearray()]
    for channel_id in sorted(selection.intervals):
        entry = bytearray()
        _put_varint(entry, channel_id)
        _put_varint(entry, selection.intervals[channel_id])
        if len(bodies[-1]) + len(entry) > budget:
            bodies.append(bytearray())
        bodies[-1] += entry
    if len(bodies) > 255:
        raise WireError("selection too large for the live link")
    version = selection.version
    return [
        _seal(
            _PREFIX.pack(MAGIC, TYPE_HELLO, VERSION)
            + _HELLO.pack(
                session,
                counter,
                registry_seq,
                version,
                selection.refresh_ms,
                part,
                len(bodies),
            )
            + bytes(body),
            key,
        )
        for part, body in enumerate(bodies)
    ]


def decode_hello(datagram: bytes, key: bytes | None) -> Hello:
    """Parse and authenticate one HELLO; raises WireError on anything else."""
    body = _open(datagram, key, TYPE_HELLO)
    if len(body) < _HELLO.size:
        raise WireError("short hello")
    session, counter, registry_seq, version, refresh_ms, part, parts = _HELLO.unpack_from(body)
    if parts == 0 or part >= parts:
        raise WireError("bad hello part")
    intervals: dict[int, int] = {}
    pos = _HELLO.size
    while pos < len(body):
        channel_id, pos = _get_varint(body, pos)
        interval, pos = _get_varint(body, pos)
        intervals[channel_id] = interval
    return Hello(session, counter, registry_seq, version, refresh_ms, part, parts, intervals)


def encode_frame(boot: int, seq: int, batch: bytes, key: bytes | None) -> bytes:
    """Wrap one serialized SampleBatch as a FRAME datagram."""
    return _seal(_PREFIX.pack(MAGIC, TYPE_FRAME, VERSION) + _FRAME.pack(boot, seq) + batch, key)


def decode_frame(datagram: bytes, key: bytes | None) -> tuple[int, int, bytes]:
    """Return (boot, seq, serialized SampleBatch); raises WireError otherwise."""
    body = _open(datagram, key, TYPE_FRAME)
    if len(body) < _FRAME.size:
        raise WireError("short frame")
    boot, seq = _FRAME.unpack_from(body)
    return boot, seq, bytes(body[_FRAME.size :])


def frame_overhead(key: bytes | None) -> int:
    """Bytes a FRAME adds around its SampleBatch."""
    return _PREFIX.size + _FRAME.size + (MAC_BYTES if key else 0)
