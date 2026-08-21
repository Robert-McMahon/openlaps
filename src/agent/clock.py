"""System-clock wall mapping for monotonic capture timestamps.

``t_mono_ns`` remains the ordering and interval truth.  The host system clock,
disciplined externally by chrony, is the only wall-time authority; this module
samples realtime and monotonic together on every conversion so chrony's runtime
slew is observed without putting a second steering loop in the agent.
"""

from __future__ import annotations

import time


class SystemClock:
    """Project monotonic timestamps onto the current system realtime clock."""

    __slots__ = ()

    def __call__(self, t_mono_ns: int) -> float:
        """Return wall milliseconds for ``t_mono_ns`` using a fresh correlation."""
        realtime_ns = time.time_ns()
        monotonic_ns = time.monotonic_ns()
        return (realtime_ns + t_mono_ns - monotonic_ns) / 1e6
