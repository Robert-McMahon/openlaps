"""Our own car, reconciled: the feed's count against ours, and the two clocks.

The car number from the race plan (P7.8) identifies us in the feed. Two
independent systems count our laps -- the timekeepers' transponder loop and
the vehicle's GPS start line -- and when they disagree by more than a lap
one of them has missed something. Whose count is higher says which: the
feed ahead means the vehicle missed a line crossing (a GPS dropout, a
track file with the line in the wrong place); the vehicle ahead means the
transponder was not seen (a dead battery, a loop fault, a wrong number in
the plan). Either is a ``field.lap_count`` finding, and it alerts through
the same chain as every strategy warning.

The ``Passing`` stream makes the same comparison sharper: our transponder
crossing the main line and our GPS crossing the start line are two
timestamps for one event, and their offset is the vehicle clock measured
against the timekeepers'.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

MONITOR_LAP_COUNT = "field.lap_count"
MONITORS = (MONITOR_LAP_COUNT,)


@dataclass(frozen=True, slots=True)
class Finding:
    """A ``watch_findings`` row under the ``field.`` namespace."""

    monitor: str
    severity: str
    score: float
    summary: dict[str, object]


def lap_count_finding(
    car_number: str,
    feed_laps: int | None,
    vehicle_laps: int,
    *,
    tolerance: int = 1,
) -> Finding | None:
    """A finding when the two counts disagree by more than ``tolerance``."""
    if feed_laps is None:
        return None
    delta = feed_laps - vehicle_laps
    if abs(delta) <= tolerance:
        return None
    if delta > 0:
        cause = "vehicle timing missed a line crossing"
        which = "feed"
    else:
        cause = "transponder not seen by the timekeepers"
        which = "vehicle"
    return Finding(
        monitor=MONITOR_LAP_COUNT,
        severity="warning" if abs(delta) <= 3 else "critical",
        score=float(abs(delta)),
        summary={
            "message": (
                f"car {car_number}: feed says {feed_laps} laps, vehicle says {vehicle_laps} "
                f"({which} is higher: {cause})"
            ),
            "car_number": car_number,
            "feed_laps": feed_laps,
            "vehicle_laps": vehicle_laps,
            "delta": delta,
            "higher": which,
            "likely_cause": cause,
        },
    )


def clock_offset_s(
    passing_tod: datetime, crossings: list[datetime], *, window_s: float = 30.0
) -> float | None:
    """Vehicle clock minus timekeepers' clock, from the nearest crossing to a passing.

    Positive means the vehicle stamped its crossing later than the
    timekeepers stamped theirs. ``None`` when no crossing is within the
    window: the passing may be a pit-lane main-line crossing the vehicle
    recorded as something else, or a lap the vehicle missed.
    """
    best: float | None = None
    for crossed_at in crossings:
        offset = (crossed_at - passing_tod).total_seconds()
        if abs(offset) <= window_s and (best is None or abs(offset) < abs(best)):
            best = offset
    return None if best is None else round(best, 3)
