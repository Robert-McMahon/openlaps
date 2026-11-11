"""The strategy arithmetic, with no I/O: the state of the race for this car.

Everything here is a function of what the stable views say (``RaceInputs``)
and a handful of tunables (``StrategyPolicy``), so a test can hand it a
known fuel profile and a race plan and check every number against a hand
calculation, bounds included.

The fuel model is P6.3's, in code rather than in a panel:

    fuel_remaining = level_at_last_rebase - fuel burned since that re-base

The re-base is a level reading from ``v_stint_fuel_level`` -- the first
accepted reading of the stint that began during a refuelling stop, which is
the key-on window with the car stationary -- and the burn since is the sum
of clean per-lap counter deltas from ``v_lap_fuel``. A lap the view could
not measure (a counter reset, no samples) is *substituted* with the rolling
mean for the fraction of that lap after the re-base, never averaged into
the burn itself, and the substitution widens the bounds.

That is ``fuel_model = "level"``, and it needs a level sender that reads
litres. ``fuel_model = "counter"`` is for a car whose sender does not: it
anchors the tank on the moments its contents are actually known -- full at
the end of a refuelling stop, whatever the crew marked, nearly empty when
the surge-tank switch closes -- and runs the ECU's fuel-used counter
between them (``fuel_anchors`` and ``_counter_fuel`` below).

Every projection carries a lower and an upper bound. The lower bound is the
one that gets radioed.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

# --- inputs -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanFacts:
    """The latest ``v_race_plan`` revision for the session."""

    revision: int
    race_end_at: datetime | None
    race_end_laps: int | None
    end_authority: str
    tank_l: float
    usable_fuel_l: float
    refuel_min_s: int
    service_typical_s: int
    driver_limits: dict[str, int] = field(default_factory=dict)
    planned_stops: list[dict[str, Any]] = field(default_factory=list)
    # Litres actually burned per litre the fuel-used counter reports.
    fuel_used_scale: float = 1.0


@dataclass(frozen=True, slots=True)
class LapFact:
    """One ``v_lap_fuel`` row, in crossing order."""

    lap_number: int
    crossed_at: datetime
    lap_time_s: float | None
    valid: bool
    pit_status: str | None
    stint_number: int | None
    fuel_used_l: float | None
    measurement_status: str


@dataclass(frozen=True, slots=True)
class StintFact:
    """One ``v_stint_fuel_level`` row."""

    stint_number: int
    driver: str
    started: datetime
    ended: datetime | None
    sample_count: int
    level_start_l: float | None
    level_end_l: float | None


@dataclass(frozen=True, slots=True)
class StopFact:
    """One ``v_pit_stops`` row for this vehicle since the session started."""

    entry_at: datetime
    exit_at: datetime | None
    is_open: bool
    stop_type: str
    # Whether the level sender read full before the car left the stop; None
    # when that was not looked for (no threshold configured, stop still open).
    full_confirmed: bool | None = None


@dataclass(frozen=True, slots=True)
class FlagFact:
    """One ``v_session_flags`` row: a red-flag period, open while ``ended`` is None."""

    started: datetime
    ended: datetime | None


@dataclass(frozen=True, slots=True)
class FuelMarkFact:
    """One ``v_fuel_marks`` row: what the crew says was in the tank."""

    at: datetime
    level_l: float


@dataclass(frozen=True, slots=True)
class CounterSpan:
    """Fuel-used counter consumption between two instants, unscaled.

    ``end`` None means "to now". ``used_l`` None means the counter had no
    samples in the span, which is not the same as having used nothing.
    """

    start: datetime
    end: datetime | None
    used_l: float | None


@dataclass(frozen=True, slots=True)
class RaceInputs:
    """Everything one evaluation reads, as of ``now``."""

    now: datetime
    vehicle_id: str
    session_id: str
    session_started: datetime
    plan: PlanFacts | None
    laps: list[LapFact]
    stints: list[StintFact]
    stops: list[StopFact]
    reds: list[FlagFact] = field(default_factory=list)
    fuel_marks: list[FuelMarkFact] = field(default_factory=list)
    # Periods the surge-tank switch read "low", as (first, last) instants.
    surge_episodes: list[tuple[datetime, datetime]] = field(default_factory=list)
    counter_spans: list[CounterSpan] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class StrategyPolicy:
    """Tunables; every one is documented in ``example.env``."""

    burn_window_laps: int = 5
    burn_sigma: float = 2.0
    outlier_fraction: float = 0.2
    driver_margin_s: float = 600.0
    plan_drift_laps: int = 3
    short_fill_fraction: float = 0.10
    window_warn_laps: int = 2
    max_simulated_stops: int = 50
    # "level": re-base on the level sender's litres (the original model).
    # "counter": anchor on full / fuel mark / surge light and run the
    # fuel-used counter between them.
    fuel_model: str = "level"
    # The reading that means "the sender shows a full tank". Unset, a stop
    # is never confirmed full and the tank is assumed full after a refuel.
    full_channel: str = "car.fuel_level"
    full_threshold: float | None = None
    full_when: str = "above"  # above | below: which side of the threshold is full
    full_min_samples: int = 3
    # The reading that means "the surge tank's float switch has closed".
    surge_channel: str = "car.surge_tank_switch"
    surge_threshold: float | None = None
    surge_when: str = "above"
    surge_hold_s: float = 5.0  # shorter than this is slosh, not an empty tank
    surge_reserve_l: float = 1.8  # usable litres left when it closes
    counter_channel: str = "car.fuel_total_used"
    counter_error_fraction: float = 0.03
    calibration_warn_fraction: float = 0.05


# The uncertainty a re-base carries, in litres, by where the reading came
# from. A stationary key-on reading is the sensor's 0.1 L quantum plus
# dither; a reading taken on track is dominated by slosh.
_REBASE_UNCERTAINTY_L = {
    "key_on": 0.3,
    "session_start": 0.5,
    "moving": 2.0,
    "plan": 3.0,
    "none": 0.0,
    # The counter model's anchors. "full" is a brimmed tank the sender
    # agreed with; the sender steps in 5-10 L blocks, so its agreement is
    # worth a few litres, not tenths. "assumed_full" is a refuel stop nobody
    # and nothing confirmed. "surge" is a float switch at a known volume.
    "full": 3.0,
    "assumed_full": 5.0,
    "operator": 3.0,
    "surge": 0.5,
}

MONITORS = (
    "strategy.driver_time",
    "strategy.fuel_calibration",
    "strategy.pit_window",
    "strategy.plan_drift",
    "strategy.short_fill",
    "strategy.surge",
)


# --- outputs ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    """A strategy warning, written as a ``watch_findings`` row."""

    monitor: str
    severity: str  # warning | critical
    score: float
    summary: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StrategyState:
    """One ``strategy_state`` row plus the findings that hold at this instant."""

    time: datetime
    vehicle_id: str
    session_id: str | None
    trigger: str
    lap_number: int | None
    plan_revision: int | None
    fuel_remaining_l: float | None
    fuel_remaining_lo_l: float | None
    fuel_remaining_hi_l: float | None
    rebase_confidence: str
    rebase_level_l: float | None
    rebase_at: datetime | None
    fuel_added_l: float | None
    burn_l_per_lap: float | None
    burn_sd: float | None
    burn_laps: int
    lap_time_ref_s: float | None
    laps_to_dry_lo: float | None
    laps_to_dry_hi: float | None
    time_to_dry_s_lo: float | None
    time_to_dry_s_hi: float | None
    laps_remaining: int | None
    stops_needed: int | None
    window_open_lap: int | None
    window_close_lap: int | None
    target_lap_s: float | None
    driver: str | None
    driver_time_remaining_s: float | None
    driver_total_remaining_s: float | None
    refuel_elapsed_s: float | None
    refuel_remaining_s: float | None
    refuel_release_at: datetime | None
    stop_plan: list[dict[str, Any]]
    plan_drift: dict[str, Any]
    findings: tuple[Finding, ...] = ()


def idle_state(now: datetime, vehicle_id: str) -> StrategyState:
    """The row written once when a session ends: no session, no numbers."""
    return StrategyState(
        time=now,
        vehicle_id=vehicle_id,
        session_id=None,
        trigger="idle",
        lap_number=None,
        plan_revision=None,
        fuel_remaining_l=None,
        fuel_remaining_lo_l=None,
        fuel_remaining_hi_l=None,
        rebase_confidence="none",
        rebase_level_l=None,
        rebase_at=None,
        fuel_added_l=None,
        burn_l_per_lap=None,
        burn_sd=None,
        burn_laps=0,
        lap_time_ref_s=None,
        laps_to_dry_lo=None,
        laps_to_dry_hi=None,
        time_to_dry_s_lo=None,
        time_to_dry_s_hi=None,
        laps_remaining=None,
        stops_needed=None,
        window_open_lap=None,
        window_close_lap=None,
        target_lap_s=None,
        driver=None,
        driver_time_remaining_s=None,
        driver_total_remaining_s=None,
        refuel_elapsed_s=None,
        refuel_remaining_s=None,
        refuel_release_at=None,
        stop_plan=[],
        plan_drift={},
    )


# --- the evaluation -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Burn:
    mean: float | None
    sd: float | None
    laps: int
    lap_time_s: float | None
    lo: float | None
    hi: float | None


@dataclass(frozen=True, slots=True)
class _Rebase:
    level_l: float | None
    at: datetime | None
    confidence: str
    fuel_added_l: float | None


def evaluate(inputs: RaceInputs, policy: StrategyPolicy, trigger: str) -> StrategyState:
    """Compute the state of the race for this car as of ``inputs.now``."""
    now = inputs.now
    plan = inputs.plan
    laps = sorted(inputs.laps, key=lambda lap: lap.crossed_at)
    lap_count = len(laps)
    stints = sorted(inputs.stints, key=lambda stint: stint.stint_number)
    stops = sorted(inputs.stops, key=lambda stop: stop.entry_at)
    findings: list[Finding] = []

    reds = sorted(inputs.reds, key=lambda red: red.started)
    scale = plan.fuel_used_scale if plan is not None else 1.0
    if scale != 1.0:
        laps = [
            replace(lap, fuel_used_l=lap.fuel_used_l * scale)
            if lap.fuel_used_l is not None
            else lap
            for lap in laps
        ]

    clean = clean_laps(laps, stops, policy, reds)
    burn = _burn(clean, policy)
    if policy.fuel_model == "counter":
        rebase, (fuel, fuel_lo, fuel_hi) = _counter_fuel(
            inputs, laps, stops, burn, scale, policy, findings
        )
    else:
        rebase = _rebase(stints, stops, plan, inputs.session_started, now)
        fuel, fuel_lo, fuel_hi = _fuel_remaining(rebase, laps, stops, burn)

    usable = usable_lo = usable_hi = None
    if plan is not None and fuel is not None:
        unusable = plan.tank_l - plan.usable_fuel_l
        usable = max(fuel - unusable, 0.0)
        usable_lo = max((fuel_lo or fuel) - unusable, 0.0)
        usable_hi = max((fuel_hi or fuel) - unusable, 0.0)

    laps_lo = laps_hi = time_lo = time_hi = None
    if usable is not None and burn.mean:
        laps_lo = usable_lo / burn.hi if burn.hi else None
        laps_hi = usable_hi / burn.lo if burn.lo else None
        if burn.lap_time_s and laps_lo is not None and laps_hi is not None:
            time_lo = laps_lo * burn.lap_time_s
            time_hi = laps_hi * burn.lap_time_s

    laps_remaining = _laps_remaining(plan, lap_count, burn.lap_time_s, now)

    stops_needed = window_open = window_close = None
    full_range_lo = None
    if plan is not None and burn.hi and laps_lo is not None and laps_remaining is not None:
        full_range_lo = plan.usable_fuel_l / burn.hi
        planned_refuels = sum(
            1 for stop in _remaining_planned(plan, lap_count, now) if stop.get("type") == "refuel"
        )
        if laps_remaining <= laps_lo:
            stops_needed = 0
        elif full_range_lo >= 1.0:
            stops_needed = math.ceil((laps_remaining - laps_lo) / full_range_lo)
            stops_total = max(stops_needed, planned_refuels)
            end_lap = lap_count + laps_remaining
            window_close = lap_count + math.floor(laps_lo)
            window_open = max(lap_count, math.ceil(end_lap - stops_total * full_range_lo))
            _window_findings(findings, lap_count, window_open, window_close, laps_lo, policy)

    stint = stints[-1] if stints else None
    driver_remaining, total_remaining = _driver_time(
        stint, stints, plan, now, findings, policy, reds
    )

    stop_plan: list[dict[str, Any]] = []
    plan_drift: dict[str, Any] = {}
    if plan is not None and laps_lo is not None and burn.lap_time_s and full_range_lo:
        stop_plan = _stop_plan(
            plan,
            lap_count,
            laps_remaining,
            laps_lo,
            full_range_lo,
            burn.lap_time_s,
            driver_remaining,
            now,
            policy,
        )
        plan_drift = _plan_drift(plan, stop_plan, lap_count, burn.lap_time_s, now, policy)
        if plan_drift.get("diverged"):
            findings.append(
                Finding(
                    "strategy.plan_drift",
                    "warning",
                    min(1.0, plan_drift["max_abs_delta"] / max(policy.plan_drift_laps, 1)),
                    {
                        "message": (
                            f"the numbers now say {plan_drift['computed']} stop(s); the plan "
                            f"says {plan_drift['planned']}, first stop off by "
                            f"{plan_drift['first_delta']:+d} lap(s)"
                        ),
                        **plan_drift,
                    },
                )
            )

    target = _target_lap(plan, laps_lo, lap_count, now)
    refuel_elapsed, refuel_remaining, release_at = _refuel_clock(stops, plan, now)

    if rebase.confidence == "key_on" and plan is not None and rebase.level_l is not None:
        expected = plan.tank_l
        floor_l = expected * (1.0 - policy.short_fill_fraction)
        if rebase.level_l < floor_l:
            findings.append(
                Finding(
                    "strategy.short_fill",
                    "warning",
                    min(1.0, (expected - rebase.level_l) / expected),
                    {
                        "message": (
                            f"level after refuel {rebase.level_l:.1f} L against a "
                            f"{expected:.1f} L tank"
                        ),
                        "expected_l": expected,
                        "observed_l": rebase.level_l,
                        "fuel_added_l": rebase.fuel_added_l,
                    },
                )
            )

    return StrategyState(
        time=now,
        vehicle_id=inputs.vehicle_id,
        session_id=inputs.session_id,
        trigger=trigger,
        lap_number=lap_count or None,
        plan_revision=plan.revision if plan else None,
        fuel_remaining_l=_round(fuel),
        fuel_remaining_lo_l=_round(fuel_lo),
        fuel_remaining_hi_l=_round(fuel_hi),
        rebase_confidence=rebase.confidence,
        rebase_level_l=_round(rebase.level_l),
        rebase_at=rebase.at,
        fuel_added_l=_round(rebase.fuel_added_l),
        burn_l_per_lap=_round(burn.mean, 4),
        burn_sd=_round(burn.sd, 4),
        burn_laps=burn.laps,
        lap_time_ref_s=_round(burn.lap_time_s),
        laps_to_dry_lo=_round(laps_lo),
        laps_to_dry_hi=_round(laps_hi),
        time_to_dry_s_lo=_round(time_lo, 1),
        time_to_dry_s_hi=_round(time_hi, 1),
        laps_remaining=laps_remaining,
        stops_needed=stops_needed,
        window_open_lap=window_open,
        window_close_lap=window_close,
        target_lap_s=_round(target),
        driver=stint.driver if stint else None,
        driver_time_remaining_s=_round(driver_remaining, 1),
        driver_total_remaining_s=_round(total_remaining, 1),
        refuel_elapsed_s=_round(refuel_elapsed, 1),
        refuel_remaining_s=_round(refuel_remaining, 1),
        refuel_release_at=release_at,
        stop_plan=stop_plan,
        plan_drift=plan_drift,
        findings=tuple(findings),
    )


# --- laps and burn ----------------------------------------------------------------


def _lap_window(lap: LapFact) -> tuple[datetime, datetime] | None:
    if lap.lap_time_s is None or lap.lap_time_s <= 0:
        return None
    return lap.crossed_at - timedelta(seconds=lap.lap_time_s), lap.crossed_at


def _touches_pits(lap: LapFact, stops: list[StopFact]) -> bool:
    """An in-lap or an out-lap: a pit crossing fell inside the lap's window."""
    window = _lap_window(lap)
    if window is None:
        return True
    start, end = window
    for stop in stops:
        if start < stop.entry_at <= end:
            return True
        if stop.exit_at is not None and start < stop.exit_at <= end:
            return True
        if stop.entry_at <= start and (stop.exit_at is None or stop.exit_at >= end):
            return True
    return False


