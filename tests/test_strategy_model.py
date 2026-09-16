"""P7.9: every strategy number against a hand calculation, bounds included.

The scenarios below are small enough to work by hand and the expected values
are written as the arithmetic, not as numbers copied from a run. Each one
exercises a rule the brief names: a counter-reset lap excluded from the burn
and substituted in fuel remaining, in- and out-laps excluded, a lap-time
outlier excluded, the re-base after a refuel with its confidence, the pit
window, the stop plan and its diff against the operator's plan, driver
time, the target lap and the refuel clock.
"""

from __future__ import annotations

import math
import statistics
from datetime import UTC, datetime, timedelta

import pytest

from pit.strategy.model import (
    LapFact,
    PlanFacts,
    RaceInputs,
    StintFact,
    StopFact,
    StrategyPolicy,
    clean_laps,
    evaluate,
    idle_state,
)
from pit.strategy.publisher import OUTPUT_CHANNELS, messages, mqtt_message

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=UTC)
POLICY = StrategyPolicy()


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _lap(
    number: int,
    crossed_s: float,
    fuel: float | None,
    *,
    lap_time: float = 100.0,
    status: str = "clean",
    pit_status: str = "track",
    valid: bool = True,
) -> LapFact:
    return LapFact(
        lap_number=number,
        crossed_at=_at(crossed_s),
        lap_time_s=lap_time,
        valid=valid,
        pit_status=pit_status,
        stint_number=1,
        fuel_used_l=fuel,
        measurement_status=status,
    )


def _plan(**overrides) -> PlanFacts:
    base = dict(
        revision=1,
        race_end_at=None,
        race_end_laps=100,
        end_authority="laps",
        tank_l=60.0,
        usable_fuel_l=55.0,
        refuel_min_s=480,
        service_typical_s=120,
        driver_limits={"max_continuous_min": 120, "max_total_min": 360},
        planned_stops=[
            {"type": "refuel", "at_lap": 40, "driver_in": "Driver B"},
            {"type": "refuel", "at_lap": 80},
        ],
    )
    base.update(overrides)
    return PlanFacts(**base)


def _inputs(now_s: float, plan: PlanFacts | None, laps, stints, stops=()) -> RaceInputs:
    return RaceInputs(
        now=_at(now_s),
        vehicle_id="example-club-racer",
        session_id="s-1",
        session_started=T0,
        plan=plan,
        laps=list(laps),
        stints=list(stints),
        stops=list(stops),
    )


# --- scenario A: a stint from the start, one reset lap, one slow lap -------------

# Ten laps of 100 s. Lap 4's counter reset mid-lap; lap 7 took 130 s (a
# yellow) and is excluded from the burn by the outlier rule but its counter
# delta is still real fuel that left the tank.
A_FUEL = {1: 1.0, 2: 1.1, 3: 0.9, 4: None, 5: 1.0, 6: 1.0, 7: 1.2, 8: 0.8, 9: 1.1, 10: 0.9}
A_LAPS = [
    _lap(
        n,
        100.0 * n,
        A_FUEL[n],
        lap_time=130.0 if n == 7 else 100.0,
        status="counter_reset" if n == 4 else "clean",
    )
    for n in range(1, 11)
]
A_STINT = StintFact(1, "Driver A", T0, None, 40, 58.0, 50.0)
A_BURNS = [1.0, 1.0, 0.8, 1.1, 0.9]  # laps 5, 6, 8, 9, 10
A_MEAN = sum(A_BURNS) / 5
A_SD = statistics.stdev(A_BURNS)
A_HI = A_MEAN + 2 * A_SD
A_LO = A_MEAN - 2 * A_SD


def test_burn_is_a_rolling_mean_over_clean_laps_only():
    state = evaluate(_inputs(1050, _plan(), A_LAPS, [A_STINT]), POLICY, "lap")
    assert state.burn_laps == 5
    assert state.burn_l_per_lap == pytest.approx(A_MEAN, abs=1e-4)
    assert state.burn_sd == pytest.approx(A_SD, abs=1e-4)
    assert state.lap_time_ref_s == 100.0
    kept = [lap.lap_number for lap in clean_laps(A_LAPS, [], POLICY)]
    assert kept == [1, 2, 3, 5, 6, 8, 9, 10], "the reset lap and the slow lap are out"


