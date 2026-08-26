"""Race-format display strings for the pit's live timing payloads.

Grafana has no race-timing unit: its ``s`` unit renders 68.5 as
"1.13 min" and 58.31 as "58.3 s", neither of which is how anyone on a
pit wall reads a lap time. There is also no dashboard-side way to build
"1:08.5" out of a numeric field, so the string has to travel in the
payload. The two MQTT publishers (live-decoder and the timing
extrapolator) attach a pre-formatted ``display`` field alongside the
numeric ``value``, and the timing panels show that field. The number
stays in the payload untouched for anything that computes.

The resolution rule: a *running* clock is read in tenths; a
*definitive* time -- a completed lap or sector, announced once -- is
shown to the millisecond.
"""

from __future__ import annotations

import math

# Vehicle-owned channels the live-decoder republishes verbatim. Running
# clocks and predictions read in tenths; completed times in milliseconds.
_LIVE_TENTHS = frozenset({"timing.lap_elapsed", "timing.predicted_lap"})
_DEFINITIVE_MILLIS = frozenset({"lap.last_time", "lap.best_time"})
_SIGNED_TENTHS = frozenset({"timing.delta_best"})

# What a gated pit clock shows. A dash is a labelled absence; the old
# behaviour -- the panel holding the last non-null number -- looked like
# a working clock exactly when the clock had lost its authority.
GATED_DISPLAY = "—"


def lap_time_display(seconds: float, *, decimals: int) -> str:
    """Format a duration in seconds as m:ss with the given decimals.

    58.31 -> "0:58.3" (decimals=1); 68.5 -> "1:08.500" (decimals=3).
    Rounding happens before the minute split so 59.96 becomes "1:00.0"
    rather than the impossible "0:60.0".
    """
    sign = "-" if seconds < 0 else ""
    scale = 10**decimals
    total = round(abs(seconds) * scale)
    minutes, rest = divmod(total, 60 * scale)
    return f"{sign}{minutes}:{rest / scale:0{3 + decimals}.{decimals}f}"


def display_for(channel: str, value: object) -> str | None:
    """Display string for a vehicle-owned timing channel, else None.

    Non-numeric and non-finite values return None: the caller's JSON
    encoding is the single place that decides what to do with those.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    if channel in _LIVE_TENTHS:
        return lap_time_display(float(value), decimals=1)
    if channel in _DEFINITIVE_MILLIS:
        return lap_time_display(float(value), decimals=3)
    if channel in _SIGNED_TENTHS:
        return f"{float(value):+.1f}"
    return None


def pit_clock_display(value: float | None, status: str) -> str:
    """Display string for an extrapolated pit clock value.

    The authoritative value announced at a crossing is the definitive
    time and gets milliseconds; the running extrapolation (and the
    runaway-bounded degraded value) is a clock and gets tenths. A gated
    clock has no value and says so.
    """
    if value is None:
        return GATED_DISPLAY
    return lap_time_display(value, decimals=3 if status == "authoritative" else 1)