def _under_red(lap: LapFact, reds: list[FlagFact]) -> bool:
    """Any part of the lap was driven while the race was stopped."""
    window = _lap_window(lap)
    start, end = window if window is not None else (lap.crossed_at, lap.crossed_at)
    return any(red.started <= end and (red.ended is None or red.ended >= start) for red in reds)


def red_seconds(start: datetime, end: datetime, reds: list[FlagFact]) -> float:
    """Seconds between ``start`` and ``end`` that fell under a red flag."""
    total = 0.0
    for red in reds:
        overlap = (min(end, red.ended or end) - max(start, red.started)).total_seconds()
        if overlap > 0:
            total += overlap
    return total


def clean_laps(
    laps: list[LapFact],
    stops: list[StopFact],
    policy: StrategyPolicy,
    reds: list[FlagFact] | None = None,
) -> list[LapFact]:
    """Laps the burn may be computed from, in crossing order.

    Kept: valid, timed, a clean counter measurement, positive burn, not
    crossed in the pit lane, not touching a pit stop and not driven under a
    recorded red flag. Then the lap-time outlier rule stands in for the flag
    states the pit does not have: a lap slower than the recent median by
    more than ``outlier_fraction`` is a yellow, traffic or a spin, and burns
    nothing like a racing lap.
    """
    reds = reds or []
    candidates = [
        lap
        for lap in laps
        if lap.valid
        and lap.lap_time_s is not None
        and lap.lap_time_s > 0
        and lap.measurement_status == "clean"
        and lap.fuel_used_l is not None
        and lap.fuel_used_l > 0
        and lap.pit_status != "pit"
        and not _touches_pits(lap, stops)
        and not _under_red(lap, reds)
    ]
    if not candidates:
        return []
    recent = candidates[-max(policy.burn_window_laps * 2, 1) :]
    reference = statistics.median(lap.lap_time_s for lap in recent if lap.lap_time_s)
    ceiling = reference * (1.0 + policy.outlier_fraction)
    return [lap for lap in candidates if lap.lap_time_s is not None and lap.lap_time_s <= ceiling]


