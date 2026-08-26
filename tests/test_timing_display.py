"""Race-format display strings shared by the two live MQTT publishers."""

from __future__ import annotations

import math

from pit.timing_display import GATED_DISPLAY, display_for, lap_time_display, pit_clock_display


def test_lap_time_display_reads_like_a_stopwatch():
    assert lap_time_display(58.31, decimals=1) == "0:58.3"
    assert lap_time_display(68.46, decimals=1) == "1:08.5"
    assert lap_time_display(68.4996, decimals=3) == "1:08.500"
    assert lap_time_display(0.0, decimals=1) == "0:00.0"
    assert lap_time_display(600.0, decimals=1) == "10:00.0"


def test_rounding_carries_into_the_minute_instead_of_showing_sixty():
    assert lap_time_display(59.96, decimals=1) == "1:00.0"
    assert lap_time_display(119.9996, decimals=3) == "2:00.000"


def test_negative_durations_keep_their_sign():
    # Never expected from the timing engine, but a formatter that silently
    # mangles a bad input hides the bug that produced it.
    assert lap_time_display(-68.46, decimals=1) == "-1:08.5"


def test_display_for_covers_exactly_the_vehicle_timing_channels():
    assert display_for("timing.lap_elapsed", 68.46) == "1:08.5"
    assert display_for("timing.predicted_lap", 58.31) == "0:58.3"
    assert display_for("lap.last_time", 68.4996) == "1:08.500"
    assert display_for("lap.best_time", 59.9996) == "1:00.000"
    assert display_for("timing.delta_best", 0.35) == "+0.3"
    assert display_for("timing.delta_best", -0.42) == "-0.4"

    assert display_for("car.rpm", 4500.0) is None
    assert display_for("lap.event", "{}") is None
    assert display_for("lap.number", 12) is None


def test_display_for_leaves_unencodable_values_to_the_caller():
    # The JSON encoder is the single authority on rejecting non-finite
    # values; the formatter must not turn them into a plausible string.
    assert display_for("timing.lap_elapsed", math.nan) is None
    assert display_for("timing.lap_elapsed", math.inf) is None
    assert display_for("lap.last_time", True) is None


def test_pit_clock_display_switches_resolution_on_authority():
    assert pit_clock_display(95.06, "extrapolating") == "1:35.1"
    assert pit_clock_display(300.0, "degraded") == "5:00.0"
    assert pit_clock_display(92.5004, "authoritative") == "1:32.500"
    assert pit_clock_display(None, "gated") == GATED_DISPLAY
