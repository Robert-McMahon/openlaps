"""Tell chrony which second it is, from the receiver's own RMC sentences.

A PPS edge says *when* a second starts and never *which* one. On a board
whose only other time source is internet NTP, that leaves the whole second
to the RTC whenever the car is offline: on 2026-11-10 a Luckfox booted with
PPS locked to microseconds and the clock a whole 12 s fast. chrony also
labels such an unpaired PPS "stratum 10" when it is first selected under
the `local` fallback, and it never recovers from that by itself.

The agent already owns the receiver's UART and reads every RMC, which
carries the UTC date and time of its fix. This module turns the
top-of-second RMC into one sample a second for a chrony `SOCK` refclock, so
chrony.conf can pair the two (`refclock PPS /dev/pps0 lock NMEA`). The
sample only has to land within a few hundred milliseconds -- it numbers the
pulse, the pulse does the timing.

The wire format and the sign of the offset are `tools/chrony_sock.py`'s;
`tests/test_gnss_time.py` holds the two byte-for-byte equal.
"""

from __future__ import annotations

import calendar
import logging
import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass

from collectors.serial.nmea import _validated_body

logger = logging.getLogger(__name__)

SOCK_MAGIC = 0x534F434B
# chrony's `struct sock_sample`, native layout: it crosses a unix socket to a
# local process. Kept identical to tools/chrony_sock.py by test.
SOCK_SAMPLE = struct.Struct("@lldiiii")

NS_PER_S = 1_000_000_000
# A receiver that has never had a fix reports its firmware's idea of "now",
# which is years stale. Nothing older than this module can be a real fix.
EARLIEST_PLAUSIBLE_UTC_S = calendar.timegm((2026, 1, 1, 0, 0, 0))
SEND_FAILURE_LOG_INTERVAL_S = 60.0

Sink = Callable[[bytes], object]


def pack_time_sample(utc_ns: int, arrival_realtime_ns: int) -> bytes:
    """One ``struct sock_sample``: the clock read `arrival`, the truth was `utc`.

    chrony's convention is that a positive offset means the local clock is
    behind.
    """
    tv_sec, remainder_ns = divmod(arrival_realtime_ns, NS_PER_S)
    offset_s = (utc_ns - arrival_realtime_ns) / NS_PER_S
    return SOCK_SAMPLE.pack(tv_sec, remainder_ns // 1000, offset_s, 0, 0, 0, SOCK_MAGIC)


def rmc_utc_second(line: bytes) -> int | None:
    """UTC epoch second of a valid top-of-second RMC, else ``None``.

    Only a sentence whose time has no fractional part is used: at 50 Hz the
    other 49 a second say nothing more, and one fixed phase keeps the
    serial latency a constant chrony.conf can subtract (`offset`).
    """
    if b"RMC," not in line[:8]:
        return None
    try:
        fields = _validated_body(line.decode("ascii").strip()).split(",")
        if len(fields) < 10 or fields[2] != "A":
            return None
        clock, date = fields[1], fields[9]
        whole, _, fraction = clock.partition(".")
        if len(whole) != 6 or len(date) != 6 or fraction.strip("0"):
            return None
        hour, minute, second = int(whole[0:2]), int(whole[2:4]), int(whole[4:6])
        day, month, two_digit_year = int(date[0:2]), int(date[2:4]), int(date[4:6])
        # GPS time starts in 1980 and an unfixed receiver reports dates from
        # there, so 80-99 are the last century, not 2080-2099.
        year = (1900 if two_digit_year >= 80 else 2000) + two_digit_year
        if not (hour < 24 and minute < 60 and second < 60 and 1 <= month <= 12 and 1 <= day <= 31):
            return None
        utc_s = calendar.timegm((year, month, day, hour, minute, second))
    except (UnicodeDecodeError, ValueError):
        return None
    return utc_s if utc_s >= EARLIEST_PLAUSIBLE_UTC_S else None


@dataclass(slots=True)
class GnssTimeStats:
    samples_sent: int = 0
    send_failures: int = 0
    # Arrival minus the second the sentence names, by the system clock. Once
    # chrony is locked this is the serial latency, i.e. the `offset` to put
    # on the SOCK refclock line.
    last_latency_ms: float | None = None


class ChronyNmeaFeed:
    """Send one time sample per top-of-second RMC to chrony's SOCK refclock.

    Never raises: chrony being absent, restarted or unwilling must not cost
    a single telemetry sample. Failures are counted and logged at most once
    a minute.
    """

    def __init__(self, socket_path: str, *, sink: Sink | None = None) -> None:
        self.socket_path = socket_path
        self.stats = GnssTimeStats()
        self._socket: socket.socket | None = None
        self._sink = sink if sink is not None else self._send
        self._next_failure_log_mono = 0.0
        self._announced = False

    def handle_line(self, line: bytes, arrival_realtime_ns: int) -> bool:
        """Offer one received line; True when it produced a sample."""
        utc_s = rmc_utc_second(line)
        if utc_s is None:
            return False
        utc_ns = utc_s * NS_PER_S
        self.stats.last_latency_ms = (arrival_realtime_ns - utc_ns) / 1e6
        try:
            self._sink(pack_time_sample(utc_ns, arrival_realtime_ns))
        except OSError as exc:
            self.stats.send_failures += 1
            now = time.monotonic()
            if now >= self._next_failure_log_mono:
                self._next_failure_log_mono = now + SEND_FAILURE_LOG_INTERVAL_S
                logger.warning(
                    "gnss time: cannot send to chrony at %s: %s (%d failures)",
                    self.socket_path,
                    exc,
                    self.stats.send_failures,
                )
            return False
        self.stats.samples_sent += 1
        if not self._announced:
            self._announced = True
            logger.info(
                "gnss time: feeding chrony at %s (first sample: clock %+.1f ms from RMC)",
                self.socket_path,
                self.stats.last_latency_ms,
            )
        return True

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def _send(self, sample: bytes) -> None:
        if self._socket is None:
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._socket.sendto(sample, self.socket_path)