def _burn(clean: list[LapFact], policy: StrategyPolicy) -> _Burn:
    window = clean[-max(policy.burn_window_laps, 1) :]
    if not window:
        return _Burn(None, None, 0, None, None, None)
    burns = [lap.fuel_used_l for lap in window if lap.fuel_used_l is not None]
    mean = statistics.fmean(burns)
    sd = statistics.stdev(burns) if len(burns) >= 2 else None
    spread = policy.burn_sigma * sd if sd is not None else 0.0
    lap_time = statistics.fmean(lap.lap_time_s for lap in window if lap.lap_time_s)
    lo = max(mean - spread, mean * 0.25)
    hi = mean + spread
    return _Burn(mean, sd, len(window), lap_time, lo, hi)


# --- the re-base and fuel remaining ----------------------------------------------


def _stint_containing(stints: list[StintFact], at: datetime, now: datetime) -> StintFact | None:
    for stint in stints:
        end = stint.ended or now
        if stint.started <= at <= end:
            return stint
    return None


def _rebase(
    stints: list[StintFact],
    stops: list[StopFact],
    plan: PlanFacts | None,
    session_started: datetime,
    now: datetime,
) -> _Rebase:
    """Where the fuel model last had a trustworthy level reading."""
    current = stints[-1] if stints else None
    refuels = [
        stop
        for stop in stops
        if stop.stop_type == "refuel" and not stop.is_open and stop.exit_at is not None
    ]
    last = refuels[-1] if refuels else None

    if last is not None and last.exit_at is not None:
        stint = _stint_containing(stints, last.exit_at, now)
        if stint is not None and stint.started >= last.entry_at and stint.level_start_l is not None:
            # The stint began during the stop: its first accepted reading is
            # the key-on window, car stationary, engine not yet turning.
            before = next(
                (
                    candidate.level_end_l
                    for candidate in reversed(stints)
                    if candidate.stint_number < stint.stint_number
                    and candidate.level_end_l is not None
                ),
                None,
            )
            added = stint.level_start_l - before if before is not None else None
            return _Rebase(stint.level_start_l, stint.started, "key_on", added)
        if current is not None and current.level_end_l is not None:
            # The stint spans the stop, so its first reading predates the
            # fill. The only post-fill reading the view offers is the latest
            # one, taken on track: track the sensor and say so.
            return _Rebase(current.level_end_l, now, "moving", None)
        if plan is not None:
            return _Rebase(plan.tank_l, last.exit_at, "plan", None)
        return _Rebase(None, None, "none", None)

    first = stints[0] if stints else None
    if first is not None and first.level_start_l is not None:
        return _Rebase(first.level_start_l, first.started, "session_start", None)
    if current is not None and current.level_end_l is not None:
        return _Rebase(current.level_end_l, now, "moving", None)
    if plan is not None:
        return _Rebase(plan.tank_l, session_started, "plan", None)
    return _Rebase(None, None, "none", None)


