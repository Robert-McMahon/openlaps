"""Timestamp plumbing shared by every collector.

Collectors stamp `t_mono_ns` themselves (``docs/AGENT_DESIGN.md`` -> Clock
discipline: monotonic is the ordering truth) and derive `t_wall_ms` from a
mapping the agent supplies. These are the types that contract is expressed in,
plus the standalone mapping used when no agent is wired up.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from core.samples import Sample

WallClock = Callable[[int], float]
"""Maps ``t_mono_ns`` onto wall-clock milliseconds."""

Emit = Callable[[Sample], None]
"""Hands one captured sample to the pipeline; collectors own no queue."""


class MonotonicWallClock:
    """Fixed-offset monotonic-to-wall mapping, anchored at construction.

    Collectors only need *a* mapping; the agent supplies its GNSS-steered one
    (``docs/AGENT_DESIGN.md`` -> Clock discipline) in production.
    """

    __slots__ = ("_offset_ms",)

    def __init__(self) -> None:
        self._offset_ms = time.time() * 1000.0 - time.monotonic_ns() / 1e6

    def __call__(self, t_mono_ns: int) -> float:
        """Return the wall-clock milliseconds corresponding to ``t_mono_ns``."""
        return self._offset_ms + t_mono_ns / 1e6
