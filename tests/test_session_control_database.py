"""Session-control Timescale ownership tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import psycopg
import pytest

from pit.db.migrate import apply_migrations
from pit.session_control import database as database_module
from pit.session_control.database import SessionDatabase, retry_backoff
from pit.session_control.state import SessionState

T0 = 1_782_900_000_000
MIN = 60_000
VEHICLE = "test-car"


def test_database_retry_backoff_is_bounded_and_resets_after_progress():
    assert retry_backoff(0.5, made_progress=False) == 1.0
    assert retry_backoff(8.0, made_progress=False) == 15.0
    assert retry_backoff(15.0, made_progress=True) == 0.5


def test_disconnected_database_queues_latest_state_per_session():
    async def exercise() -> None:
        database = SessionDatabase("postgresql://invalid", VEHICLE)
        first = SessionState()
        first.start_session("race", "Driver A", now_ms=T0)
        later = SessionState.from_dict(first.to_dict())
        later.change_driver("Driver B", now_ms=T0 + 90 * MIN)

        assert await database.record(first.to_dict()) is False
        assert await database.record(later.to_dict()) is False

        assert database.pending_count == 1
        assert database.pending_states()[0]["driver"] == "Driver B"

    asyncio.run(exercise())


def test_database_retry_queue_survives_restart_and_preserves_each_session(tmp_path: Path):
    async def exercise() -> None:
        queue_path = tmp_path / "database-queue.json"
        database = SessionDatabase("postgresql://invalid", VEHICLE, queue_path=queue_path)
        first = SessionState()
        first.start_session("race", "Driver A", now_ms=T0)
        first.end_session(now_ms=T0 + MIN)
        second = SessionState()
        second.start_session("test", "Driver B", now_ms=T0 + 2 * MIN)

        await database.record(first.to_dict())
        await database.record(second.to_dict())

        restored = SessionDatabase("postgresql://invalid", VEHICLE, queue_path=queue_path)
        assert restored.pending_count == 2
        assert [state["session_id"] for state in restored.pending_states()] == [
            first.session_id,
            second.session_id,
        ]

    asyncio.run(exercise())


def test_database_retry_queue_fails_closed_when_snapshot_is_invalid(tmp_path: Path):
    queue_path = tmp_path / "database-queue.json"
    valid = SessionState()
    valid.start_session("race", "Driver A", now_ms=T0)
    queue_path.write_text(
        json.dumps([{"status": "active", "session_id": "bad"}, valid.to_dict()]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="session_type"):
        SessionDatabase("postgresql://invalid", VEHICLE, queue_path=queue_path)

    assert queue_path.exists()


def test_database_retry_queue_write_failure_is_not_reported_as_queued(tmp_path: Path, monkeypatch):
    async def exercise() -> None:
        database = SessionDatabase(
            "postgresql://invalid",
            VEHICLE,
            queue_path=tmp_path / "database-queue.json",
        )
        state = SessionState()
        state.start_session("race", "Driver A", now_ms=T0)

        def fail_save(_path, _states):
            raise OSError("disk full")

        monkeypatch.setattr(database_module, "_save_pending", fail_save)
        with pytest.raises(OSError, match="disk full"):
            await database.record(state.to_dict())
        assert database.pending_count == 0

    asyncio.run(exercise())


def test_permanent_database_error_is_not_queued_as_an_outage(tmp_path: Path, monkeypatch):
    class Connection:
        closed = False

        async def close(self) -> None:
            self.closed = True

    async def exercise() -> None:
        database = SessionDatabase(
            "postgresql://invalid",
            VEHICLE,
            queue_path=tmp_path / "database-queue.json",
        )
        database._conn = Connection()  # type: ignore[assignment]
        state = SessionState()
        state.start_session("race", "Driver A", now_ms=T0)

        async def fail_write(_conn, _state):
            raise psycopg.IntegrityError("constraint violation")

        monkeypatch.setattr(database, "_write_state", fail_write)
        with pytest.raises(psycopg.IntegrityError):
            await database.record(state.to_dict())
        assert database.pending_count == 0

    asyncio.run(exercise())


def test_session_with_two_stints_round_trips_to_timescale(timescale_dsn):
    with psycopg.connect(timescale_dsn) as conn:
        apply_migrations(conn)

    async def exercise() -> None:
        database = SessionDatabase(timescale_dsn, VEHICLE)
        assert await database.connect_once()
        state = SessionState()

        state.start_session("race", "Driver A", track_name="Wanneroo", car="Car 7", now_ms=T0)
        assert await database.record(state.to_dict())
        state.change_driver("Driver B", now_ms=T0 + 90 * MIN)
        assert await database.record(state.to_dict())
        state.end_session(now_ms=T0 + 200 * MIN)
        assert await database.record(state.to_dict())
        await database.close()

    asyncio.run(exercise())

    with psycopg.connect(timescale_dsn) as conn:
        session = conn.execute(
            """
            SELECT session_id, vehicle_id, session_type, track_name, car,
                   extract(epoch FROM started) * 1000,
                   extract(epoch FROM ended) * 1000, status
            FROM sessions
            """
        ).fetchone()
        assert session is not None
        assert session[1:5] == (VEHICLE, "race", "Wanneroo", "Car 7")
        assert float(session[5]) == T0
        assert float(session[6]) == T0 + 200 * MIN
        assert session[7] == "ended"

        stints = conn.execute(
            """
            SELECT st.stint_number, d.name,
                   extract(epoch FROM st.started) * 1000,
                   extract(epoch FROM st.ended) * 1000
            FROM stints st
            JOIN drivers d USING (driver_id)
            ORDER BY st.stint_number
            """
        ).fetchall()
        assert [(row[0], row[1]) for row in stints] == [
            (1, "Driver A"),
            (2, "Driver B"),
        ]
        assert [float(row[2]) for row in stints] == [T0, T0 + 90 * MIN]
        assert [float(row[3]) for row in stints] == [
            T0 + 90 * MIN,
            T0 + 200 * MIN,
        ]


def test_backdated_driver_change_reattributes_laps(timescale_dsn):
    """Moving a stint boundary re-points the laps in the moved window.

    The vehicle stamps stint_number into lap.event from the session state it
    knew at the crossing, so laps completed between the real driver change
    and the late button press arrive attributed to the closed stint. The
    snapshot write re-windows them (docs/plan/PHASE5.md -> P5.6); a lap the
    ingest-writer left with a NULL stint is not touched.
    """
    with psycopg.connect(timescale_dsn) as conn:
        apply_migrations(conn)

    state = SessionState()
    state.start_session("race", "Driver A", track_name="Wanneroo", now_ms=T0)

    def record_snapshot(snapshot: dict[str, object]) -> None:
        async def push() -> None:
            database = SessionDatabase(timescale_dsn, VEHICLE)
            assert await database.connect_once()
            assert await database.record(snapshot)
            await database.close()

        asyncio.run(push())

    record_snapshot(state.to_dict())

    # Laps as the ingest-writer wrote them: all stamped stint 1, because the
    # driver-change button had not been pressed yet. Lap 4 arrived before
    # session-control's rows existed and carries no stint at all.
    with psycopg.connect(timescale_dsn) as conn:
        stint_1 = conn.execute("SELECT stint_id FROM stints WHERE stint_number = 1").fetchone()
        assert stint_1 is not None
        for lap_number, crossed_ms, stint_id in [
            (1, T0 + 10 * MIN, stint_1[0]),
            (2, T0 + 35 * MIN, stint_1[0]),
            (3, T0 + 50 * MIN, stint_1[0]),
            (4, T0 + 40 * MIN, None),
        ]:
            conn.execute(
                """
                INSERT INTO laps (vehicle_id, session_id, stint_id, lap_number, crossed_at)
                VALUES (%s, %s, %s, %s, to_timestamp(%s / 1000.0))
                """,
                (VEHICLE, state.session_id, stint_id, lap_number, crossed_ms),
            )

    # The swap actually happened at T0+30min; the button is pressed later
    # and backdated (the controller passes `at` through as now_ms).
    state.change_driver("Driver B", now_ms=T0 + 30 * MIN)
    record_snapshot(state.to_dict())

    with psycopg.connect(timescale_dsn) as conn:
        rows = conn.execute(
            """
            SELECT l.lap_number, st.stint_number
            FROM laps l
            LEFT JOIN stints st USING (stint_id)
            ORDER BY l.lap_number
            """
        ).fetchall()
        assert rows == [(1, 1), (2, 2), (3, 2), (4, None)]
