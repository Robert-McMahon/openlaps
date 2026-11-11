"""What the strategy service reads and writes, on one connection.

Reads are from the stable views only (``docs/PIT_SCHEMA.md``): the service
inherits every counter-reset, missing-sample and low-voltage rule from
``v_lap_fuel`` and ``v_stint_fuel_level`` and reimplements none of them.
Writes go to the two tables it owns rows in -- ``strategy_state`` and its
``strategy.*`` findings in ``watch_findings`` -- in one transaction per
evaluation, in the pit-monitor pattern (ADR 0011 decision 2).

The connection is dropped on any error and redialled on the next use, so a
database that went away is a counted error on ``/health`` and never a
service that keeps evaluating against a dead session.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from pit.strategy.model import (
    MONITORS,
    CounterSpan,
    Finding,
    FlagFact,
    FuelMarkFact,
    LapFact,
    PlanFacts,
    RaceInputs,
    StintFact,
    StopFact,
    StrategyPolicy,
    StrategyState,
    counter_requests,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Probe:
    """The cheap per-poll read that decides whether to evaluate."""

    session_id: str | None
    session_started: datetime | None
    last_crossed_at: datetime | None
    plan_revision: int | None
    in_pits: bool


_PROBE_SESSION = """
SELECT session_id, started FROM v_session_active
WHERE vehicle_id = %s ORDER BY started DESC LIMIT 1
"""
_PROBE_LAP = "SELECT max(crossed_at) FROM v_laps WHERE vehicle_id = %s AND session_id = %s"
_PROBE_PLAN = "SELECT revision FROM v_race_plan WHERE session_id = %s"
_PROBE_PITS = """
SELECT bool_or(is_open) FROM v_pit_stops WHERE vehicle_id = %s AND entry_at >= %s
"""

_PLAN = """
SELECT revision, race_end_at, race_end_laps, end_authority, tank_l, usable_fuel_l,
       refuel_min_s, service_typical_s, driver_limits, planned_stops, fuel_used_scale
FROM v_race_plan WHERE session_id = %s
"""
_FLAGS = """
SELECT started, ended FROM v_session_flags
WHERE session_id = %s AND kind = 'red' ORDER BY started
"""
_MARKS = "SELECT at, level_l FROM v_fuel_marks WHERE session_id = %s ORDER BY at"
# How many readings in a window sat on the "full" side of the threshold.
# `{op}` is one of two literals chosen in code, never caller text.
_FULL_READINGS = """
SELECT count(*) FROM v_samples_named
WHERE vehicle_id = %s AND channel = %s AND value IS NOT NULL
  AND time >= %s AND time <= %s AND value {op} %s
"""
# The surge switch's state changes since the session started, from the
# one-second trace: a row per second is small enough to window over a whole
# race, where the raw channel is not. One row per change of state.
_SURGE_CHANGES = """
SELECT time, is_on FROM (
    SELECT time, avg {op} %s AS is_on, lag(avg {op} %s) OVER (ORDER BY time) AS was_on
    FROM v_samples_1s_named
    WHERE vehicle_id = %s AND channel = %s AND time >= %s AND time <= %s
) changes
WHERE was_on IS DISTINCT FROM is_on
ORDER BY time
"""
_SURGE_LAST = """
SELECT max(time) FROM v_samples_1s_named
WHERE vehicle_id = %s AND channel = %s AND time >= %s AND time <= %s
"""
# The fuel-used counter is cumulative and restarts from zero when the ECU
# is power-cycled (the kill switch is off for every refuel). Sum the rises;
# where it fell, the engine has burned the new value since the restart.
_COUNTER_USED = """
SELECT count(*),
       sum(CASE WHEN previous IS NULL THEN 0
                WHEN value >= previous THEN value - previous
                ELSE value END)
FROM (
    SELECT value, lag(value) OVER (ORDER BY time) AS previous
    FROM v_samples_named
    WHERE vehicle_id = %s AND channel = %s AND value IS NOT NULL
      AND time >= %s AND time <= %s
) counter
"""
_LAPS = """
SELECT lap_number, crossed_at, lap_time_s, valid, pit_status, stint_number,
       fuel_used_l, measurement_status
