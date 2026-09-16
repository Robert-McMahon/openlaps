#!/usr/bin/env python3
"""Discipline chrony from a GNSS PPS wire on a GPIO, with no kernel driver.

The normal way to do this is `pps-gpio`: a device-tree node, a `/dev/pps0`,
and `refclock PPS /dev/pps0` in chrony.conf. On the Luckfox Omni3576 that
route costs a boot-partition reflash -- the kernel has every PPS client
compiled out, no module tree at all, and neither `CONFIG_OF_OVERLAY` nor
`CONFIG_OF_DYNAMIC`, so a DT node cannot be added at runtime
(`deploy/targets/luckfox-omni3576/README.md` has the measurements).

This gets the same signal without touching the kernel, and the reason it is
not a worse answer than it sounds is where the timestamp comes from.
gpiolib's character device stamps an edge **in the hard IRQ handler** --
`edge_irq_handler()` stores it there with the comment "so we get it as close
in time as possible to the actual event" -- and hands it to userspace already
stamped. Python's scheduling latency therefore affects when this process
*learns* about an edge, and not the time it reports for it. What is left is
IRQ latency, which is the same thing `pps-gpio` would be exposed to.

**No NMEA, and that is deliberate.** A PPS edge says *when* a second starts,
never *which* second it is, so a kernel PPS refclock is paired with a coarse
source (`refclock PPS /dev/pps0 lock NMEA`). This shim takes the coarse
second from the system clock instead, by rounding the edge's own realtime
timestamp -- valid exactly while the clock is already inside +/-0.5 s, which
is what chrony's NTP sources guarantee and what `--max-offset` enforces
sample by sample. That matters on this board specifically: it has one usable
UART, the agent owns it, and there is no second port to feed a timing reader.

    sudo tools/pps_gpio_shim.py --chip /dev/gpiochip3 --line 2

Samples go to chrony's SOCK refclock in the same `struct sock_sample` the
RP2040 shim beside this one sends, through the same socket contract
(`deploy/systemd/chrony-openlaps-sock.conf`). Chrony cannot tell the two
apart, which is the point: `docs/BENCH_RUNBOOK.md`'s comparison can put this
against a timing head on the same box and read one number.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import logging
import os
import select
import struct
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chrony_sock import SOCK_SAMPLE, ChronySocket, pack_sock_sample  # noqa: E402

logger = logging.getLogger("pps-gpio-shim")

# include/uapi/linux/gpio.h, the v2 ABI (kernel 5.10+). Spelled out rather
# than taken from libgpiod because Debian 12 ships libgpiod 1.6, whose v1
# ABI reports CLOCK_MONOTONIC timestamps -- converting those to realtime
# would add exactly the error this tool exists to avoid. v2's
# EVENT_CLOCK_REALTIME gives the kernel's own realtime stamp directly.
GPIO_MAX_NAME_SIZE = 32
GPIO_V2_LINES_MAX = 64
GPIO_V2_LINE_NUM_ATTRS_MAX = 10

GPIO_V2_LINE_FLAG_INPUT = 1 << 2
GPIO_V2_LINE_FLAG_EDGE_RISING = 1 << 4
GPIO_V2_LINE_FLAG_EDGE_FALLING = 1 << 5
GPIO_V2_LINE_FLAG_BIAS_PULL_UP = 1 << 8
GPIO_V2_LINE_FLAG_BIAS_PULL_DOWN = 1 << 9
GPIO_V2_LINE_FLAG_BIAS_DISABLED = 1 << 10
GPIO_V2_LINE_FLAG_EVENT_CLOCK_REALTIME = 1 << 11

BIAS_FLAGS = {
    "as-is": 0,
    "disabled": GPIO_V2_LINE_FLAG_BIAS_DISABLED,
    "pull-up": GPIO_V2_LINE_FLAG_BIAS_PULL_UP,
    "pull-down": GPIO_V2_LINE_FLAG_BIAS_PULL_DOWN,
}
EDGE_FLAGS = {
    "rising": GPIO_V2_LINE_FLAG_EDGE_RISING,
    "falling": GPIO_V2_LINE_FLAG_EDGE_FALLING,
}

# struct gpio_v2_line_event: __u64 timestamp_ns; __u32 id, offset, seqno,
# line_seqno; __u32 padding[6].
LINE_EVENT = struct.Struct("=QIIII24s")
GPIO_V2_LINE_EVENT_RISING_EDGE = 1
GPIO_V2_LINE_EVENT_FALLING_EDGE = 2
EVENT_IDS = {"rising": GPIO_V2_LINE_EVENT_RISING_EDGE, "falling": GPIO_V2_LINE_EVENT_FALLING_EDGE}

NS_PER_S = 1_000_000_000
DEFAULT_MAX_OFFSET_S = 0.2
DEFAULT_CONSUMER = "openlaps-pps"


class _LineAttribute(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32), ("padding", ctypes.c_uint32), ("value", ctypes.c_uint64)]


class _LineConfigAttribute(ctypes.Structure):
    _fields_ = [("attr", _LineAttribute), ("mask", ctypes.c_uint64)]


class _LineConfig(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("num_attrs", ctypes.c_uint32),
        ("padding", ctypes.c_uint32 * 5),
        ("attrs", _LineConfigAttribute * GPIO_V2_LINE_NUM_ATTRS_MAX),
    ]


class _LineRequest(ctypes.Structure):
    _fields_ = [
        ("offsets", ctypes.c_uint32 * GPIO_V2_LINES_MAX),
        ("consumer", ctypes.c_char * GPIO_MAX_NAME_SIZE),
        ("config", _LineConfig),
        ("num_lines", ctypes.c_uint32),
        ("event_buffer_size", ctypes.c_uint32),
        ("padding", ctypes.c_uint32 * 5),
        ("fd", ctypes.c_int32),
    ]


def _iowr(type_: int, nr: int, size: int) -> int:
    """Linux `_IOWR`, for the one ioctl this tool issues."""
    return (3 << 30) | (size << 16) | (type_ << 8) | nr


GPIO_V2_GET_LINE_IOCTL = _iowr(0xB4, 0x07, ctypes.sizeof(_LineRequest))


@dataclass(slots=True)
class PpsStats:
    """Counters, in the same spirit as the RP2040 shim's."""

    accepted: int = 0
    out_of_range: int = 0
    wrong_edge: int = 0
    sink_errors: int = 0
    device_errors: int = 0


