"""The Natsoft packet framing layer, reimplemented from its description.

A feed document is one or more packets. Each packet is a ten-byte ASCII
header -- the magic ``!@#``, a five-digit zero-padded payload length, a
previous-packet id and a next-packet id -- followed by exactly that many
payload bytes. ``=`` as the previous id opens a document and ``=`` as the
next id closes it; between them the ids chain through ``A``-``Z`` then
``a``-``z`` and wrap. The payload of a completed chain is one XML document,
UTF-8 with a Latin-1 fallback for the odd driver name.

This module is the protocol facts only (locked decision 6: the reference
client carries no licence, so nothing of its code is here). It is
incremental -- ``PacketFramer.feed`` takes whatever the socket delivered
and returns every document that completed -- and it encodes as well as
decodes, so tests and fixtures build byte streams from the same rules the
reader checks.
"""

from __future__ import annotations

MAGIC = b"!@#"
HEADER_LEN = 10
MAX_PAYLOAD = 99_999
TERMINAL = "="

_IDS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# A document longer than this is not timing data; it is a stream that lost
# its framing and is being read as one endless chain.
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024


class PacketError(ValueError):
    """The byte stream is not a chain of well-formed packets."""


def next_packet_id(packet_id: str) -> str:
    """The id that follows ``packet_id`` in the cycle ``A``..``Z``, ``a``..``z``."""
    try:
        index = _IDS.index(packet_id)
    except ValueError:
        raise PacketError(f"not a packet id: {packet_id!r}") from None
    return _IDS[(index + 1) % len(_IDS)]


def encode_packet(payload: bytes, previous: str, following: str) -> bytes:
    """One packet: header plus payload."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload of {len(payload)} bytes exceeds {MAX_PAYLOAD}")
    if len(previous) != 1 or len(following) != 1:
        raise ValueError("packet ids are single characters")
    header = MAGIC + f"{len(payload):05d}".encode("ascii") + previous.encode() + following.encode()
    return header + payload


def encode_document(
    document: str | bytes, *, chunk: int = MAX_PAYLOAD, first_id: str = "A"
) -> bytes:
    """The packet chain for one document, split into ``chunk``-byte payloads."""
    data = document.encode("utf-8") if isinstance(document, str) else document
    if chunk <= 0 or chunk > MAX_PAYLOAD:
        raise ValueError("chunk must be between 1 and MAX_PAYLOAD")
    parts = [data[i : i + chunk] for i in range(0, len(data), chunk)] or [b""]
    if len(parts) == 1:
        return encode_packet(parts[0], TERMINAL, TERMINAL)
    out = bytearray()
    previous = TERMINAL
    current = first_id
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        following = TERMINAL if last else current
        out += encode_packet(part, previous, following)
        previous = current
        current = next_packet_id(current)
    return bytes(out)


def decode_payload(data: bytes) -> str:
    """UTF-8, or Latin-1 when a byte sequence is not UTF-8."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


class PacketFramer:
    """Turns a byte stream into complete documents, one ``feed`` at a time.

    Any violation -- wrong magic, a length that is not five digits, a chain
    whose ids do not follow on -- raises ``PacketError`` and leaves the framer
    reset. The caller drops the connection and redials: there is no
    resynchronising inside a stream that has already lied once.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._parts: list[bytes] = []
        self._size = 0
        self._expect_previous = TERMINAL
        self._last_following: str | None = None

    def reset(self) -> None:
        """Forget every buffered byte and any half-assembled document."""
        self._buffer.clear()
        self._parts.clear()
        self._size = 0
        self._expect_previous = TERMINAL
        self._last_following = None

    @property
    def buffered(self) -> int:
        """Bytes received but not yet framed."""
        return len(self._buffer)

    def feed(self, data: bytes) -> list[str]:
        """Consume ``data``; return every document it completed, in order."""
        self._buffer += data
        documents: list[str] = []
        try:
            while True:
                packet = self._next_packet()
                if packet is None:
                    break
                previous, following, payload = packet
                document = self._chain(previous, following, payload)
                if document is not None:
                    documents.append(document)
        except PacketError:
            self.reset()
            raise
        return documents

    def _next_packet(self) -> tuple[str, str, bytes] | None:
        if len(self._buffer) < HEADER_LEN:
            return None
        header = bytes(self._buffer[:HEADER_LEN])
        if header[:3] != MAGIC:
            raise PacketError(f"bad magic {header[:3]!r}")
        digits = header[3:8]
        if not digits.isdigit():
            raise PacketError(f"bad length field {digits!r}")
        length = int(digits)
        if len(self._buffer) < HEADER_LEN + length:
            return None
        payload = bytes(self._buffer[HEADER_LEN : HEADER_LEN + length])
        del self._buffer[: HEADER_LEN + length]
        return chr(header[8]), chr(header[9]), payload

    def _chain(self, previous: str, following: str, payload: bytes) -> str | None:
        if previous != self._expect_previous:
            raise PacketError(
                f"broken chain: packet says previous {previous!r}, "
                f"expected {self._expect_previous!r}"
            )
        if following != TERMINAL:
            if following not in _IDS:
                raise PacketError(f"bad packet id {following!r}")
            # The first id in a chain is whatever the feed is up to; from then
            # on each next id must be the successor of the last one.
            if self._last_following is not None and following != next_packet_id(
                self._last_following
            ):
                raise PacketError(
                    f"broken chain: next id {following!r} does not follow {self._last_following!r}"
                )
        self._parts.append(payload)
        self._size += len(payload)
        if self._size > MAX_DOCUMENT_BYTES:
            raise PacketError(f"document exceeds {MAX_DOCUMENT_BYTES} bytes")
        if following == TERMINAL:
            document = decode_payload(b"".join(self._parts))
            self._parts.clear()
            self._size = 0
            self._expect_previous = TERMINAL
            self._last_following = None
            return document
        self._expect_previous = following
        self._last_following = following
        return None
