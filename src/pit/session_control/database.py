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

logger = logging.getLogger(__name__)

_CONNECT_BACKOFF_START_S = 0.5
_CONNECT_BACKOFF_MAX_S = 15.0


class SessionDatabase:
    """Own ``drivers``, ``sessions`` and ``stints`` with outage retry."""

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
                    if self._pending.pop(session_id, None) is not None:
                        self._persist_pending()
                    return True
                except (TimeoutError, psycopg.Error, ValueError, TypeError) as exc:
                    self.errors += 1
                    logger.warning("session-control: database write deferred: %s", exc)
                    await self._drop_connection()
            self._queue(state)
            return False

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
            except (TimeoutError, psycopg.Error, ValueError, TypeError) as exc:
                self.errors += 1
                logger.warning("session-control: queued database write failed: %s", exc)
                await self._drop_connection()
                return False
            del self._pending[session_id]
            self._persist_pending()
            return True

    def _queue(self, state: dict[str, object]) -> None:
        session_id = _as_str(state.get("session_id"))
        self._pending[session_id] = copy.deepcopy(state)
        self._persist_pending()
        self._wake.set()

    def _persist_pending(self) -> None:
        if self._queue_path is None:
            return
        try:
            _save_pending(self._queue_path, list(self._pending.values()))
        except OSError as exc:
            logger.error("session-control: cannot persist database retry queue: %s", exc)

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
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError("retry queue must be a JSON array")
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("retry queue items must be JSON objects")
            state = dict(item)
            session_id = _as_str(state.get("session_id"))
            if not session_id:
                raise ValueError("queued state requires session_id")
            pending[session_id] = state
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("session-control: ignoring invalid database retry queue %s: %s", path, exc)
        pending.clear()
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
