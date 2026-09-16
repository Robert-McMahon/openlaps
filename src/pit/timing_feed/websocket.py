"""Just enough of RFC 6455 to accept one client on one path.

The Timing71 standalone server speaks plain JSON text frames over a
WebSocket; accepting them needs the opening handshake and the server side
of the framing (client frames are masked, ours are not), plus ping, pong
and close. That is a hundred lines of the standard library, which is less
to keep in step than a dependency for one endpoint (phase ground rule: no
new dependency without a sentence -- this is the sentence).

Fragments are reassembled up to ``MAX_MESSAGE_BYTES``; anything larger, or
an unmasked client frame, is a protocol violation and the socket is
closed. Nothing here is a general WebSocket implementation: no extensions,
no subprotocols, no compression.
"""

from __future__ import annotations

import base64
import hashlib
import struct
from collections.abc import Iterator
from typing import BinaryIO

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE_BYTES = 1024 * 1024

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(ValueError):
    """The peer broke the framing rules."""


def accept_key(client_key: str) -> str:
    """``Sec-WebSocket-Accept`` for a ``Sec-WebSocket-Key``."""
    digest = hashlib.sha1((client_key.strip() + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_frame(opcode: int, payload: bytes = b"", *, mask: bytes | None = None) -> bytes:
    """One unfragmented frame; masked only when ``mask`` (four bytes) is given."""
    header = bytearray([0x80 | (opcode & 0x0F)])
    length = len(payload)
    mask_bit = 0x80 if mask is not None else 0
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header += struct.pack("!H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack("!Q", length)
    if mask is not None:
        if len(mask) != 4:
            raise ValueError("mask is four bytes")
        header += mask
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(header) + payload


def encode_close(code: int = 1000, reason: str = "") -> bytes:
    return encode_frame(OP_CLOSE, struct.pack("!H", code) + reason.encode("utf-8")[:120])


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise EOFError("websocket closed")
        data += chunk
    return data


def read_frame(stream: BinaryIO, *, require_mask: bool = True) -> tuple[bool, int, bytes]:
    """``(fin, opcode, payload)`` for the next frame; raises ``EOFError`` at close."""
    first, second = _read_exact(stream, 2)
    fin = bool(first & 0x80)
    if first & 0x70:
        raise WebSocketError("reserved bits set")
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", _read_exact(stream, 2))
    elif length == 127:
        (length,) = struct.unpack("!Q", _read_exact(stream, 8))
    if length > MAX_MESSAGE_BYTES:
        raise WebSocketError(f"frame of {length} bytes exceeds the cap")
    if require_mask and not masked:
        raise WebSocketError("client frame is not masked")
    mask = _read_exact(stream, 4) if masked else None
    payload = _read_exact(stream, length)
    if mask is not None:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload


def messages(stream: BinaryIO, out: BinaryIO) -> Iterator[str]:
    """Yield every complete text message; answer pings; stop on close.

    Control frames are handled inline. A binary message is decoded as text
    too -- some clients send JSON that way -- and anything not UTF-8 is a
    protocol error.
    """
    parts: list[bytes] = []
    size = 0
    while True:
        try:
            fin, opcode, payload = read_frame(stream)
        except EOFError:
            return
        if opcode == OP_CLOSE:
            try:
                out.write(encode_close())
                out.flush()
            except OSError:
                pass
            return
        if opcode == OP_PING:
            out.write(encode_frame(OP_PONG, payload))
            out.flush()
            continue
        if opcode == OP_PONG:
            continue
        if opcode in (OP_TEXT, OP_BINARY):
            if parts:
                raise WebSocketError("new message while a fragmented one is open")
            parts = [payload]
            size = len(payload)
        elif opcode == OP_CONTINUATION:
            if not parts:
                raise WebSocketError("continuation without a message")
            parts.append(payload)
            size += len(payload)
        else:
            raise WebSocketError(f"unknown opcode {opcode}")
        if size > MAX_MESSAGE_BYTES:
            raise WebSocketError("message exceeds the cap")
        if fin:
            data = b"".join(parts)
            parts = []
            size = 0
            try:
                yield data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise WebSocketError("message is not UTF-8") from exc
