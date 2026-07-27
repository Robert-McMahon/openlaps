"""GNSS-steered monotonic-to-wall clock mapping for the vehicle agent.

``docs/AGENT_DESIGN.md`` -> Clock discipline: ``t_mono_ns`` is the ordering
and interval truth everywhere; the wall clock maps from monotonic via an
affine offset initialised from the system clock. When a GNSS time source is
present among the mapped channels the offset is steered gently toward GPS
time — slewed, never stepped, while running; a step is allowed only during
startup, before the agent begins publishing.
"""

from __future__ import annotations

import time

SOURCE_SYSTEM = "system"
SOURCE_GNSS = "gnss"

DEFAULT_MAX_SLEW_PPM = 500.0


class SteeredClock:
    """Affine monotonic-to-wall mapping with GNSS slewing.

    Single-writer: only one thread (the pipeline) may call ``observe_gnss``
    and ``mark_running``. Reads (``__call__`` and the state properties) are
    safe from any thread — the offset is one float assignment.
    """

    __slots__ = ("_max_slew_ppm", "_offset_ms", "_running", "_source", "_t_last_observe_ns")

    def __init__(self, *, max_slew_ppm: float = DEFAULT_MAX_SLEW_PPM) -> None:
        if max_slew_ppm <= 0:
            raise ValueError("max_slew_ppm must be positive")
        self._max_slew_ppm = max_slew_ppm
        self._offset_ms = time.time() * 1000.0 - time.monotonic_ns() / 1e6
        self._source = SOURCE_SYSTEM
        self._running = False
        self._t_last_observe_ns: int | None = None

    def __call__(self, t_mono_ns: int) -> float:
        """Return the wall-clock milliseconds corresponding to ``t_mono_ns``."""
        return self._offset_ms + t_mono_ns / 1e6

    @property
    def offset_ms(self) -> float:
        """Current monotonic-to-wall offset (feeds ``sys.agent.clock_offset_ms``)."""
        return self._offset_ms

    @property
    def source(self) -> str:
        """Where the offset came from (feeds ``sys.agent.clock_source``)."""
        return self._source

    def mark_running(self) -> None:
        """Close the startup window: from here on GNSS observations only slew.

        The very first GNSS observation still steps regardless: the vehicle
        has no RTC guarantee, so initial acquisition *is* the startup
        correction for the wall mapping — slewing away a potentially huge
        system-clock error at ppm rates would take hours for no benefit.
        """
        self._running = True

    def observe_gnss(self, t_mono_ns: int, gnss_unix_ms: float) -> None:
        """Steer the mapping toward a GNSS time fix captured at ``t_mono_ns``."""
        target_offset_ms = gnss_unix_ms - t_mono_ns / 1e6
        if not self._running or self._t_last_observe_ns is None:
            self._offset_ms = target_offset_ms
            self._source = SOURCE_GNSS
            self._t_last_observe_ns = t_mono_ns
            return

        previous_ns = self._t_last_observe_ns
        self._t_last_observe_ns = t_mono_ns
        error_ms = target_offset_ms - self._offset_ms
        if previous_ns is None or t_mono_ns <= previous_ns:
            # No usable interval to bound a slew against; hold the offset and
            # let the next observation correct it.
            self._source = SOURCE_GNSS
            return
        elapsed_ms = (t_mono_ns - previous_ns) / 1e6
        max_adjust_ms = self._max_slew_ppm * 1e-6 * elapsed_ms
        adjust_ms = max(-max_adjust_ms, min(max_adjust_ms, error_ms))
        self._offset_ms += adjust_ms
        self._source = SOURCE_GNSS