def _fuel_remaining(
    rebase: _Rebase, laps: list[LapFact], stops: list[StopFact], burn: _Burn
) -> tuple[float | None, float | None, float | None]:
    """The re-base less what the counter says was burned since it."""
    if rebase.level_l is None or rebase.at is None:
        return None, None, None
    uncertainty = _REBASE_UNCERTAINTY_L[rebase.confidence]
    if rebase.confidence == "moving":
        # The reading *is* the current level; nothing to subtract.
        return rebase.level_l, rebase.level_l - uncertainty, rebase.level_l + uncertainty

    used = 0.0
    substituted = 0
    for lap in laps:
        if lap.crossed_at <= rebase.at:
            continue
        if lap.measurement_status == "clean" and lap.fuel_used_l is not None:
            # A clean counter delta, including the out-lap's: after a stop the
            # counter restarts from zero, so the delta covers exactly the
            # fraction of the lap that was driven.
            used += lap.fuel_used_l
            continue
        # Unmeasured (a counter reset mid-lap, no samples): substitute the
        # rolling mean for the fraction of a racing lap actually driven after
        # the re-base -- time inside a pit stop burns nothing -- and widen
        # the bounds for it. Never averaged into the burn itself.
        if burn.mean is None:
            continue
        fraction = 1.0
        window = _lap_window(lap)
        if window is not None:
            start, end = window
            driven = _driven_seconds(max(start, rebase.at), end, stops)
            reference = burn.lap_time_s or (end - start).total_seconds()
            fraction = min(max(driven / reference, 0.0), 1.0) if reference > 0 else 1.0
        used += burn.mean * fraction
        substituted += 1
    remaining = rebase.level_l - used
    spread = uncertainty + substituted * (burn.sd or (burn.mean or 0.0) * 0.25)
    return remaining, remaining - spread, remaining + spread