def test_fuel_remaining_subtracts_measured_laps_and_substitutes_the_reset_lap():
    state = evaluate(_inputs(1050, _plan(), A_LAPS, [A_STINT]), POLICY, "lap")
    assert state.rebase_confidence == "session_start"
    assert state.rebase_level_l == 58.0 and state.rebase_at == T0
    measured = sum(fuel for fuel in A_FUEL.values() if fuel is not None)  # 9.0, lap 7 included
    remaining = 58.0 - measured - A_MEAN  # the reset lap at the rolling mean
    assert state.fuel_remaining_l == pytest.approx(remaining, abs=0.01)
    spread = 0.5 + A_SD  # session-start reading plus one substituted lap
    assert state.fuel_remaining_lo_l == pytest.approx(remaining - spread, abs=0.01)
    assert state.fuel_remaining_hi_l == pytest.approx(remaining + spread, abs=0.01)


def test_laps_and_time_to_dry_carry_bounds_from_burn_and_fuel():
    state = evaluate(_inputs(1050, _plan(), A_LAPS, [A_STINT]), POLICY, "lap")
    remaining = 58.0 - 9.0 - A_MEAN
    spread = 0.5 + A_SD
    usable_lo = remaining - spread - 5.0  # 5 L of the 60 L tank is unusable
    usable_hi = remaining + spread - 5.0
    assert state.laps_to_dry_lo == pytest.approx(usable_lo / A_HI, abs=0.01)
    assert state.laps_to_dry_hi == pytest.approx(usable_hi / A_LO, abs=0.01)
    assert state.time_to_dry_s_lo == pytest.approx(usable_lo / A_HI * 100.0, abs=1.0)
    assert state.laps_to_dry_lo < state.laps_to_dry_hi


def test_pit_window_from_the_race_end_and_the_planned_stop_count():
    state = evaluate(_inputs(1050, _plan(), A_LAPS, [A_STINT]), POLICY, "lap")
    laps_lo = state.laps_to_dry_lo
    full_range = 55.0 / A_HI
    assert state.laps_remaining == 90
    assert state.stops_needed == math.ceil((90 - laps_lo) / full_range) == 2
    assert state.window_close_lap == 10 + math.floor(laps_lo)
    assert state.window_open_lap == max(10, math.ceil(100 - 2 * full_range))
    assert not [f for f in state.findings if f.monitor == "strategy.pit_window"]


def test_stop_plan_is_recomputed_from_the_burn_and_diffed_against_the_operator():
    state = evaluate(_inputs(1050, _plan(), A_LAPS, [A_STINT]), POLICY, "lap")
    first_stop = 10 + math.floor(state.laps_to_dry_lo)
    second_stop = first_stop + math.floor(55.0 / A_HI)
    assert [stop["lap"] for stop in state.stop_plan] == [first_stop, second_stop]
    assert [stop["type"] for stop in state.stop_plan] == ["refuel", "refuel"]
    assert state.stop_plan[0]["driver_in"] == "Driver B"
    assert state.stop_plan[0]["planned_lap"] == 40
    assert state.stop_plan[0]["delta_laps"] == first_stop - 40
    assert state.stop_plan[1]["delta_laps"] == second_stop - 80
    assert state.plan_drift["planned"] == 2 and state.plan_drift["computed"] == 2
    assert state.plan_drift["max_abs_delta"] == max(abs(first_stop - 40), abs(second_stop - 80))
    assert state.plan_drift["diverged"] is True
    drift = [f for f in state.findings if f.monitor == "strategy.plan_drift"]
    assert len(drift) == 1 and drift[0].severity == "warning"


