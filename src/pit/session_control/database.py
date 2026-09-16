"""Timescale persistence and retry queue for session-control.

The controller calls ``record`` before submitting a NATS update. When the
held connection is healthy, the relational rows commit first so ingest-writer
can resolve session/stint foreign keys as soon as the command reaches the car.
During an outage, full session snapshots are queued durably per session and
retried; NATS publication and the HTTP response are never held behind that
retry.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import tempfile
from collections import OrderedDict
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from pit.session_control.plan import RacePlan
from pit.session_control.state import SessionState

logger = logging.getLogger(__name__)

_CONNECT_BACKOFF_START_S = 0.5
_CONNECT_BACKOFF_MAX_S = 15.0


class PlanUnavailable(RuntimeError):
    """The race plan could not be read or written because the database is unreachable."""


_PLAN_COLUMNS = (
    "session_id, revision, race_end_at, race_end_laps, end_authority, tank_l, usable_fuel_l, "
    "refuel_min_s, service_typical_s, driver_limits, planned_stops, car_number, updated_at, "
    "updated_by"
)


_STRATEGY_COLUMNS = (
    "time, session_id, trigger, lap_number, plan_revision, fuel_remaining_l, "
    "fuel_remaining_lo_l, fuel_remaining_hi_l, rebase_confidence, burn_l_per_lap, burn_sd, "
    "burn_laps, laps_to_dry_lo, laps_to_dry_hi, time_to_dry_s_lo, laps_remaining, stops_needed, "
    "window_open_lap, window_close_lap, target_lap_s, driver, driver_time_remaining_s, "
    "driver_total_remaining_s, refuel_remaining_s, refuel_release_at, stop_plan, plan_drift"
)


class SessionDatabase:
    """Own ``drivers``, ``sessions``, ``stints`` and ``race_plans`` with outage retry."""

    def __init__(
        self,
        dsn: str,
        vehicle_id: str,
        *,
        operation_timeout_s: float = 2.0,
        queue_path: Path | None = None,
    ) -> None:
        self._dsn = dsn
        self._vehicle_id = vehicle_id
        self._operation_timeout_s = operation_timeout_s
        self._queue_path = queue_path
        self._conn: psycopg.AsyncConnection | None = None
        self._pending = _load_pending(queue_path)
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self.connects = 0
        self.errors = 0

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._conn.closed

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def pending_states(self) -> list[dict[str, object]]:
        """Copies of queued snapshots, primarily for health and tests."""
        return [copy.deepcopy(state) for state in self._pending.values()]

    async def connect_once(self) -> bool:
        """Attempt one bounded connection; false leaves retry to ``run``."""
        async with self._lock:
            if self.connected:
                return True
            try:
                conn = await asyncio.wait_for(
                    psycopg.AsyncConnection.connect(
                        self._dsn,
                        autocommit=False,
                        connect_timeout=max(1, int(self._operation_timeout_s)),
                    ),
                    timeout=self._operation_timeout_s + 0.5,
                )
            except (TimeoutError, psycopg.Error) as exc:
                self.errors += 1
                logger.warning("session-control: cannot reach database: %s", exc)
                return False
            self._conn = conn
            self.connects += 1
            logger.info("session-control: database connected")
            return True

    async def record(self, state: dict[str, object]) -> bool:
        """Commit a snapshot now when connected, otherwise queue it for retry."""
        state = SessionState.from_dict(state).to_dict()
        session_id = _as_str(state.get("session_id"))
        if not session_id:
            return True
        async with self._lock:
            if self.connected:
                try:
                    await asyncio.wait_for(
                        self._write_state(self._require(), state),
                        timeout=self._operation_timeout_s,
                    )
                    pending = self._pending.pop(session_id, None)
                    if pending is not None:
                        try:
                            self._persist_pending()
                        except OSError as exc:
                            # The DB commit is durable. Retaining this snapshot
                            # only causes a harmless idempotent replay later.
                            self._pending[session_id] = pending
                            logger.error(
                                "session-control: cannot update database retry queue: %s", exc
                            )
                    return True
                except (TimeoutError, psycopg.OperationalError, psycopg.InterfaceError) as exc:
                    self.errors += 1
                    logger.warning("session-control: database write deferred: %s", exc)
                    await self._drop_connection()
            self._queue(state)
            return False

    async def save_plan(self, session_id: str, plan: RacePlan) -> dict[str, object]:
        """Insert the next revision of the session's race plan and return it.

        No retry queue, unlike session snapshots: a plan the operator cannot
        confirm was saved is worse than one they have to submit again, so an
        unreachable database is a 503 to the form, not a promise.
        """
        async with self._lock:
            if not self.connected:
                raise PlanUnavailable("database unavailable; the plan was not saved")
            try:
                return await asyncio.wait_for(
                    self._insert_plan(self._require(), session_id, plan),
                    timeout=self._operation_timeout_s,
                )
            except psycopg.IntegrityError as exc:
                # The session row is not there yet -- it is in the retry
                # queue from an outage -- so the plan cannot reference it.
                # The transaction rolled back; the connection is fine.
                raise PlanUnavailable(
                    "the session is not in the database yet; try again shortly"
                ) from exc
            except (TimeoutError, psycopg.OperationalError, psycopg.InterfaceError) as exc:
                self.errors += 1
                await self._drop_connection()
                raise PlanUnavailable("database unavailable; the plan was not saved") from exc

    async def load_plan(self, session_id: str) -> dict[str, object] | None:
        """The latest revision of the session's plan, or None if it has none."""
        async with self._lock:
            if not self.connected:
                raise PlanUnavailable("database unavailable; the plan cannot be read")
            try:
                return await asyncio.wait_for(
                    self._select_plan(self._require(), session_id),
                    timeout=self._operation_timeout_s,
                )
            except (TimeoutError, psycopg.OperationalError, psycopg.InterfaceError) as exc:
                self.errors += 1
                await self._drop_connection()
                raise PlanUnavailable("database unavailable; the plan cannot be read") from exc

    async def load_strategy(self, session_id: str) -> dict[str, object] | None:
        """The strategy service's latest evaluation for the session, or None.

        Read-only, from `v_strategy_latest` (P7.9): the session UI shows the
        numbers the crew would otherwise open a dashboard for. None when
        the strategy service has not evaluated this session yet.
        """
        async with self._lock:
            if not self.connected:
                raise PlanUnavailable("database unavailable; strategy cannot be read")
            try:
                return await asyncio.wait_for(
                    self._select_strategy(self._require(), session_id),
                    timeout=self._operation_timeout_s,
                )
            except (TimeoutError, psycopg.OperationalError, psycopg.InterfaceError) as exc:
                self.errors += 1
                await self._drop_connection()
                raise PlanUnavailable("database unavailable; strategy cannot be read") from exc

    async def _select_strategy(
        self, conn: psycopg.AsyncConnection, session_id: str
    ) -> dict[str, object] | None:
        async with conn.transaction():
            row = await (
                await conn.execute(
                    f"SELECT {_STRATEGY_COLUMNS} FROM v_strategy_latest WHERE session_id = %s",
                    (session_id,),
                )
            ).fetchone()
        return _strategy_row(row) if row is not None else None

    async def _insert_plan(
        self, conn: psycopg.AsyncConnection, session_id: str, plan: RacePlan
    ) -> dict[str, object]:
        async with conn.transaction():
            row = await (
                await conn.execute(
                    f"""
                    INSERT INTO race_plans (
                        session_id, revision, race_end_at, race_end_laps, end_authority,
                        tank_l, usable_fuel_l, refuel_min_s, service_typical_s,
                        driver_limits, planned_stops, car_number, updated_by
                    )
                    SELECT %s, COALESCE(MAX(revision), 0) + 1, %s, %s, %s, %s, %s, %s, %s,
                           %s::jsonb, %s::jsonb, %s, %s
                    FROM race_plans WHERE session_id = %s
                    RETURNING {_PLAN_COLUMNS}
                    """,
                    (
                        session_id,
                        _timestamp(plan.race_end_at_ms) if plan.race_end_at_ms else None,
                        plan.race_end_laps,
                        plan.end_authority,
                        plan.tank_l,
                        plan.usable_fuel_l,
                        plan.refuel_min_s,
                        plan.service_typical_s,
                        json.dumps(plan.driver_limits),
                        json.dumps(plan.to_dict()["planned_stops"]),
                        plan.car_number,
                        plan.updated_by,
                        session_id,
                    ),
                )
            ).fetchone()
        assert row is not None
        return _plan_row(row)

    async def _select_plan(
        self, conn: psycopg.AsyncConnection, session_id: str
    ) -> dict[str, object] | None:
        async with conn.transaction():
            row = await (
                await conn.execute(
                    f"SELECT {_PLAN_COLUMNS} FROM v_race_plan WHERE session_id = %s",
                    (session_id,),
                )
            ).fetchone()
        return _plan_row(row) if row is not None else None

    async def run(self, stop: asyncio.Event) -> None:
        """Reconnect and drain queued snapshots until stopped."""
        backoff = _CONNECT_BACKOFF_START_S
        while not stop.is_set():
            if not self.connected and not await self.connect_once():
                await _sleep_unless(stop, backoff)
                backoff = retry_backoff(backoff, made_progress=False)
                continue
            flushed = await self._flush_one()
            if not self.connected:
                await _sleep_unless(stop, backoff)
                backoff = retry_backoff(backoff, made_progress=False)
                continue
            if flushed:
                backoff = retry_backoff(backoff, made_progress=True)
            else:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=0.5)
                except TimeoutError:
                    pass
        await self.close()

    async def close(self) -> None:
        async with self._lock:
            await self._drop_connection()

    async def _flush_one(self) -> bool:
        async with self._lock:
            if not self._pending or not self.connected:
                return False
            session_id, state = next(iter(self._pending.items()))
            try:
                await asyncio.wait_for(
                    self._write_state(self._require(), state),
                    timeout=self._operation_timeout_s,
                )
            except (TimeoutError, psycopg.OperationalError, psycopg.InterfaceError) as exc:
                self.errors += 1
                logger.warning("session-control: queued database write failed: %s", exc)
                await self._drop_connection()
                return False
            state = self._pending.pop(session_id)
            try:
                self._persist_pending()
            except OSError as exc:
                self.errors += 1
                self._pending[session_id] = state
                logger.error("session-control: cannot update database retry queue: %s", exc)
                return False
            return True

    def _queue(self, state: dict[str, object]) -> None:
        session_id = _as_str(state.get("session_id"))
        previous = self._pending.get(session_id)
        self._pending[session_id] = copy.deepcopy(state)
        try:
            self._persist_pending()
        except OSError:
            if previous is None:
                del self._pending[session_id]
            else:
                self._pending[session_id] = previous
            raise
        self._wake.set()

    def _persist_pending(self) -> None:
        if self._queue_path is None:
            return
        _save_pending(self._queue_path, list(self._pending.values()))

    async def _drop_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.close()
            except Exception:  # noqa: BLE001 - closing a dead connection
                pass

    async def _write_state(
        self, conn: psycopg.AsyncConnection, state: Mapping[str, object]
    ) -> None:
        session_id = _required_str(state, "session_id")
        status = _required_str(state, "status")
        started = _timestamp(_required_int(state, "session_start"))
        closed = _closed_stints(state.get("stints"))
        ended = (
            max((_required_int(stint, "end_ms") for stint in closed), default=None)
            if status == "ended"
            else None
        )
        ended_at = _timestamp(ended) if ended is not None else None

        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO sessions (
                    session_id, vehicle_id, session_type, track_name, car, started, ended, status
                ) VALUES (%s, %s, %s, NULLIF(%s, ''), NULLIF(%s, ''), %s, %s, %s)
                ON CONFLICT (session_id) DO UPDATE SET
                    vehicle_id = EXCLUDED.vehicle_id,
                    session_type = EXCLUDED.session_type,
                    track_name = EXCLUDED.track_name,
                    car = EXCLUDED.car,
                    started = EXCLUDED.started,
                    ended = EXCLUDED.ended,
                    status = EXCLUDED.status
                """,
                (
                    session_id,
                    self._vehicle_id,
                    _required_str(state, "session_type"),
                    _as_str(state.get("track_name")),
                    _as_str(state.get("car")),
                    started,
                    ended_at,
                    status,
                ),
            )
            stints = list(closed)
            if status == "active":
                stints.append(
                    {
                        "driver": _required_str(state, "driver"),
                        "stint_number": _required_int(state, "stint_number"),
                        "start_ms": _required_int(state, "stint_start"),
                        "end_ms": None,
                    }
                )
            for stint in stints:
                driver_id = await self._upsert_driver(conn, _required_str(stint, "driver"))
                end_ms = stint["end_ms"]
                await conn.execute(
                    """
                    INSERT INTO stints (
                        session_id, stint_number, driver_id, started, ended
                    ) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (session_id, stint_number) DO UPDATE SET
                        driver_id = EXCLUDED.driver_id,
                        started = EXCLUDED.started,
                        ended = EXCLUDED.ended
                    """,
                    (
                        session_id,
                        _required_int(stint, "stint_number"),
                        driver_id,
                        _timestamp(_required_int(stint, "start_ms")),
                        _timestamp(end_ms) if isinstance(end_ms, int) else None,
                    ),
                )
            # Backdating a driver change moves a stint boundary after laps in
            # the moved window were already attributed: the vehicle stamps
            # stint_number into lap.event from the session state it knew at
            # the crossing, so laps recorded between the real change and the
            # late button press point at the closed stint. Re-window them
            # from the stint rows just written, in the same transaction. This
            # is a deliberate, narrow crossing of an ownership boundary —
            # `laps` belongs to the ingest-writer — and an explicit operator
            # amendment is the one case where the vehicle's stamp is known to
            # be wrong (docs/plan/PHASE5.md -> P5.6).
            #
            # Snapshot-derived rather than transition-carried on purpose: the
            # retry queue replays whole snapshots, and this UPDATE is
            # idempotent, so a queued backdate converges to the same rows as
            # a live one. Scope: only laps of this session that already point
            # at a stint; a lap the writer left with a NULL stint (it arrived
            # before our rows did) is the writer's to resolve, not ours.
            await conn.execute(
                """
                UPDATE laps l
                SET stint_id = s.stint_id
                FROM stints s
                WHERE l.session_id = %s
                  AND s.session_id = l.session_id
                  AND l.stint_id IS NOT NULL
                  AND l.stint_id <> s.stint_id
                  AND l.crossed_at >= s.started
                  AND (s.ended IS NULL OR l.crossed_at < s.ended)
                """,
                (session_id,),
            )

    @staticmethod
    async def _upsert_driver(conn: psycopg.AsyncConnection, name: str) -> int:
        row = await (
            await conn.execute(
                """
                INSERT INTO drivers (name) VALUES (%s)
                ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
                RETURNING driver_id
                """,
                (name,),
            )
        ).fetchone()
        if row is None:
            raise RuntimeError("driver upsert returned no id")
        return int(row[0])

    def _require(self) -> psycopg.AsyncConnection:
        if not self.connected or self._conn is None:
            raise RuntimeError("database is not connected")
        return self._conn


def _load_pending(path: Path | None) -> OrderedDict[str, dict[str, object]]:
    pending: OrderedDict[str, dict[str, object]] = OrderedDict()
    if path is None:
        return pending
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return pending
    value = json.loads(raw)
    if not isinstance(value, list):
        raise ValueError("retry queue must be a JSON array")
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("retry queue items must be JSON objects")
        state = SessionState.from_dict(dict(item)).to_dict()
        session_id = _as_str(state["session_id"])
        pending[session_id] = state
    return pending


def _save_pending(path: Path, states: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(states, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _closed_stints(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise TypeError("stints must be a list")
    if not all(isinstance(item, dict) for item in value):
        raise TypeError("each stint must be an object")
    return [dict(item) for item in value]


def _required_str(data: Mapping[str, object], key: str) -> str:
    value = _as_str(data.get(key))
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _required_int(data: Mapping[str, object], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _timestamp(epoch_ms: int) -> datetime:
    return datetime.fromtimestamp(epoch_ms / 1000.0, tz=UTC)


async def _sleep_unless(stop: asyncio.Event, delay: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except TimeoutError:
        pass


def retry_backoff(current: float, *, made_progress: bool) -> float:
    """Advance bounded retry delay, resetting once a queued state commits."""
    if made_progress:
        return _CONNECT_BACKOFF_START_S
    return min(current * 2, _CONNECT_BACKOFF_MAX_S)


def _plan_row(row: tuple) -> dict[str, object]:
    """A race_plans row in the shape the API returns (epoch ms, not datetimes)."""
    (
        session_id,
        revision,
        race_end_at,
        race_end_laps,
        end_authority,
        tank_l,
        usable_fuel_l,
        refuel_min_s,
        service_typical_s,
        driver_limits,
        planned_stops,
        car_number,
        updated_at,
        updated_by,
    ) = row
    return {
        "session_id": session_id,
        "revision": revision,
        "race_end_at_ms": int(race_end_at.timestamp() * 1000) if race_end_at else None,
        "race_end_laps": race_end_laps,
        "end_authority": end_authority,
        "tank_l": tank_l,
        "usable_fuel_l": usable_fuel_l,
        "refuel_min_s": refuel_min_s,
        "service_typical_s": service_typical_s,
        "driver_limits": driver_limits,
        "planned_stops": planned_stops,
        "car_number": car_number,
        "updated_at_ms": int(updated_at.timestamp() * 1000),
        "updated_by": updated_by,
    }


def _strategy_row(row: tuple) -> dict[str, object]:
    """A v_strategy_latest row in the API's shape (epoch ms, not datetimes)."""
    names = [name.strip() for name in _STRATEGY_COLUMNS.split(",")]
    data = dict(zip(names, row, strict=True))
    for name in ("time", "refuel_release_at"):
        stamp = data.pop(name)
        data[f"{name}_ms"] = int(stamp.timestamp() * 1000) if stamp else None
    return data
