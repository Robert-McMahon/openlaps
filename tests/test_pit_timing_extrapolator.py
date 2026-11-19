"""P6.6 pit-side lap/sector clocks: authoritative resets and visible degradation."""

from __future__ import annotations

import json

import pytest

from pit.timing_extrapolator.engine import ClockPolicy, TimingExtrapolator


def event(kind: str, at: float, **values: object) -> str:
    payload = {
        "type": kind,
        "time": at,
        "line": "StartFinish" if kind == "lap_completed" else "Sector1",
        "lap_number": 7,
        "sector": 0,
        "split_time": 0.0,
        "lap_time": 0.0,
    }
    payload.update(values)
    return json.dumps(payload)


def healthy(engine: TimingExtrapolator) -> None:
    engine.observe("sys.host.clock_offset_s", 0.001)
    engine.observe("sys.host.clock_stratum", 1)
    engine.observe("sys.host.clock_source", "GPS")


def test_lap_clock_keeps_counting_without_new_vehicle_samples():
    engine = TimingExtrapolator(ClockPolicy(fallback_max_lap_s=300.0))
    healthy(engine)
    engine.observe("lap.event", event("lap_completed", 1_000.0, lap_time=92.5))

    first = engine.tick(1_010.0)
    later = engine.tick(1_025.0)

    assert first["timing.lap_elapsed_pit"].value == pytest.approx(10.0)
    assert later["timing.lap_elapsed_pit"].value == pytest.approx(25.0)
    assert later["timing.lap_elapsed_pit"].status == "extrapolating"


def test_missing_clock_health_gates_extrapolation_visibly():
    engine = TimingExtrapolator(ClockPolicy())
    engine.observe("lap.event", event("lap_completed", 1_000.0, lap_time=92.5))

    output = engine.tick(1_010.0)["timing.lap_elapsed_pit"]

    assert output.value is None
    assert output.status == "gated"
    assert output.reason == "clock_health_missing"


@pytest.mark.parametrize(
    ("channel", "value", "reason"),
    [
        ("sys.host.clock_offset_s", 0.2, "clock_offset"),
        ("sys.host.clock_stratum", 5, "clock_stratum"),
        ("sys.host.clock_source", "LOCAL", "clock_source"),
    ],
)
def test_unhealthy_clock_signal_gates_extrapolation(channel: str, value: object, reason: str):
    engine = TimingExtrapolator(ClockPolicy())
    healthy(engine)
    engine.observe(channel, value)
    engine.observe("lap.event", event("lap_completed", 1_000.0, lap_time=92.5))

    output = engine.tick(1_010.0)["timing.lap_elapsed_pit"]

    assert output == output.__class__(None, "gated", reason)


def test_sector_completion_resets_sector_clock_at_authoritative_crossing_time():
    engine = TimingExtrapolator(ClockPolicy())
    healthy(engine)

    completed = engine.observe(
        "lap.event", event("sector_completed", 1_030.0, sector=1, split_time=30.0)
    )
    running = engine.tick(1_042.5)

    assert completed["timing.sector_elapsed_pit"].value == pytest.approx(30.0)
    assert completed["timing.sector_elapsed_pit"].status == "authoritative"
    assert running["timing.sector_elapsed_pit"].value == pytest.approx(12.5)


def test_lost_completion_degrades_at_best_lap_multiple_instead_of_running_away():
    engine = TimingExtrapolator(ClockPolicy(best_lap_multiple=2.0, fallback_max_lap_s=300.0))
    healthy(engine)
    engine.observe("lap.best_time", 90.0)
    engine.observe("lap.event", event("lap_completed", 1_000.0, lap_time=92.5))

    output = engine.tick(1_200.0)["timing.lap_elapsed_pit"]

    assert output.value == pytest.approx(180.0)
    assert output.status == "degraded"
    assert output.reason == "runaway"