FROM v_lap_fuel WHERE vehicle_id = %s AND session_id = %s ORDER BY crossed_at
"""
_STINTS = """
SELECT stint_number, driver, started, ended, sample_count, level_start_l, level_end_l
FROM v_stint_fuel_level WHERE session_id = %s ORDER BY stint_number
"""
_STOPS = """
SELECT entry_at, exit_at, is_open, stop_type
FROM v_pit_stops WHERE vehicle_id = %s AND entry_at >= %s ORDER BY entry_at
"""
_OPEN_FINDINGS = """
SELECT finding_id, monitor FROM watch_findings
WHERE vehicle_id = %s AND closed_at IS NULL AND monitor = ANY(%s)
"""

_INSERT_STATE = """
INSERT INTO strategy_state (
    time, vehicle_id, session_id, trigger, lap_number, plan_revision,
    fuel_remaining_l, fuel_remaining_lo_l, fuel_remaining_hi_l,
    rebase_confidence, rebase_level_l, rebase_at, fuel_added_l,
    burn_l_per_lap, burn_sd, burn_laps, lap_time_ref_s,
    laps_to_dry_lo, laps_to_dry_hi, time_to_dry_s_lo, time_to_dry_s_hi,
    laps_remaining, stops_needed, window_open_lap, window_close_lap, target_lap_s,
    driver, driver_time_remaining_s, driver_total_remaining_s,
    refuel_elapsed_s, refuel_remaining_s, refuel_release_at, stop_plan, plan_drift
) VALUES (
    %s, %s, %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s, %s, %s
)
"""
_OPEN_FINDING = """
INSERT INTO watch_findings
    (finding_id, vehicle_id, monitor, opened_at, severity, peak_score, summary)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""
_UPDATE_FINDING = """
UPDATE watch_findings SET severity = %s, peak_score = greatest(peak_score, %s), summary = %s
WHERE finding_id = %s
"""
_CLOSE_FINDING = (
    "UPDATE watch_findings SET closed_at = %s WHERE finding_id = %s AND closed_at IS NULL"
)