def open_line(
    chip: str,
    line: int,
    *,
    edge: str = "rising",
    bias: str = "as-is",
    consumer: str = DEFAULT_CONSUMER,
    event_buffer_size: int = 16,
) -> int:
    """Request `line` for edge events and return the request file descriptor.

    The returned fd is the *line request*, not the chip: closing it releases
    the line, and reading it yields `struct gpio_v2_line_event` records.
    """
    request = _LineRequest()
    request.offsets[0] = line
    request.num_lines = 1
    request.consumer = consumer.encode("ascii")[: GPIO_MAX_NAME_SIZE - 1]
    request.event_buffer_size = event_buffer_size
    request.config.flags = (
        GPIO_V2_LINE_FLAG_INPUT
        | GPIO_V2_LINE_FLAG_EVENT_CLOCK_REALTIME
        | EDGE_FLAGS[edge]
        | BIAS_FLAGS[bias]
    )

    chip_fd = os.open(chip, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.ioctl(chip_fd, GPIO_V2_GET_LINE_IOCTL, request)
    finally:
        os.close(chip_fd)
    if request.fd < 0:
        raise OSError(errno.EIO, f"{chip}: kernel returned no line fd for offset {line}")
    return request.fd


def parse_events(buffer: bytes) -> list[tuple[int, int, int]]:
    """Split a read into ``(timestamp_ns, event id, line seqno)`` triples."""
    events = []
    for offset in range(0, len(buffer) - LINE_EVENT.size + 1, LINE_EVENT.size):
        timestamp_ns, event_id, _line, _seqno, line_seqno, _pad = LINE_EVENT.unpack_from(
            buffer, offset
        )
        events.append((timestamp_ns, event_id, line_seqno))
    return events


class PpsShim:
    """Turn edge timestamps into chrony samples, or into a counter."""

    def __init__(
        self,
        sink: Callable[[bytes], object],
        *,
        max_offset_s: float = DEFAULT_MAX_OFFSET_S,
        edge: str = "rising",
    ) -> None:
        self._sink = sink
        self._max_offset_s = max_offset_s
        self._expected_id = EVENT_IDS[edge]
        self.stats = PpsStats()

    def process_edge(
        self, timestamp_ns: int, event_id: int = GPIO_V2_LINE_EVENT_RISING_EDGE
    ) -> bool:
        """Submit one edge; False means it was rejected, and why is a counter."""
        if event_id != self._expected_id:
            self.stats.wrong_edge += 1
            return False

        # Which second this edge *is* comes from the clock we are correcting,
        # which is only sound while that clock is already close. Past the
        # guard the rounding picks a neighbouring second and the sample would
        # be a confident lie -- a full second of error -- so it is dropped
        # rather than sent. A shim that never accepts anything is visible in
        # `out_of_range`; a shim that sends this is a stepped clock.
        utc_second, remainder_ns = divmod(timestamp_ns + NS_PER_S // 2, NS_PER_S)
        offset_s = (NS_PER_S // 2 - remainder_ns) / NS_PER_S
        if abs(offset_s) > self._max_offset_s:
            self.stats.out_of_range += 1
            return False

        try:
            self._sink(pack_sock_sample(utc_second, timestamp_ns))
        except OSError:
            self.stats.sink_errors += 1
            return False
        self.stats.accepted += 1
        return True


class _EdgeLogger:
    """A sink that reports each sample instead of sending it anywhere.

    `--dry-run` is the first thing to run after wiring a receiver, before
    chronyd is in the picture at all: it answers "is the pin the one I think
    it is, and is the receiver actually pulsing" with no socket, no unit and
    no root beyond reading the GPIO chip.
    """

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, sample: bytes) -> None:
        tv_sec, tv_usec, offset_s, _, _, _, _ = SOCK_SAMPLE.unpack(sample)
        self.count += 1
        logger.info(
            "edge %d at %d.%06d, offset %+.6f s (not sent: --dry-run)",
            self.count,
            tv_sec,
            tv_usec,
            offset_s,
        )


def run(
    chip: str,
    line: int,
    chrony_socket: str,
    *,
    edge: str = "rising",
    bias: str = "as-is",
    max_offset_s: float = DEFAULT_MAX_OFFSET_S,
    reconnect_s: float = 1.0,
    dry_run: bool = False,
    stop: Callable[[], bool] | None = None,
) -> None:
    """Read edges forever, degrading a missing line to silence and retries."""
    destination = None if dry_run else ChronySocket(chrony_socket)
    sink = _EdgeLogger() if destination is None else destination.send
    shim = PpsShim(sink, max_offset_s=max_offset_s, edge=edge)
    keep_going = (lambda: True) if stop is None else (lambda: not stop())
    try:
        while keep_going():
            fd = None
            try:
                fd = open_line(chip, line, edge=edge, bias=bias)
                logger.info("watching %s line %d for %s edges", chip, line, edge)
                while keep_going():
                    readable, _, _ = select.select([fd], [], [], 1.0)
                    if not readable:
                        continue
                    buffer = os.read(fd, LINE_EVENT.size * 16)
                    for timestamp_ns, event_id, _seqno in parse_events(buffer):
                        shim.process_edge(timestamp_ns, event_id)
            except OSError as exc:
                shim.stats.device_errors += 1
                logger.warning("%s line %d unavailable: %s", chip, line, exc)
                time.sleep(reconnect_s)
            finally:
                if fd is not None:
                    os.close(fd)
    finally:
        if destination is not None:
            destination.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--chip", default="/dev/gpiochip3", help="GPIO chip device")
    parser.add_argument("--line", type=int, required=True, help="line offset within the chip")
    parser.add_argument("--edge", choices=sorted(EDGE_FLAGS), default="rising")
    parser.add_argument(
        "--bias",
        choices=sorted(BIAS_FLAGS),
        default="as-is",
        help="internal bias; leave as-is for a receiver that drives the line",
    )
    parser.add_argument(
        "--max-offset",
        type=float,
        default=DEFAULT_MAX_OFFSET_S,
        dest="max_offset_s",
        help=(
            "reject an edge implying a larger correction (default: %(default)s s). "
            "Widen it toward 0.5 for a first look at an undisciplined clock"
        ),
    )
    parser.add_argument("--chrony-socket", default="/run/chrony/openlaps-timing.sock")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log each edge and its offset instead of sending it to chrony",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper())
    try:
        run(
            args.chip,
            args.line,
            args.chrony_socket,
            edge=args.edge,
            bias=args.bias,
            max_offset_s=args.max_offset_s,
            dry_run=args.dry_run,
        )
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