def test_the_stop_plan_changes_when_the_burn_changes():
    thirsty = [
        _lap(
            n,
            100.0 * n,
            (A_FUEL[n] or 0) * 1.5 if A_FUEL[n] else None,
            status=lap.measurement_status,
        )
        for n, lap in zip(range(1, 11), A_LAPS, strict=True)
    ]
    lean = evaluate(_inputs(1050, _plan(), A_LAPS, [A_STINT]), POLICY, "lap")
    rich = evaluate(_inputs(1050, _plan(), thirsty, [A_STINT]), POLICY, "lap")
    assert rich.burn_l_per_lap > lean.burn_l_per_lap
    assert rich.stop_plan[0]["lap"] < lean.stop_plan[0]["lap"]
    assert len(rich.stop_plan) >= len(lean.stop_plan)


def test_a_lap_planned_stop_gives_no_target_lap_time():
    state = evaluate(_inputs(1050, _plan(), A_LAPS, [A_STINT]), POLICY, "lap")
    assert state.target_lap_s is None


# --- scenario B: a refuel stop, the re-base, a short fill, in- and out-laps --------

B_PLAN = _plan(
    race_end_at=_at(21600),
    race_end_laps=None,
    end_authority="time",
    driver_limits={"max_continuous_min": 120},
    planned_stops=[{"type": "refuel", "at_ms": round(_at(10800).timestamp() * 1000)}],
)
B_STOP = StopFact(entry_at=_at(1950), exit_at=_at(2450), is_open=False, stop_type="refuel")
B_STINTS = [
    StintFact(1, "Driver A", T0, _at(2000), 100, 58.0, 40.0),
    StintFact(2, "Driver B", _at(2000), None, 30, 52.0, 50.0),
]
B_LAPS = (
    [_lap(n, 100.0 * n, 1.0) for n in range(1, 20)]
    + [_lap(20, 2000, 0.5, pit_status="pit")]  # the in-lap, crossed in the lane
    + [_lap(21, 2500, None, lap_time=500.0, status="counter_reset")]  # the out-lap
    + [_lap(22, 2600, 1.0), _lap(23, 2700, 1.0)]
)


def test_in_and_out_laps_are_excluded_from_the_burn():
    kept = [lap.lap_number for lap in clean_laps(B_LAPS, [B_STOP], POLICY)]
    assert 20 not in kept and 21 not in kept
    assert kept[-2:] == [22, 23]


def test_the_refuel_rebase_comes_from_the_stint_that_began_in_the_stop():
    state = evaluate(_inputs(2750, B_PLAN, B_LAPS, B_STINTS, [B_STOP]), POLICY, "lap")
    assert state.rebase_confidence == "key_on"
    assert state.rebase_level_l == 52.0 and state.rebase_at == _at(2000)
    assert state.fuel_added_l == pytest.approx(52.0 - 40.0)
    # The out-lap's counter reset is substituted for the 50 s driven after
    # the stop exit, half a 100 s racing lap at the 1.0 L/lap burn.
    assert state.fuel_remaining_l == pytest.approx(52.0 - 0.5 - 1.0 - 1.0)
    assert state.fuel_remaining_lo_l == pytest.approx(49.5 - (0.3 + 0.25))
    assert state.burn_l_per_lap == 1.0 and state.burn_sd == 0.0


def test_a_short_fill_is_the_warning_that_is_easiest_to_forget():
    state = evaluate(_inputs(2750, B_PLAN, B_LAPS, B_STINTS, [B_STOP]), POLICY, "lap")
    short = [f for f in state.findings if f.monitor == "strategy.short_fill"]
    assert len(short) == 1
    assert short[0].summary["expected_l"] == 60.0
    assert short[0].summary["observed_l"] == 52.0
    assert short[0].summary["fuel_added_l"] == pytest.approx(12.0)

    full = [StintFact(1, "Driver A", T0, _at(2000), 100, 58.0, 40.0)] + [
        StintFact(2, "Driver B", _at(2000), None, 30, 59.5, 50.0)
    ]
    clean = evaluate(_inputs(2750, B_PLAN, B_LAPS, full, [B_STOP]), POLICY, "lap")
    assert not [f for f in clean.findings if f.monitor == "strategy.short_fill"]


