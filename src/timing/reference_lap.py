"""
reference_lap — best-lap distance→time curve and live delta.

Pure, side-effect free. During each lap the caller records
(lap_distance, elapsed_lap_time) samples; when a lap completes *valid* and
faster than the current reference, the recorded curve becomes the new
reference. While a lap is in progress the live delta versus the reference is:

    delta_best   = elapsed_now − reference.time_at(lap_distance_now)
    predicted    = reference.lap_time + delta_best

(negative delta = ahead of the best lap). Curves are decimated onto a distance
grid so a persisted reference stays small (~500 points for a 2.4 km track).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field


@dataclass
class ReferenceLap:
    """Monotonic distance→elapsed-time curve for one completed lap."""

    distances: list[float]  # metres from lap start, strictly increasing
    times: list[float]  # elapsed seconds at each distance
    lap_time: float  # official lap time (s)

    def time_at(self, distance: float) -> float:
        """Elapsed time on the reference lap at `distance` (linear interp)."""
        d, t = self.distances, self.times
        if not d:
            return 0.0
        if distance <= d[0]:
            return t[0]
        if distance >= d[-1]:
            return t[-1]
        i = bisect.bisect_right(d, distance)
        span = d[i] - d[i - 1]
        frac = (distance - d[i - 1]) / span if span > 0 else 0.0
        return t[i - 1] + frac * (t[i] - t[i - 1])

    def to_dict(self) -> dict:
        return {
            "distances": [round(x, 2) for x in self.distances],
            "times": [round(x, 3) for x in self.times],
            "lap_time": round(self.lap_time, 3),
        }

    @classmethod
    def from_dict(cls, d: dict) -> ReferenceLap:
        return cls(
            distances=list(d.get("distances", [])),
            times=list(d.get("times", [])),
            lap_time=float(d.get("lap_time", 0.0)),
        )


@dataclass
class LapRecorder:
    """Collects (distance, elapsed) samples for the lap in progress."""

    grid_m: float = 5.0  # decimation grid (metres)
    _dist: list[float] = field(default_factory=list)
    _time: list[float] = field(default_factory=list)

    def reset(self) -> None:
        self._dist.clear()
        self._time.clear()

    def add(self, distance: float, elapsed: float) -> None:
        """Record a sample; keeps at most one sample per grid bin."""
        if self._dist:
            if distance <= self._dist[-1]:  # must stay monotonic
                return
            if distance - self._dist[-1] < self.grid_m:
                return
        self._dist.append(distance)
        self._time.append(elapsed)

    def complete(self, lap_time: float) -> ReferenceLap | None:
        """Close the lap; returns the curve (or None if too sparse)."""
        if len(self._dist) < 10:
            return None
        return ReferenceLap(
            distances=list(self._dist),
            times=list(self._time),
            lap_time=lap_time,
        )


class DeltaTracker:
    """Live delta vs a reference lap."""

    def __init__(self, reference: ReferenceLap | None = None):
        self.reference = reference

    def offer(self, candidate: ReferenceLap | None, valid: bool) -> bool:
        """Adopt `candidate` if it is a valid lap faster than the reference."""
        if candidate is None or not valid or candidate.lap_time <= 0:
            return False
        if self.reference is None or candidate.lap_time < self.reference.lap_time:
            self.reference = candidate
            return True
        return False

    def delta(self, distance: float, elapsed: float) -> float | None:
        if self.reference is None:
            return None
        return elapsed - self.reference.time_at(distance)

    def predicted(self, distance: float, elapsed: float) -> float | None:
        d = self.delta(distance, elapsed)
        if d is None:
            return None
        return self.reference.lap_time + d