def test_lap_completion_publishes_vehicle_time_then_resets_both_running_clocks():
    engine = TimingExtrapolator(ClockPolicy())
    healthy(engine)
    engine.observe("lap.event", event("sector_completed", 1_070.0, sector=2, split_time=35.0))

    completed = engine.observe("lap.event", event("lap_completed", 1_100.0, lap_time=100.0))
    running = engine.tick(1_103.0)

    assert completed["timing.lap_elapsed_pit"].value == pytest.approx(100.0)
    assert completed["timing.lap_elapsed_pit"].status == "authoritative"
    assert running["timing.lap_elapsed_pit"].value == pytest.approx(3.0)
    assert running["timing.sector_elapsed_pit"].value == pytest.approx(3.0)


def test_a_lap_with_no_known_time_restarts_the_clocks_without_publishing_one():
    engine = TimingExtrapolator(ClockPolicy())
    healthy(engine)

    completed = engine.observe("lap.event", event("lap_completed", 1_100.0, lap_time=None))
    running = engine.tick(1_103.0)

    assert "timing.lap_elapsed_pit" not in completed
    assert running["timing.lap_elapsed_pit"].value == pytest.approx(3.0)


def test_each_sector_holds_its_time_until_the_car_is_in_it_again():
    engine = TimingExtrapolator(ClockPolicy())
    healthy(engine)
    engine.observe("lap.event", event("lap_completed", 1_000.0, lap_time=100.0))

    # Sector 1 runs; the others have not been timed yet and are not listed.
    first = engine.tick(1_010.0)
    assert first["timing.sector_1_pit"].value == pytest.approx(10.0)
    assert "timing.sector_2_pit" not in first

    done = engine.observe(
        "lap.event", event("sector_completed", 1_035.0, sector=1, split_time=35.0)
    )
    assert done["timing.sector_1_pit"] == done["timing.sector_elapsed_pit"]
    second = engine.tick(1_040.0)
    assert second["timing.sector_1_pit"].value == pytest.approx(35.0)
    assert second["timing.sector_1_pit"].status == "authoritative"
    assert second["timing.sector_2_pit"].value == pytest.approx(5.0)
    assert second["timing.sector_2_pit"].status == "extrapolating"

    engine.observe("lap.event", event("sector_completed", 1_075.0, sector=2, split_time=40.0))
    third = engine.tick(1_080.0)
    assert third["timing.sector_2_pit"].value == pytest.approx(40.0)
    assert third["timing.sector_3_pit"].value == pytest.approx(5.0)

    # The lap ends: sector 1 runs again, sectors 2 and 3 keep their times.
    engine.observe("lap.event", event("sector_completed", 1_108.0, sector=3, split_time=33.0))
    engine.observe("lap.event", event("lap_completed", 1_108.0, lap_time=108.0))
    new_lap = engine.tick(1_110.0)
    assert new_lap["timing.sector_1_pit"].value == pytest.approx(2.0)
    assert new_lap["timing.sector_2_pit"].value == pytest.approx(40.0)
    assert new_lap["timing.sector_3_pit"].value == pytest.approx(33.0)
    assert "timing.sector_4_pit" not in new_lap


def test_a_gated_clock_blanks_the_running_sector_but_keeps_completed_times():
    engine = TimingExtrapolator(ClockPolicy())
    healthy(engine)
    engine.observe("lap.event", event("lap_completed", 1_000.0, lap_time=100.0))
    engine.observe("lap.event", event("sector_completed", 1_035.0, sector=1, split_time=35.0))
    engine.observe("sys.host.clock_stratum", 9)

    output = engine.tick(1_040.0)

    assert output["timing.sector_1_pit"].value == pytest.approx(35.0)
    assert output["timing.sector_2_pit"].status == "gated"


def test_a_sector_joined_mid_lap_is_marked_not_timed():
    engine = TimingExtrapolator(ClockPolicy())
    healthy(engine)
    engine.observe("lap.event", event("sector_completed", 1_075.0, sector=2, split_time=40.0))

    output = engine.tick(1_080.0)

    assert output["timing.sector_1_pit"].status == "pending"
    assert output["timing.sector_1_pit"].value is None
    assert output["timing.sector_2_pit"].value == pytest.approx(40.0)
    assert output["timing.sector_3_pit"].value == pytest.approx(5.0)