class StrategyDatabase:
    """Owns one connection to the pit database and every statement on it."""

    def __init__(self, dsn: str, vehicle_id: str, policy: StrategyPolicy | None = None) -> None:
        self._dsn = dsn
        self._vehicle = vehicle_id
        # The policy names the channels and thresholds the counter fuel model
        # reads; the level model needs none of them.
        self._policy = policy or StrategyPolicy()
        self._conn: psycopg.Connection | None = None
        # monitor -> the open finding's id, so a warning that persists across
        # evaluations is one row that closes once, not a row per lap.
        self.open_findings: dict[str, uuid.UUID] = {}

    # -- reads ------------------------------------------------------------------

    def probe(self) -> Probe:
        """The handful of values whose change means "evaluate again"."""
        conn = self._connection()
        try:
            session = conn.execute(_PROBE_SESSION, (self._vehicle,)).fetchone()
            if session is None:
                return Probe(None, None, None, None, False)
            session_id, started = session
            (last_lap,) = conn.execute(_PROBE_LAP, (self._vehicle, session_id)).fetchone()
            plan = conn.execute(_PROBE_PLAN, (session_id,)).fetchone()
            (in_pits,) = conn.execute(_PROBE_PITS, (self._vehicle, started)).fetchone()
            return Probe(session_id, started, last_lap, plan[0] if plan else None, bool(in_pits))
        except psycopg.Error:
            self.close()
            raise

    def read_inputs(self, session_id: str, session_started: datetime, now: datetime) -> RaceInputs:
        """Everything one evaluation needs, from the views."""
        conn = self._connection()
        try:
            plan_row = conn.execute(_PLAN, (session_id,)).fetchone()
            laps = conn.execute(_LAPS, (self._vehicle, session_id)).fetchall()
            stints = conn.execute(_STINTS, (session_id,)).fetchall()
            stops = conn.execute(_STOPS, (self._vehicle, session_started)).fetchall()
            flags = conn.execute(_FLAGS, (session_id,)).fetchall()
            marks = conn.execute(_MARKS, (session_id,)).fetchall()
        except psycopg.Error:
            self.close()
            raise
        plan = None
        if plan_row is not None:
            plan = PlanFacts(
                revision=int(plan_row[0]),
                race_end_at=plan_row[1],
                race_end_laps=plan_row[2],
                end_authority=str(plan_row[3]),
                tank_l=float(plan_row[4]),
                usable_fuel_l=float(plan_row[5]),
                refuel_min_s=int(plan_row[6]),
                service_typical_s=int(plan_row[7]),
                driver_limits={
                    key: int(value)
                    for key, value in (plan_row[8] or {}).items()
                    if isinstance(value, int | float) and not isinstance(value, bool)
                },
                planned_stops=[stop for stop in (plan_row[9] or []) if isinstance(stop, dict)],
                fuel_used_scale=float(plan_row[10] or 1.0),
            )
        inputs = RaceInputs(
            now=now,
            vehicle_id=self._vehicle,
            session_id=session_id,
            session_started=session_started,
            plan=plan,
            laps=[
                LapFact(
                    lap_number=int(row[0]),
                    crossed_at=row[1],
                    lap_time_s=_float(row[2]),
                    valid=bool(row[3]),
                    pit_status=row[4],
                    stint_number=row[5],
                    fuel_used_l=_float(row[6]),
                    measurement_status=str(row[7]),
                )
                for row in laps
            ],
            stints=[
                StintFact(
                    stint_number=int(row[0]),
                    driver=str(row[1]),
                    started=row[2],
                    ended=row[3],
                    sample_count=int(row[4] or 0),
                    level_start_l=_float(row[5]),
                    level_end_l=_float(row[6]),
                )
                for row in stints
            ],
            stops=[
                StopFact(
                    entry_at=row[0], exit_at=row[1], is_open=bool(row[2]), stop_type=str(row[3])
                )
                for row in stops
            ],
            reds=[FlagFact(started=row[0], ended=row[1]) for row in flags],
            fuel_marks=[FuelMarkFact(at=row[0], level_l=float(row[1])) for row in marks],
        )
        if self._policy.fuel_model != "counter":
            return inputs
        try:
            return self._read_counter_inputs(conn, inputs)
        except psycopg.Error:
            self.close()
            raise

    def _read_counter_inputs(self, conn: psycopg.Connection, inputs: RaceInputs) -> RaceInputs:
        """What the counter fuel model needs beyond the views' rows.

        In order, because each step decides what the next reads: whether the
        sender confirmed each refuel, when the surge switch closed, and only
        then -- the anchor now being known -- the counter since it.
        """
        policy = self._policy
        now = inputs.now
        stops = inputs.stops
        if policy.full_threshold is not None:
            query = _FULL_READINGS.format(op=_operator(policy.full_when))
            checked = []
            for stop in stops:
                if stop.stop_type == "refuel" and not stop.is_open and stop.exit_at is not None:
                    (count,) = conn.execute(
                        query,
                        (
                            self._vehicle,
                            policy.full_channel,
                            stop.entry_at,
                            stop.exit_at,
                            policy.full_threshold,
                        ),
                    ).fetchone()
                    stop = replace(stop, full_confirmed=int(count) >= policy.full_min_samples)
                checked.append(stop)
            stops = checked

        episodes: list[tuple[datetime, datetime]] = []
        if policy.surge_threshold is not None:
            op = _operator(policy.surge_when)
            window = (self._vehicle, policy.surge_channel, inputs.session_started, now)
            changes = conn.execute(
                _SURGE_CHANGES.format(op=op),
                (policy.surge_threshold, policy.surge_threshold, *window),
            ).fetchall()
            (last_sample,) = conn.execute(_SURGE_LAST, window).fetchone()
            episodes = surge_episodes(changes, last_sample)

        inputs = replace(inputs, stops=stops, surge_episodes=episodes)
        spans = []
        for start, end in counter_requests(inputs, policy):
            count, used_cc = conn.execute(
                _COUNTER_USED, (self._vehicle, policy.counter_channel, start, end or now)
            ).fetchone()
            used_l = float(used_cc) / 1000.0 if count and used_cc is not None else None
            spans.append(CounterSpan(start=start, end=end, used_l=used_l))
        return replace(inputs, counter_spans=spans)

    def adopt_open_findings(self) -> int:
        """On startup, take over strategy findings a previous run left open."""
        conn = self._connection()
        try:
            rows = conn.execute(_OPEN_FINDINGS, (self._vehicle, list(MONITORS))).fetchall()
        except psycopg.Error:
            self.close()
            raise
        self.open_findings = {str(monitor): finding_id for finding_id, monitor in rows}
        return len(self.open_findings)

    # -- writes -----------------------------------------------------------------

    def write(self, state: StrategyState) -> None:
        """Commit one evaluation: the state row and its findings, together."""
        conn = self._connection()
        try:
            with conn.transaction():
                conn.execute(_INSERT_STATE, _state_row(state))
                self._reconcile_findings(conn, state)
        except psycopg.Error:
            self.close()
            raise

    def _reconcile_findings(self, conn: psycopg.Connection, state: StrategyState) -> None:
        active: dict[str, Finding] = {finding.monitor: finding for finding in state.findings}
        for monitor, finding in active.items():
            existing = self.open_findings.get(monitor)
            summary = Jsonb(finding.summary)
            if existing is None:
                finding_id = uuid.uuid4()
                conn.execute(
                    _OPEN_FINDING,
                    (
                        finding_id,
                        state.vehicle_id,
                        monitor,
                        state.time,
                        finding.severity,
                        finding.score,
                        summary,
                    ),
                )
                self.open_findings[monitor] = finding_id
            else:
                conn.execute(_UPDATE_FINDING, (finding.severity, finding.score, summary, existing))
        for monitor in list(self.open_findings):
            if monitor not in active:
                conn.execute(_CLOSE_FINDING, (state.time, self.open_findings.pop(monitor)))

    # -- the connection ------------------------------------------------------------

    def close(self) -> None:
        """Close the connection if one is open; safe to call repeatedly."""
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - closing an already-broken connection
            pass

    def _connection(self) -> psycopg.Connection:
        if self._conn is not None and not self._conn.closed:
            return self._conn
        self._conn = psycopg.connect(self._dsn, autocommit=True)
        return self._conn