# --- the counter model: anchors and the fuel-used counter between them -----------


@dataclass(frozen=True, slots=True)
class FuelAnchor:
    """A moment the tank's contents are known, and how."""

    at: datetime
    level_l: float
    confidence: str  # plan | assumed_full | full | operator | surge


# Where two anchors share an instant, the better-informed one wins.
_ANCHOR_RANK = {"plan": 0, "assumed_full": 1, "full": 2, "surge": 3, "operator": 4}


def fuel_anchors(inputs: RaceInputs, policy: StrategyPolicy) -> list[FuelAnchor]:
    """Every moment this session at which the tank's contents are known.

    In time order; the last one is what the fuel model stands on.

    * The session start, assumed a full tank (``plan``).
    * The end of each closed refuelling stop: ``full`` when the level sender
      read full before the car left, ``assumed_full`` when it did not or was
      not asked. A fuel mark made during the stop replaces the stop's own
      anchor -- the crew's word on a short fill beats an assumption.
    * Each fuel mark (``operator``).
    * Each time the surge-tank switch stayed closed for ``surge_hold_s``
      (``surge``): the main tank is empty and ``surge_reserve_l`` usable
      litres remain. Shorter closures are slosh.
    """
    plan = inputs.plan
    anchors: list[FuelAnchor] = []
    marks = sorted(inputs.fuel_marks, key=lambda mark: mark.at)
    if plan is not None:
        anchors.append(FuelAnchor(inputs.session_started, plan.tank_l, "plan"))
        for stop in inputs.stops:
            if stop.stop_type != "refuel" or stop.is_open or stop.exit_at is None:
                continue
            if any(stop.entry_at <= mark.at <= stop.exit_at for mark in marks):
                continue
            confidence = "full" if stop.full_confirmed else "assumed_full"
            anchors.append(FuelAnchor(stop.exit_at, plan.tank_l, confidence))
    anchors.extend(FuelAnchor(mark.at, mark.level_l, "operator") for mark in marks)
    unusable = plan.tank_l - plan.usable_fuel_l if plan is not None else 0.0
    for first, last in inputs.surge_episodes:
        if (last - first).total_seconds() >= policy.surge_hold_s:
            anchors.append(FuelAnchor(first, unusable + policy.surge_reserve_l, "surge"))
    return sorted(anchors, key=lambda anchor: (anchor.at, _ANCHOR_RANK[anchor.confidence]))


