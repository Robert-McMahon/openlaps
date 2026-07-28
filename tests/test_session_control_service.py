"""Session-control persistence and transition orchestration tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from pit.session_control.service import (
    SessionController,
    SessionPersistenceError,
    StateFile,
    _wait_for_stop_or_task_failure,
    load_roster,
)

T0 = 1_782_900_000_000


class RecordingDatabase:
    def __init__(self, events: list[str], *, succeeds: bool = True) -> None:
        self.events = events
        self.succeeds = succeeds
        self.states: list[dict[str, object]] = []

    async def record(self, state: dict[str, object]) -> bool:
        self.events.append("database")
        self.states.append(state)
        return self.succeeds


class RecordingPublisher:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.payloads: list[dict[str, object]] = []

    def submit(self, payload: dict[str, object]) -> None:
        self.events.append("publish")
        self.payloads.append(payload)


def test_state_file_round_trips_and_replaces_atomically(tmp_path: Path):
    path = tmp_path / "nested" / "session.json"
    state_file = StateFile(path)
    state = state_file.load()
    state.start_session("race", "Driver A", now_ms=T0)

    state_file.save(state)

    restored = state_file.load()
    assert restored.session_id == state.session_id
    assert restored.driver == "Driver A"
    assert restored.status == "active"
    assert list(path.parent.glob(f".{path.name}.*")) == []


def test_corrupt_state_file_is_ignored(tmp_path: Path):
    path = tmp_path / "session.json"
    path.write_text("{broken", encoding="utf-8")

    state = StateFile(path).load()

    assert state.status == "none"


def test_structurally_invalid_state_file_is_ignored(tmp_path: Path):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"status": "active"}), encoding="utf-8")

    state = StateFile(path).load()

    assert state.status == "none"


def test_load_roster_returns_configured_placeholders(tmp_path: Path):
    path = tmp_path / "roster.json"
    path.write_text(
        json.dumps(
            {
                "drivers": ["Driver A", "Driver B"],
                "session_types": ["practice", "race"],
            }
        ),
        encoding="utf-8",
    )

    assert load_roster(path) == {
        "drivers": ["Driver A", "Driver B"],
        "session_types": ["practice", "race"],
    }


def test_happy_path_records_database_before_publish_and_persists(tmp_path: Path):
    async def exercise() -> None:
        events: list[str] = []
        state_file = StateFile(tmp_path / "session.json")
        database = RecordingDatabase(events)
        publisher = RecordingPublisher(events)
        controller = SessionController(state_file, database, publisher)

        payload = await controller.act(
            "start",
            {
                "session_type": "race",
                "driver": "Driver A",
                "track_name": "Wanneroo",
                "car": "Car 7",
            },
            now_ms=T0,
        )

        assert events == ["database", "publish"]
        assert database.states[0]["session_id"] == payload["session_id"]
        assert publisher.payloads == [payload]
        assert state_file.load().session_id == payload["session_id"]

    asyncio.run(exercise())


def test_database_outage_does_not_block_publish_or_transition(tmp_path: Path):
    async def exercise() -> None:
        events: list[str] = []
        database = RecordingDatabase(events, succeeds=False)
        publisher = RecordingPublisher(events)
        controller = SessionController(StateFile(tmp_path / "session.json"), database, publisher)

        payload = await controller.act(
            "start", {"session_type": "test", "driver": "Driver A"}, now_ms=T0
        )

        assert payload["status"] == "active"
        assert events == ["database", "publish"]
        assert publisher.payloads[-1] == payload

    asyncio.run(exercise())


def test_failed_state_write_rolls_back_without_database_or_publish(tmp_path: Path, monkeypatch):
    async def exercise() -> None:
        events: list[str] = []
        state_file = StateFile(tmp_path / "session.json")
        monkeypatch.setattr(state_file, "save", lambda _state: False)
        controller = SessionController(
            state_file,
            RecordingDatabase(events),
            RecordingPublisher(events),
        )

        with pytest.raises(SessionPersistenceError, match="persist"):
            await controller.act(
                "start",
                {"session_type": "test", "driver": "Driver A"},
                now_ms=T0,
            )

        assert controller.state.status == "none"
        assert events == []

    asyncio.run(exercise())


def test_failed_database_retry_queue_does_not_publish(tmp_path: Path):
    class FailingDatabase:
        async def record(self, state: dict[str, object]) -> bool:
            del state
            raise OSError("disk full")

    async def exercise() -> None:
        events: list[str] = []
        state_file = StateFile(tmp_path / "session.json")
        controller = SessionController(
            state_file,
            FailingDatabase(),
            RecordingPublisher(events),
        )

        with pytest.raises(SessionPersistenceError, match="database retry queue"):
            await controller.act(
                "start",
                {"session_type": "test", "driver": "Driver A"},
                now_ms=T0,
            )

        assert controller.state.status == "none"
        assert StateFile(state_file.path).load().status == "none"
        assert events == []

    asyncio.run(exercise())


def test_permanent_database_error_rolls_back_transition(tmp_path: Path):
    class FailingDatabase:
        async def record(self, state: dict[str, object]) -> bool:
            del state
            raise RuntimeError("schema mismatch")

    async def exercise() -> None:
        events: list[str] = []
        state_file = StateFile(tmp_path / "session.json")
        controller = SessionController(
            state_file,
            FailingDatabase(),
            RecordingPublisher(events),
        )

        with pytest.raises(RuntimeError, match="schema mismatch"):
            await controller.act(
                "start",
                {"session_type": "test", "driver": "Driver A"},
                now_ms=T0,
            )

        assert controller.state.status == "none"
        assert StateFile(state_file.path).load().status == "none"
        assert events == []

    asyncio.run(exercise())


def test_background_task_failure_is_propagated():
    async def exercise() -> None:
        async def fail() -> None:
            await asyncio.sleep(0)
            raise RuntimeError("permanent database error")

        stop = asyncio.Event()
        task = asyncio.create_task(fail(), name="session-database")

        with pytest.raises(RuntimeError, match="session-database failed") as caught:
            await _wait_for_stop_or_task_failure(stop, (task,))

        assert isinstance(caught.value.__cause__, RuntimeError)
        assert "permanent database error" in str(caught.value.__cause__)

    asyncio.run(exercise())


def test_startup_republishes_persisted_current_state(tmp_path: Path):
    async def exercise() -> None:
        events: list[str] = []
        state_file = StateFile(tmp_path / "session.json")
        state = state_file.load()
        state.start_session("practice", "Driver A", now_ms=T0)
        state_file.save(state)
        publisher = RecordingPublisher(events)
        controller = SessionController(state_file, RecordingDatabase(events), publisher)

        controller.republish_current()

        assert len(publisher.payloads) == 1
        assert publisher.payloads[0]["session_id"] == state.session_id

    asyncio.run(exercise())
