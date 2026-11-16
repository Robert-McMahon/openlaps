"""Stamp NMEA sentences at the receiver's time of fix, not at arrival.

A sentence reaches the agent some milliseconds after the instant it
describes: the receiver computes, queues the sentence behind others in the
same epoch, clocks it out at the baud rate, and the reader thread has to be
scheduled to pick it up. The fixed part of that is a latency consumers can
subtract. The variable part is not: measured on the car on 2026-11-13,
arrival stamps of a 50 Hz RMC stream jittered by 8 ms rms (20 ms epochs
arriving 3 to 37 ms apart). At 40 m/s that is 0.3 m of along-track error
on a fix the receiver rates at 1.5 cm, and the fusion filter rejected 60 %
of fixes as outliers because of it.

Every RMC, GGA and GST names its epoch (``hhmmss.ss`` UTC). The delay from
epoch to arrival is the fixed latency plus a jitter that is never negative,
so the smallest delay seen recently is the fixed latency, and

    stamp = epoch + smallest recent (arrival - epoch)

puts every sentence on the receiver's own clock while leaving the stamp in
the agent's monotonic timebase. Only differences are used, so it does not
matter whether the system clock is right, or set at all. The window is
short enough that drift between the two oscillators (tens of ppm at worst,
under 1 ppm once chrony is locked to the PPS) stays well under a
millisecond.

A sentence that arrives late (a held-off reader thread, a backlog) is
stamped when it was true, which is before it arrived. Sentences of one
epoch share one stamp.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

NS_PER_S = 1_000_000_000
DAY_S = 86_400
DEFAULT_WINDOW_S = 10.0


def time_of_day_s(field: str) -> float | None:
    """Seconds since UTC midnight from an NMEA ``hhmmss[.sss]`` field."""
    whole, _, fraction = field.partition(".")
    if len(whole) != 6 or not whole.isdigit() or (fraction and not fraction.isdigit()):
        return None
    hour, minute, second = int(whole[0:2]), int(whole[2:4]), int(whole[4:6])
    if hour > 23 or minute > 59 or second > 60:  # 60: a leap second
        return None
    return hour * 3600 + minute * 60 + second + (float("0." + fraction) if fraction else 0.0)


@dataclass(slots=True)
class FixTimeStats:
    restamped: int = 0
    resets: int = 0
    # Arrival minus stamp for the latest sentence: how late it was read,
    # beyond the fastest recent one.
    last_delay_ms: float = 0.0
    max_delay_ms: float = 0.0


class ReceiverClock:
    """Map receiver epochs onto the monotonic clock; not thread-safe."""

    def __init__(self, window_s: float = DEFAULT_WINDOW_S) -> None:
        self.stats = FixTimeStats()
        self._window_ns = round(window_s * NS_PER_S)
        # Monotonic queue of (arrival, arrival - epoch): offsets increase
        # front to back, so the front is the window's minimum.
        self._offsets: deque[tuple[int, int]] = deque()
        self._day_ns = 0
        self._last_tod_s: float | None = None
        self._last_epoch_ns: int | None = None
        self._last_stamp_ns: int | None = None

    def reset(self) -> None:
        """Forget the mapping (the receiver was reconnected or reconfigured)."""
        self._offsets.clear()
        self._day_ns = 0
        self._last_tod_s = self._last_epoch_ns = self._last_stamp_ns = None

    def stamp(self, tod_s: float, arrival_mono_ns: int) -> int:
        """Monotonic stamp for a sentence of epoch ``tod_s`` read at ``arrival``."""
        last_tod = self._last_tod_s
        if last_tod is not None and tod_s < last_tod:
            if last_tod - tod_s > DAY_S / 2:
                self._day_ns += DAY_S * NS_PER_S  # past midnight
            else:
                # The receiver's time went backwards: it lost and regained
                # its clock. Nothing learned before that still holds.
                self.reset()
                self.stats.resets += 1
        self._last_tod_s = tod_s
        epoch_ns = self._day_ns + round(tod_s * NS_PER_S)
        if epoch_ns == self._last_epoch_ns and self._last_stamp_ns is not None:
            # A later sentence of the same epoch (GGA after RMC): it was
            # queued behind the first, which is not a faster arrival.
            return min(self._last_stamp_ns, arrival_mono_ns)

        offset = arrival_mono_ns - epoch_ns
        offsets = self._offsets
        horizon = arrival_mono_ns - self._window_ns
        while offsets and offsets[0][0] < horizon:
            offsets.popleft()
        while offsets and offsets[-1][1] >= offset:
            offsets.pop()
        offsets.append((arrival_mono_ns, offset))

        stamp = epoch_ns + offsets[0][1]  # never after arrival
        if self._last_stamp_ns is not None and stamp < self._last_stamp_ns:
            # The minimum just dropped by more than an epoch (the sentences
            # before this one were all read late): keep stamps ordered. The
            # last stamp is no later than its own arrival, so nor is this.
            stamp = self._last_stamp_ns
        self._last_epoch_ns = epoch_ns
        self._last_stamp_ns = stamp
        delay_ms = (arrival_mono_ns - stamp) / 1e6
        self.stats.restamped += 1
        self.stats.last_delay_ms = delay_ms
        self.stats.max_delay_ms = max(self.stats.max_delay_ms, delay_ms)
        return stamp
