"""What the timing feed reads and writes, on one connection.

Writes are the four ``field_*`` tables, one transaction per document, and
the ``field.*`` findings in ``watch_findings`` -- in the pit-monitor pattern
(ADR 0011 decision 2): sync psycopg, the owner role, the connection dropped
on any error and redialled on the next use. Reads are from the stable views
only: ``v_session_active`` and ``v_race_plan`` for our car number, ``v_laps``
for the vehicle's own count of its laps.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from pit.timing_feed.model import Batch
from pit.timing_feed.reconcile import MONITORS, Finding

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OurCar:
    """The open session and the car number its plan says we race as."""

    session_id: str
    session_started: datetime
    car_number: str | None


_OUR_CAR = """
SELECT sa.session_id, sa.started, rp.car_number
FROM v_session_active sa
LEFT JOIN v_race_plan rp ON rp.session_id = sa.session_id
WHERE sa.vehicle_id = %s ORDER BY sa.started DESC LIMIT 1
"""
_VEHICLE_LAPS = """
SELECT count(*), max(crossed_at) FROM v_laps
WHERE vehicle_id = %s AND session_id = %s
"""
_CROSSINGS = """
SELECT crossed_at FROM v_laps
WHERE vehicle_id = %s AND session_id = %s AND crossed_at >= %s
ORDER BY crossed_at
"""
_OPEN_FINDINGS = """
SELECT finding_id, monitor FROM watch_findings
WHERE vehicle_id = %s AND closed_at IS NULL AND monitor = ANY(%s)
"""
_INSERT_SESSION = """
INSERT INTO field_session (time, source, session_name, event_type, flag_state, sub_status,
    time_remaining_s, laps_remaining, time_elapsed_s, track_temp)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
_INSERT_CAR = """
INSERT INTO field_cars (time, source, epoch, car_number, competitor_id, class, position,
    class_position, laps, last_lap_s, best_lap_s, gap_lead_s, gap_next_s, sec1_s, sec2_s,
    sec3_s, pit_count, in_pit, pit_flag, driver, state)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
_INSERT_LAP = """
INSERT INTO field_laps (time, source, car_number, competitor_id, lap_number, lap_time_s,
    position, class_position, gap_lead_s, gap_next_s, pit_count, sec1_s, sec2_s, sec3_s,
    flag_state, sub_status)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
_INSERT_PASSING = """
INSERT INTO field_passings (time, source, competitor_id, line, passing_type, active, tod,
    car_number)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""
_INSERT_METRIC = """
INSERT INTO pit_metrics (source, metric, time, value, value_text) VALUES (%s, %s, %s, %s, %s)
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


