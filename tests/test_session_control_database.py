"""Session-control Timescale ownership tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import psycopg

from pit.db.migrate import apply_migrations
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