def test_time_authority_end_gives_laps_remaining_and_a_target_lap_time():
    state = evaluate(_inputs(2750, B_PLAN, B_LAPS, B_STINTS, [B_STOP]), POLICY, "lap")
    assert state.laps_remaining == math.ceil((21600 - 2750) / 100)
    usable_lo = 49.5 - 0.55 - 5.0
    assert state.laps_to_dry_lo == pytest.approx(usable_lo, abs=0.01)
    # The stop is planned at a wall-clock time: the lap time that reaches it
    # exactly dry is the time until it divided by the laps the fuel lasts.
    assert state.target_lap_s == pytest.approx((10800 - 2750) / usable_lo, abs=0.05)


def test_time_authority_stop_plan_charges_stop_time_and_folds_refuels_into_driver_changes():
    state = evaluate(_inputs(2750, B_PLAN, B_LAPS, B_STINTS, [B_STOP]), POLICY, "lap")
    laps_lo = math.floor(state.laps_to_dry_lo)  # 43
    first = 23 + laps_lo
    # Driver B has 7200 - 750 s of continuous time; the first stop (fuel) does
    # not change driver, so the second is driver-limited 21 laps later and,
    # with the fuel unable to reach the end anyway, refuels at the same time.
    second = first + math.floor((7200 - 750 - laps_lo * 100) / 100)
    assert [stop["lap"] for stop in state.stop_plan][:2] == [first, second]
    assert state.stop_plan[0]["reason"] == "fuel"
    assert state.stop_plan[1]["reason"] == "driver" and state.stop_plan[1]["type"] == "refuel"
    assert state.stop_plan[0]["planned_lap"] == 23 + math.floor((10800 - 2750) / 100)
    assert state.plan_drift["planned"] == 1 and state.plan_drift["computed"] == len(state.stop_plan)
    assert state.plan_drift["diverged"] is True


# --- scenario C: driver time, a closing window, an open refuel stop ----------------

C_PLAN = _plan(
    race_end_laps=100,
    driver_limits={"max_continuous_min": 30, "max_total_min": 60},
    planned_stops=[],
)
C_LAPS = [_lap(n, 100.0 * n, 1.0) for n in range(1, 6)]
C_STINT = StintFact(1, "Driver A", T0, None, 40, 12.0, 8.0)
C_OPEN_STOP = StopFact(entry_at=_at(1400), exit_at=None, is_open=True, stop_type="refuel")


def test_driver_within_the_margin_of_a_limit_is_a_warning_finding():
    state = evaluate(_inputs(1500, C_PLAN, C_LAPS, [C_STINT], [C_OPEN_STOP]), POLICY, "pit")
    assert state.driver == "Driver A"
    assert state.driver_time_remaining_s == pytest.approx(1800 - 1500)
    assert state.driver_total_remaining_s == pytest.approx(3600 - 1500)
    finding = next(f for f in state.findings if f.monitor == "strategy.driver_time")
    assert finding.severity == "warning"
    assert finding.summary["limit"] == "continuous"
    assert finding.summary["remaining_s"] == pytest.approx(300.0)
    assert "Driver A" in finding.summary["message"]


def test_driver_over_the_limit_is_critical_and_a_fresh_driver_is_nothing():
    over = evaluate(_inputs(1900, C_PLAN, C_LAPS, [C_STINT], [C_OPEN_STOP]), POLICY, "pit")
    finding = next(f for f in over.findings if f.monitor == "strategy.driver_time")
    assert finding.severity == "critical" and finding.summary["remaining_s"] < 0

    fresh = evaluate(_inputs(600, C_PLAN, C_LAPS, [C_STINT]), POLICY, "lap")
    assert not [f for f in fresh.findings if f.monitor == "strategy.driver_time"]