def _state_row(state: StrategyState) -> tuple:
    return (
        state.time,
        state.vehicle_id,
        state.session_id,
        state.trigger,
        state.lap_number,
        state.plan_revision,
        state.fuel_remaining_l,
        state.fuel_remaining_lo_l,
        state.fuel_remaining_hi_l,
        state.rebase_confidence,
        state.rebase_level_l,
        state.rebase_at,
        state.fuel_added_l,
        state.burn_l_per_lap,
        state.burn_sd,
        state.burn_laps,
        state.lap_time_ref_s,
        state.laps_to_dry_lo,
        state.laps_to_dry_hi,
        state.time_to_dry_s_lo,
        state.time_to_dry_s_hi,
        state.laps_remaining,
        state.stops_needed,
        state.window_open_lap,
        state.window_close_lap,
        state.target_lap_s,
        state.driver,
        state.driver_time_remaining_s,
        state.driver_total_remaining_s,
        state.refuel_elapsed_s,
        state.refuel_remaining_s,
        state.refuel_release_at,
        Jsonb(state.stop_plan, dumps=_dumps),
        Jsonb(state.plan_drift, dumps=_dumps),
    )


def _operator(when: str) -> str:
    """The comparison for an ``above`` / ``below`` threshold setting."""
    return "<=" if when == "below" else ">="


def surge_episodes(
    changes: list[tuple[datetime, bool]], last_sample: datetime | None
) -> list[tuple[datetime, datetime]]:
    """Turn the switch's state changes into (closed-at, opened-at) periods.

    A period still running ends at the last sample seen, so its length is
    how long the switch is *known* to have been closed, not how long ago it
    closed: a car that went silent with the light on has not been timed.
    """
    episodes: list[tuple[datetime, datetime]] = []
    opened: datetime | None = None
    for time, is_on in changes:
        if is_on and opened is None:
            opened = time
        elif not is_on and opened is not None:
            episodes.append((opened, time))
            opened = None
    if opened is not None and last_sample is not None and last_sample >= opened:
        episodes.append((opened, last_sample))
    return episodes


def _dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _float(value: object) -> float | None:
    return None if value is None else float(value)
