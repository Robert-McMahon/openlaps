"""Detect a socketCAN interface that is counting frames nobody receives.

On 2026-11-08 the Luckfox Omni3576's `rk3576_canfd` controller sat in an
error state for hours: the kernel's `rx_packets` counter for `can0` kept
rising at about 100 frames/s while not one frame was delivered to any
socket -- the controller was emitting bare bus-error frames and no data at
all. The link state went to ERROR-WARNING and stayed there. Taking the
interface down to recover it hung inside the driver holding the rtnl lock,
which locked out every new SSH login until the board was power cycled.

That signature is the whole detector: **the kernel's frame count rises while
our own delivered-frame count stays flat.** A quiet bus (ignition off) stops
both counters together, so it never looks like a stall. A bus with no
transceiver, or an interface that is not there yet, has no kernel counter to
rise. Only a controller that is seeing *something* and passing on *nothing*
trips it.

Two consumers share this module and must agree on the definition:

- the agent, which reports it as `sys.agent.stall_s.<bus>` so the pit sees
  the stall in Grafana and `alarms.yaml` can page the crew;
- `tools/can_watchdog.py`, which runs as root on the board, counts frames on
  its own socket, and reboots the box when the stall persists. It does not
  bounce the interface, because that is what wedged the driver.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_SYSFS_ROOT = Path("/sys")


def read_rx_packets(interface: str, sysfs_root: Path = DEFAULT_SYSFS_ROOT) -> int | None:
    """The kernel's receive counter for `interface`, or None if unreadable.

    `/sys/class/net/<if>/statistics/rx_packets` counts every frame the
    controller handed the kernel, error frames included, whether or not any
    socket wanted it. None means "cannot judge": the interface is absent or
    sysfs is not where it is on Linux. Callers treat that as not stalled.
    """
    try:
        return int((sysfs_root / "class/net" / interface / "statistics/rx_packets").read_text())
    except (OSError, ValueError):
        return None


@dataclass(slots=True)
class StallDetector:
    """Judges one interface from successive (kernel, delivered) counter pairs.

    Feed it one observation per poll and read back how long the stall has
    lasted, in seconds, or 0.0 when the bus is healthy or quiet. The first
    observation only establishes a baseline. A stall ends the moment a frame
    is delivered or the kernel counter stops rising, so a transient burst of
    error frames clears itself.
    """

    _last_kernel: int | None = None
    _last_delivered: int | None = None
    _last_time: float | None = None
    _stalled_since: float | None = None

    @property
    def stalled(self) -> bool:
        """Whether the most recent observation was part of a stall."""
        return self._stalled_since is not None

    def reset(self) -> None:
        """Forget everything, e.g. after the socket was reopened."""
        self._last_kernel = None
        self._last_delivered = None
        self._last_time = None
        self._stalled_since = None

    def observe(self, kernel_rx: int | None, delivered: int, now: float) -> float:
        """Record one poll; return the stall duration in seconds (0.0 if none)."""
        if kernel_rx is None:
            # No counter to compare against: an absent interface is a
            # different failure, reported by the collector, not a stall.
            self.reset()
            return 0.0
        if self._last_kernel is not None and self._last_delivered is not None:
            kernel_rose = kernel_rx > self._last_kernel
            delivered_rose = delivered > self._last_delivered
            if delivered_rose or not kernel_rose:
                self._stalled_since = None
            elif self._stalled_since is None:
                # The frames went missing somewhere between the last poll and
                # this one; date the stall from the last poll so one interval
                # of silence reads as one interval, not zero.
                self._stalled_since = self._last_time if self._last_time is not None else now
        self._last_kernel = kernel_rx
        self._last_delivered = delivered
        self._last_time = now
        return 0.0 if self._stalled_since is None else max(0.0, now - self._stalled_since)