class FieldDatabase:
    """Owns one connection to the pit database and every statement on it."""

    def __init__(self, dsn: str, vehicle_id: str) -> None:
        self._dsn = dsn
        self._vehicle = vehicle_id
        self._conn: psycopg.Connection | None = None
        self.open_findings: dict[str, uuid.UUID] = {}

    # -- writes

    def write(self, batch: Batch) -> int:
        """Commit one document's rows together; returns the row count."""
        if not batch:
            return 0
        conn = self._connection()
        try:
            with conn.transaction():
                cursor = conn.cursor()
                if batch.session is not None:
                    s = batch.session
                    cursor.execute(
                        _INSERT_SESSION,
                        (
                            s.time,
                            s.source,
                            s.session_name,
                            s.event_type,
                            s.flag_state,
                            s.sub_status,
                            s.time_remaining_s,
                            s.laps_remaining,
                            s.time_elapsed_s,
                            s.track_temp,
                        ),
                    )
                if batch.cars:
                    cursor.executemany(
                        _INSERT_CAR,
                        [
                            (
                                c.time,
                                c.source,
                                c.epoch,
                                c.car_number,
                                c.competitor_id,
                                c.car_class,
                                c.position,
                                c.class_position,
                                c.laps,
                                c.last_lap_s,
                                c.best_lap_s,
                                c.gap_lead_s,
                                c.gap_next_s,
                                c.sec1_s,
                                c.sec2_s,
                                c.sec3_s,
                                c.pit_count,
                                c.in_pit,
                                c.pit_flag,
                                c.driver,
                                c.state,
                            )
                            for c in batch.cars
                        ],
                    )
                if batch.laps:
                    cursor.executemany(
                        _INSERT_LAP,
                        [
                            (
                                lap.time,
                                lap.source,
                                lap.car_number,
                                lap.competitor_id,
                                lap.lap_number,
                                lap.lap_time_s,
                                lap.position,
                                lap.class_position,
                                lap.gap_lead_s,
                                lap.gap_next_s,
                                lap.pit_count,
                                lap.sec1_s,
                                lap.sec2_s,
                                lap.sec3_s,
                                lap.flag_state,
                                lap.sub_status,
                            )
                            for lap in batch.laps
                        ],
                    )
                if batch.passings:
                    cursor.executemany(
                        _INSERT_PASSING,
                        [
                            (
                                p.time,
                                p.source,
                                p.competitor_id,
                                p.line,
                                p.passing_type,
                                p.active,
                                p.tod,
                                p.car_number,
                            )
                            for p in batch.passings
                        ],
                    )
        except psycopg.Error:
            self.close()
            raise
        return batch.rows

    def write_metric(self, at: datetime, metric: str, value: float) -> None:
        """One ``pit_metrics`` row under source ``timing-feed``."""
        conn = self._connection()
        try:
            conn.execute(_INSERT_METRIC, ("timing-feed", metric, at, value, None))
        except psycopg.Error:
            self.close()
            raise

    # -- reads

    def our_car(self) -> OurCar | None:
        """The open session for this vehicle and its plan's car number."""
        conn = self._connection()
        try:
            row = conn.execute(_OUR_CAR, (self._vehicle,)).fetchone()
        except psycopg.Error:
            self.close()
            raise
        if row is None:
            return None
        return OurCar(str(row[0]), row[1], None if row[2] is None else str(row[2]).strip())

    def vehicle_laps(self, session_id: str) -> tuple[int, datetime | None]:
        """How many laps the vehicle has recorded in the session, and the last."""
        conn = self._connection()
        try:
            count, last = conn.execute(_VEHICLE_LAPS, (self._vehicle, session_id)).fetchone()
        except psycopg.Error:
            self.close()
            raise
        return int(count or 0), last

    def vehicle_crossings(self, session_id: str, since: datetime) -> list[datetime]:
        conn = self._connection()
        try:
            rows = conn.execute(_CROSSINGS, (self._vehicle, session_id, since)).fetchall()
        except psycopg.Error:
            self.close()
            raise
        return [row[0] for row in rows]

    # -- findings

    def adopt_open_findings(self) -> int:
        """On startup, take over ``field.*`` findings a previous run left open."""
        conn = self._connection()
        try:
            rows = conn.execute(_OPEN_FINDINGS, (self._vehicle, list(MONITORS))).fetchall()
        except psycopg.Error:
            self.close()
            raise
        self.open_findings = {str(monitor): finding_id for finding_id, monitor in rows}
        return len(self.open_findings)

    def reconcile_findings(self, findings: list[Finding], at: datetime) -> None:
        """Open, update or close ``field.*`` findings so exactly ``findings`` are open."""
        conn = self._connection()
        active = {finding.monitor: finding for finding in findings}
        try:
            with conn.transaction():
                for monitor, finding in active.items():
                    summary = Jsonb(finding.summary, dumps=_dumps)
                    existing = self.open_findings.get(monitor)
                    if existing is None:
                        finding_id = uuid.uuid4()
                        conn.execute(
                            _OPEN_FINDING,
                            (
                                finding_id,
                                self._vehicle,
                                monitor,
                                at,
                                finding.severity,
                                finding.score,
                                summary,
                            ),
                        )
                        self.open_findings[monitor] = finding_id
                    else:
                        conn.execute(
                            _UPDATE_FINDING, (finding.severity, finding.score, summary, existing)
                        )
                for monitor in list(self.open_findings):
                    if monitor not in active:
                        conn.execute(_CLOSE_FINDING, (at, self.open_findings.pop(monitor)))
        except psycopg.Error:
            self.close()
            raise

    # -- the connection

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


def _dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