def calibration_interval(anchors: list[FuelAnchor]) -> tuple[FuelAnchor, FuelAnchor] | None:
    """The latest known-full to surge-light run: the counter's one honest test.

    A tank that was known full (or marked by the crew) and then run until
    the surge switch closed burned a known volume; what the counter reported
    over the same interval is how far it can be trusted.
    """
    for index in range(len(anchors) - 1, 0, -1):
        surge, before = anchors[index], anchors[index - 1]
        if surge.confidence == "surge" and before.confidence in ("full", "operator"):
            return before, surge
    return None


def counter_requests(
    inputs: RaceInputs, policy: StrategyPolicy
) -> list[tuple[datetime, datetime | None]]:
    """The counter spans an evaluation of ``inputs`` will ask for.

    The database layer reads the anchors' ingredients first, calls this, and
    reads exactly these spans: the anchor is not known until then.
    """
    anchors = fuel_anchors(inputs, policy)
    if not anchors:
        return []
    requests: list[tuple[datetime, datetime | None]] = [(anchors[-1].at, None)]
    interval = calibration_interval(anchors)
    if interval is not None:
        requests.append((interval[0].at, interval[1].at))
    return requests


def _span(spans: list[CounterSpan], start: datetime, end: datetime | None) -> CounterSpan | None:
    return next((span for span in spans if span.start == start and span.end == end), None)


def _counter_fuel(
    inputs: RaceInputs,
    laps: list[LapFact],
    stops: list[StopFact],
    burn: _Burn,
    scale: float,
    policy: StrategyPolicy,
    findings: list[Finding],
) -> tuple[_Rebase, tuple[float | None, float | None, float | None]]:
    """Fuel remaining as the last anchor less the counter since it."""
    anchors = fuel_anchors(inputs, policy)
    if not anchors:
        return _Rebase(None, None, "none", None), (None, None, None)
    anchor = anchors[-1]
    rebase = _Rebase(anchor.level_l, anchor.at, anchor.confidence, None)
    uncertainty = _REBASE_UNCERTAINTY_L[anchor.confidence]

    span = _span(inputs.counter_spans, anchor.at, None)
    if span is not None and span.used_l is not None:
        used = span.used_l * scale
        remaining = anchor.level_l - used
        spread = uncertainty + used * policy.counter_error_fraction
        fuel = (remaining, remaining - spread, remaining + spread)
    else:
        # The counter has said nothing since the anchor (the car is off, or
        # the channel is missing): fall back to the per-lap deltas, which
        # are already scaled.
        fuel = _fuel_remaining(rebase, laps, stops, burn)

    if anchor.confidence == "surge":
        findings.append(
            Finding(
                "strategy.surge",
                "critical",
                1.0,
                {
                    "message": (
                        f"surge tank light on since {anchor.at:%H:%M:%S}Z: the main tank is "
                        f"empty, about {policy.surge_reserve_l:.1f} L usable was left then"
                    ),
                    "since": anchor.at.isoformat(),
                    "reserve_l": policy.surge_reserve_l,
                },
            )
        )
    if anchor.confidence == "assumed_full" and policy.full_threshold is not None:
        findings.append(
            Finding(
                "strategy.short_fill",
                "warning",
                0.5,
                {
                    "message": (
                        "the level sender did not read full at the end of the refuel stop; "
                        "a full tank is assumed - set a fuel mark if it was a short fill"
                    ),
                    "expected_l": anchor.level_l,
                },
            )
        )

    interval = calibration_interval(anchors)
    if interval is not None:
        before, surge = interval
        measured = _span(inputs.counter_spans, before.at, surge.at)
        expected = before.level_l - surge.level_l
        if measured is not None and measured.used_l and expected > 0:
            observed_scale = expected / measured.used_l
            if abs(observed_scale / scale - 1.0) > policy.calibration_warn_fraction:
                findings.append(
                    Finding(
                        "strategy.fuel_calibration",
                        "warning",
                        min(1.0, abs(observed_scale / scale - 1.0)),
                        {
                            "message": (
                                f"the tank gave {expected:.1f} L from full to the surge light "
                                f"and the counter reported {measured.used_l:.1f} L: a counter "
                                f"scale of {observed_scale:.3f} fits, the plan has {scale:.3f}"
                            ),
                            "tank_l": round(expected, 2),
                            "counter_l": round(measured.used_l, 2),
                            "observed_scale": round(observed_scale, 4),
                            "plan_scale": scale,
                        },
                    )
                )
    return rebase, fuel


def _driven_seconds(start: datetime, end: datetime, stops: list[StopFact]) -> float:
    """Seconds between ``start`` and ``end`` not spent inside a pit stop."""
    if end <= start:
        return 0.0
    driven = (end - start).total_seconds()
    for stop in stops:
        stop_end = stop.exit_at or end
        overlap = (min(end, stop_end) - max(start, stop.entry_at)).total_seconds()
        if overlap > 0:
            driven -= overlap
    return max(driven, 0.0)


# --- the race end, the window and the stop plan ------------------------------------


def _laps_remaining(
    plan: PlanFacts | None, lap_count: int, lap_time_s: float | None, now: datetime
) -> int | None:
    if plan is None:
        return None
    if plan.end_authority == "laps" and plan.race_end_laps is not None:
        return max(plan.race_end_laps - lap_count, 0)
    if plan.race_end_at is not None and lap_time_s:
        seconds = (plan.race_end_at - now).total_seconds()
        return max(math.ceil(seconds / lap_time_s), 0)
    return None