def test_a_closing_window_is_a_finding_and_the_refuel_clock_runs_from_the_entry():
    state = evaluate(_inputs(1500, C_PLAN, C_LAPS, [C_STINT], [C_OPEN_STOP]), POLICY, "pit")
    assert state.fuel_remaining_l == pytest.approx(12.0 - 5.0)
    assert state.laps_to_dry_lo == pytest.approx((7.0 - 0.5 - 5.0) / 1.0)
    assert state.window_close_lap == 5 + 1
    window = next(f for f in state.findings if f.monitor == "strategy.pit_window")
    assert window.severity == "warning"
    assert window.summary["window_close_lap"] == 6
    assert state.refuel_elapsed_s == pytest.approx(100.0)
    assert state.refuel_remaining_s == pytest.approx(380.0)
    assert state.refuel_release_at == _at(1400 + 480)


def test_no_stop_needed_means_no_window_and_no_stops():
    plan = _plan(race_end_laps=20, planned_stops=[])
    stint = StintFact(1, "Driver A", T0, None, 40, 58.0, 50.0)
    state = evaluate(_inputs(550, plan, C_LAPS, [stint]), POLICY, "lap")
    assert state.laps_remaining == 15
    assert state.stops_needed == 0
    assert state.window_open_lap is None and state.window_close_lap is None
    assert state.stop_plan == [] and state.plan_drift == {}
    assert state.findings == ()


# --- degraded inputs ---------------------------------------------------------------


def test_without_a_plan_the_burn_is_still_computed_and_nothing_fires():
    state = evaluate(_inputs(1050, None, A_LAPS, [A_STINT]), POLICY, "lap")
    assert state.burn_l_per_lap == pytest.approx(A_MEAN, abs=1e-4)
    assert state.fuel_remaining_l is not None
    assert state.laps_to_dry_lo is None and state.window_open_lap is None
    assert state.stop_plan == [] and state.findings == ()


def test_without_any_level_reading_the_plan_tank_is_assumed_and_said_so():
    stint = StintFact(1, "Driver A", T0, None, 0, None, None)
    state = evaluate(_inputs(1050, _plan(), A_LAPS, [stint]), POLICY, "lap")
    assert state.rebase_confidence == "plan"
    assert state.rebase_level_l == 60.0
    assert state.fuel_remaining_hi_l - state.fuel_remaining_lo_l > 6.0


def test_a_stint_spanning_the_refuel_tracks_the_moving_level_reading():
    stints = [StintFact(1, "Driver A", T0, None, 100, 58.0, 47.0)]
    state = evaluate(_inputs(2750, B_PLAN, B_LAPS, stints, [B_STOP]), POLICY, "lap")
    assert state.rebase_confidence == "moving"
    assert state.fuel_remaining_l == 47.0
    assert state.fuel_remaining_lo_l == pytest.approx(45.0)


def test_the_idle_row_carries_no_session_and_no_numbers():
    state = idle_state(T0, "example-club-racer")
    assert state.session_id is None and state.trigger == "idle"
    assert state.laps_to_dry_lo is None and state.findings == ()


# --- the live numbers ------------------------------------------------------------


def test_every_output_channel_is_published_with_a_display_and_nulls_survive():
    state = evaluate(_inputs(1050, _plan(), A_LAPS, [A_STINT]), POLICY, "lap")
    published = dict(messages(state))
    assert set(published) == set(OUTPUT_CHANNELS)
    assert published["strategy.laps_to_dry"]["value"] == state.laps_to_dry_lo
    assert published["strategy.target_lap_s"] == {
        "time": round(_at(1050).timestamp() * 1000),
        "value": None,
        "display": "--:--.-",
    }
    assert published["strategy.time_to_dry_s"]["display"].count(":") >= 1

    idle = dict(messages(idle_state(T0, "example-club-racer")))
    assert all(document["value"] is None for document in idle.values())


def test_mqtt_messages_are_pit_owned_and_compact():
    topic, payload = mqtt_message(
        "example-club-racer", "strategy.laps_to_dry", {"time": 1, "value": 2.5, "display": "2.5"}
    )
    assert topic == "openlaps/example-club-racer/strategy.laps_to_dry"
    assert payload == b'{"time":1,"value":2.5,"display":"2.5"}'
    with pytest.raises(ValueError, match="non-strategy"):
        mqtt_message("example-club-racer", "car.fuel_level", {})
