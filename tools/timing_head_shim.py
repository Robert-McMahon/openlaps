#!/usr/bin/env python3
"""Bridge RP2040 timing-head samples into chrony's SOCK refclock protocol."""

from __future__ import annotations

import argparse
import logging
import socket
import struct
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import serial

SOCK_MAGIC = 0x534F434B
MAX_FIRMWARE_DELAY_US = 900_000
UINT32_MASK = (1 << 32) - 1
SOCK_SAMPLE = struct.Struct("@lldiiii")
logger = logging.getLogger("timing-head-shim")


@dataclass(frozen=True, slots=True)
class TimingMessage:
    sequence: int
    utc_second: int
    edge_us: int
    edge_to_transmit_us: int
    valid: bool


@dataclass(slots=True)
class ShimStats:
    accepted: int = 0
    malformed: int = 0
    invalid: int = 0
    stale_sequence: int = 0
    sink_errors: int = 0
    device_errors: int = 0


def parse_message(line: str | bytes) -> TimingMessage:
    """Parse one versioned, whitespace-delimited timing-head message."""
    if isinstance(line, bytes):
        line = line.decode("ascii", errors="strict")
    fields = line.strip().split()
    if len(fields) != 6 or fields[0] != "TH1":
        raise ValueError("expected six-field TH1 message")
    sequence, utc_second, edge_us, delay_us, validity = map(int, fields[1:])
    if not 0 <= sequence <= UINT32_MASK:
        raise ValueError("sequence is outside uint32 range")
    if utc_second <= 0 or edge_us < 0 or delay_us < 0 or validity not in {0, 1}:
        raise ValueError("message field is outside its valid range")
    return TimingMessage(sequence, utc_second, edge_us, delay_us, bool(validity))


def _is_newer_sequence(sequence: int, previous: int) -> bool:
    difference = (sequence - previous) & UINT32_MASK
    return 0 < difference < (1 << 31)


def pack_sock_sample(utc_second: int, estimated_edge_realtime_ns: int) -> bytes:
    """Build chrony's native ``struct sock_sample`` as a full time sample."""
    tv_sec, remainder_ns = divmod(estimated_edge_realtime_ns, 1_000_000_000)
    tv_usec = remainder_ns // 1000
    # Subtract as integers before converting: epoch-scale floats otherwise
    # lose enough precision to add ~0.1 us of avoidable noise.
    offset_s = (utc_second * 1_000_000_000 - estimated_edge_realtime_ns) / 1_000_000_000.0
    return SOCK_SAMPLE.pack(tv_sec, tv_usec, offset_s, 0, 0, 0, SOCK_MAGIC)


class TimingHeadShim:
    """Validate timing-head lines and send only trustworthy chrony samples."""

    def __init__(self, sink: Callable[[bytes], object]) -> None:
        self._sink = sink
        self._last_sequence: int | None = None
        self.stats = ShimStats()

    def process_line(self, line: str | bytes, arrival_realtime_ns: int) -> bool:
        try:
            message = parse_message(line)
        except (UnicodeDecodeError, ValueError):
            self.stats.malformed += 1
            return False
        if not message.valid or message.edge_to_transmit_us > MAX_FIRMWARE_DELAY_US:
            self.stats.invalid += 1
            return False
        if self._last_sequence is not None and not _is_newer_sequence(
            message.sequence, self._last_sequence
        ):
            self.stats.stale_sequence += 1
            return False

        estimated_edge_ns = arrival_realtime_ns - message.edge_to_transmit_us * 1000
        sample = pack_sock_sample(message.utc_second, estimated_edge_ns)
        try:
            self._sink(sample)
        except OSError:
            self.stats.sink_errors += 1
            return False
        self._last_sequence = message.sequence
        self.stats.accepted += 1
        return True


class ChronySocket:
    """Unix datagram sender for a socket owned by chronyd."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

    def send(self, sample: bytes) -> None:
        self._socket.sendto(sample, self.path)

    def close(self) -> None:
        self._socket.close()


def run(device: str, baud: int, chrony_socket: str, *, reconnect_s: float = 1.0) -> None:
    """Read forever, degrading unplugged hardware and bad data to silence."""
    destination = ChronySocket(chrony_socket)
    shim = TimingHeadShim(destination.send)
    try:
        while True:
            try:
                with serial.Serial(device, baudrate=baud, timeout=2.0) as port:
                    while True:
                        line = port.readline(257)
                        arrival_ns = time.clock_gettime_ns(time.CLOCK_REALTIME)
                        if not line:
                            continue
                        if len(line) > 256 and not line.endswith((b"\n", b"\r")):
                            shim.stats.malformed += 1
                            continue
                        shim.process_line(line, arrival_ns)
            except (OSError, serial.SerialException) as exc:
                shim.stats.device_errors += 1
                logger.warning("timing head unavailable: %s", exc)
                time.sleep(reconnect_s)
    finally:
        destination.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="/dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--chrony-socket", default="/run/chrony/openlaps-timing.sock")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper())
    try:
        run(args.device, args.baud, args.chrony_socket)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