def _planned_lap(
    stop: dict[str, Any], lap_count: int, lap_time_s: float | None, now: datetime
) -> int | None:
    at_lap = stop.get("at_lap")
    if isinstance(at_lap, int | float) and not isinstance(at_lap, bool):
        return int(at_lap)
    at_ms = stop.get("at_ms")
    if isinstance(at_ms, int | float) and not isinstance(at_ms, bool) and lap_time_s:
        seconds = at_ms / 1000.0 - now.timestamp()
        return lap_count + max(math.floor(seconds / lap_time_s), 0)
    return None


def _remaining_planned(plan: PlanFacts, lap_count: int, now: datetime) -> list[dict[str, Any]]:
    remaining: list[dict[str, Any]] = []
    for stop in plan.planned_stops:
        at_lap = stop.get("at_lap")
        at_ms = stop.get("at_ms")
        if isinstance(at_lap, int | float) and not isinstance(at_lap, bool):
            if at_lap > lap_count:
                remaining.append(stop)
        elif isinstance(at_ms, int | float) and not isinstance(at_ms, bool):
            if at_ms / 1000.0 > now.timestamp():
                remaining.append(stop)
    return remaining


def _window_findings(
    findings: list[Finding],
    lap_count: int,
    window_open: int,
    window_close: int,
    laps_lo: float,
    policy: StrategyPolicy,
) -> None:
    laps_left = window_close - lap_count
    if window_open > window_close:
        findings.append(
            Finding(
                "strategy.pit_window",
                "critical",
                1.0,
                {
                    "message": (
                        f"the window has closed: stopping by lap {window_close} cannot reach "
                        f"the end with the planned stops (earliest lap {window_open})"
                    ),
                    "window_open_lap": window_open,
                    "window_close_lap": window_close,
                    "laps_to_dry_lo": round(laps_lo, 2),
                },
            )
        )
    elif laps_left <= policy.window_warn_laps:
        findings.append(
            Finding(
                "strategy.pit_window",
                "critical" if laps_lo < 1.0 else "warning",
                1.0 if laps_lo < 1.0 else 1.0 - laps_left / max(policy.window_warn_laps + 1, 1),
                {
                    "message": f"pit window closes at lap {window_close}, {laps_left} lap(s) away",
                    "window_open_lap": window_open,
                    "window_close_lap": window_close,
                    "laps_to_dry_lo": round(laps_lo, 2),
                },
            )
        )


def _stop_plan(
    plan: PlanFacts,
    lap_count: int,
    laps_remaining: int | None,
    laps_lo: float,
    full_range_lo: float,
    lap_time_s: float,
    driver_remaining_s: float | None,
    now: datetime,
    policy: StrategyPolicy,
) -> list[dict[str, Any]]:
    """The remaining stops the numbers now say, as a list.

    A walk from now to the race end: stop when the fuel or the driver's
    continuous limit runs out, whichever comes first, refuel when the fuel
    would not otherwise make the end, and take the driver coming in from the
    operator's plan when it names one. Under a time-authority end, each stop
    costs its regulated or typical duration and shortens the race in laps.
    """
    if laps_remaining is None or full_range_lo < 1.0:
        return []
    planned = _remaining_planned(plan, lap_count, now)
    max_continuous = plan.driver_limits.get("max_continuous_min")
    limit_s = max_continuous * 60.0 if max_continuous else None

    fuel_laps = laps_lo
    drive_left = driver_remaining_s if limit_s is not None else None
    lap = lap_count
    clock = now
    remaining = laps_remaining
    stops: list[dict[str, Any]] = []
    while remaining > 0 and len(stops) < policy.max_simulated_stops:
        laps_fuel = max(math.floor(fuel_laps), 0)
        laps_driver = (
            max(math.floor(drive_left / lap_time_s), 0) if drive_left is not None else None
        )
        can = laps_fuel if laps_driver is None else min(laps_fuel, laps_driver)
        if can >= remaining:
            break
        reason = "fuel" if laps_driver is None or laps_fuel <= laps_driver else "driver"
        # A driver-limited stop becomes a refuel when the fuel left after it
        # would not reach the end anyway: fold the fill into the change.
        refuel = reason == "fuel" or (fuel_laps - can) < (remaining - can)
        planned_stop = planned[len(stops)] if len(stops) < len(planned) else None
        driver_in = planned_stop.get("driver_in") if planned_stop else None
        stop_lap = lap + can
        duration = plan.refuel_min_s if refuel else plan.service_typical_s
        clock = clock + timedelta(seconds=can * lap_time_s + duration)
        stops.append(
            {
                "lap": stop_lap,
                "type": "refuel" if refuel else "service",
                "driver_in": driver_in,
                "reason": reason,
                "at": clock.isoformat(),
                "planned_lap": (
                    _planned_lap(planned_stop, lap_count, lap_time_s, now) if planned_stop else None
                ),
            }
        )
        lap = stop_lap
        remaining -= can
        fuel_laps = full_range_lo if refuel else fuel_laps - can
        if drive_left is not None and limit_s is not None:
            drive_left = (
                limit_s if (driver_in or reason == "driver") else drive_left - can * lap_time_s
            )
        if plan.end_authority == "time" and plan.race_end_at is not None:
            remaining = max(math.ceil((plan.race_end_at - clock).total_seconds() / lap_time_s), 0)
    for stop in stops:
        planned_lap = stop["planned_lap"]
        stop["delta_laps"] = stop["lap"] - planned_lap if planned_lap is not None else None
    return stops


def _plan_drift(
    plan: PlanFacts,
    stop_plan: list[dict[str, Any]],
    lap_count: int,
    lap_time_s: float,
    now: datetime,
    policy: StrategyPolicy,
) -> dict[str, Any]:
    planned = _remaining_planned(plan, lap_count, now)
    if not plan.planned_stops:
        return {}
    deltas = [stop["delta_laps"] for stop in stop_plan if stop.get("delta_laps") is not None]
    max_abs = max((abs(delta) for delta in deltas), default=0)
    diverged = len(planned) != len(stop_plan) or max_abs > policy.plan_drift_laps
    return {
        "planned": len(planned),
        "computed": len(stop_plan),
        "max_abs_delta": max_abs,
        "first_delta": deltas[0] if deltas else 0,
        "diverged": diverged,
    }


# --- driver time, the target lap, the refuel clock ----------------------------------


def _driver_time(
    stint: StintFact | None,
    stints: list[StintFact],
    plan: PlanFacts | None,
    now: datetime,
    findings: list[Finding],
    policy: StrategyPolicy,
    reds: list[FlagFact] | None = None,
) -> tuple[float | None, float | None]:
    """Drive time left on each limit; a red flag stops the driver's clock.

    A stint runs from the pit-exit line to the pit-entry line, so the stop
    before it is already outside it. The race clock runs through a red flag
    but the car is parked, so those seconds are not drive time either.
    """
    if stint is None or plan is None:
        return None, None
    reds = reds or []
    continuous_s = (now - stint.started).total_seconds() - red_seconds(stint.started, now, reds)
    total_s = sum(
        ((candidate.ended or now) - candidate.started).total_seconds()
        - red_seconds(candidate.started, candidate.ended or now, reds)
        for candidate in stints
        if candidate.driver == stint.driver
    )
    limits = plan.driver_limits
    remaining = total_remaining = None
    if limits.get("max_continuous_min"):
        remaining = limits["max_continuous_min"] * 60.0 - continuous_s
    if limits.get("max_total_min"):
        total_remaining = limits["max_total_min"] * 60.0 - total_s

    worst: tuple[str, float, float] | None = None
    for name, value, limit_min in (
        ("continuous", remaining, limits.get("max_continuous_min")),
        ("total", total_remaining, limits.get("max_total_min")),
    ):
        if value is None or limit_min is None:
            continue
        if value <= policy.driver_margin_s and (worst is None or value < worst[1]):
            worst = (name, value, limit_min * 60.0)
    if worst is not None:
        name, value, limit_s = worst
        over = value <= 0
        findings.append(
            Finding(
                "strategy.driver_time",
                "critical" if over else "warning",
                1.0 if over else 1.0 - value / max(policy.driver_margin_s, 1.0),
                {
                    "message": (
                        f"{stint.driver}: {name} drive time "
                        + (
                            f"exceeded by {-value / 60:.0f} min"
                            if over
                            else f"{value / 60:.0f} min left"
                        )
                        + f" of {limit_s / 60:.0f}"
                    ),
                    "driver": stint.driver,
                    "limit": name,
                    "remaining_s": round(value, 1),
                    "limit_s": limit_s,
                },
            )
        )
    return remaining, total_remaining


def _target_lap(
    plan: PlanFacts | None, laps_lo: float | None, lap_count: int, now: datetime
) -> float | None:
    """The lap time that reaches the next planned moment exactly dry.

    Only a stop planned at a wall-clock time (or, with none planned, a
    time-authority race end) gives pace a say in whether the fuel lasts:
    against a stop planned at a lap, burn per lap decides and pace does not.
    """
    if plan is None or not laps_lo or laps_lo <= 0:
        return None
    moment: datetime | None = None
    for stop in _remaining_planned(plan, lap_count, now):
        at_ms = stop.get("at_ms")
        if isinstance(at_ms, int | float) and not isinstance(at_ms, bool):
            moment = datetime.fromtimestamp(at_ms / 1000.0, tz=now.tzinfo)
        break
    if moment is None and plan.end_authority == "time" and plan.race_end_at is not None:
        if not _remaining_planned(plan, lap_count, now):
            moment = plan.race_end_at
    if moment is None:
        return None
    seconds = (moment - now).total_seconds()
    if seconds <= 0:
        return None
    return seconds / laps_lo


def _refuel_clock(
    stops: list[StopFact], plan: PlanFacts | None, now: datetime
) -> tuple[float | None, float | None, datetime | None]:
    open_stops = [stop for stop in stops if stop.is_open and stop.stop_type == "refuel"]
    if not open_stops or plan is None:
        return None, None, None
    stop = open_stops[-1]
    elapsed = (now - stop.entry_at).total_seconds()
    remaining = max(plan.refuel_min_s - elapsed, 0.0)
    return elapsed, remaining, stop.entry_at + timedelta(seconds=plan.refuel_min_s)


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)
