"""Tests for the pit session/driver-stint state machine."""

import pytest

from pit.session_control.state import SessionError, SessionState

T0 = 1_782_900_000_000
MIN = 60_000


def started(driver: str = "Alice", session_type: str = "race") -> SessionState:
    state = SessionState()
    state.start_session(session_type, driver, track_name="Wanneroo", now_ms=T0)
    return state


def test_start_session_payload():
    state = SessionState()
    payload = state.start_session("race", "Alice", track_name="Wanneroo", car="Car 7", now_ms=T0)

    assert payload == {
        "session_id": payload["session_id"],
        "session_type": "race",
        "driver": "Alice",
        "track_name": "Wanneroo",
        "car": "Car 7",
        "stint_number": 1,
        "session_start": T0,
        "stint_start": T0,
        "status": "active",
        "timestamp": T0,
    }
    assert str(payload["session_id"]).startswith("20260701-1000-")
    assert str(payload["session_id"]).endswith("-race")


def test_start_validation():
    state = SessionState()
    with pytest.raises(SessionError, match="session_type"):
        state.start_session("grand prix", "Alice")
    with pytest.raises(SessionError, match="driver is required"):
        state.start_session("race", "  ")
    state.start_session("race", "Alice", now_ms=T0)
    with pytest.raises(SessionError, match="already active"):
        state.start_session("race", "Bob")


def test_driver_change_logs_stint():
    state = started()

    payload = state.change_driver("Bob", now_ms=T0 + 90 * MIN)

    assert payload["driver"] == "Bob"
    assert payload["stint_number"] == 2
    assert payload["stint_start"] == T0 + 90 * MIN
    assert payload["session_start"] == T0
    assert state.stints == [
        {
            "driver": "Alice",
            "stint_number": 1,
            "start_ms": T0,
            "end_ms": T0 + 90 * MIN,
        }
    ]


def test_driver_change_validation():
    state = SessionState()
    with pytest.raises(SessionError, match="no active session"):
        state.change_driver("Bob")
    state.start_session("race", "Alice", now_ms=T0)
    with pytest.raises(SessionError, match="already the active driver"):
        state.change_driver("Alice")
    with pytest.raises(SessionError, match="driver is required"):
        state.change_driver("")
    with pytest.raises(SessionError, match="before the active stint"):
        state.change_driver("Bob", now_ms=T0 - 1)
    assert state.stints == []


def test_end_session_closes_last_stint():
    state = started()
    state.change_driver("Bob", now_ms=T0 + 90 * MIN)

    payload = state.end_session(now_ms=T0 + 200 * MIN)

    assert payload["status"] == "ended"
    assert len(state.stints) == 2
    assert state.stints[1] == {
        "driver": "Bob",
        "stint_number": 2,
        "start_ms": T0 + 90 * MIN,
        "end_ms": T0 + 200 * MIN,
    }
    with pytest.raises(SessionError, match="no active session"):
        state.end_session()


def test_end_rejects_time_before_active_stint():
    state = started()

    with pytest.raises(SessionError, match="before the active stint"):
        state.end_session(now_ms=T0 - 1)

    assert state.status == "active"
    assert state.stints == []


def test_new_session_after_end():
    state = started()
    state.end_session(now_ms=T0 + MIN)

    payload = state.start_session("practice", "Bob", now_ms=T0 + 2 * MIN)

    assert payload["status"] == "active"
    assert payload["stint_number"] == 1
    assert state.stints == []


def test_session_ids_do_not_collide_at_the_same_timestamp():
    state = SessionState()
    first = state.start_session("race", "Alice", now_ms=T0)["session_id"]
    state.end_session(now_ms=T0)

    second = state.start_session("race", "Bob", now_ms=T0)["session_id"]

    assert first != second


def test_roundtrip_serialisation():
    state = started()
    state.change_driver("Bob", now_ms=T0 + MIN)

    restored = SessionState.from_dict(state.to_dict())

    assert restored.driver == "Bob"
    assert restored.status == "active"
    assert restored.stint_number == 2
    assert restored.stints == state.stints
    restored.end_session(now_ms=T0 + 2 * MIN)
    assert restored.status == "ended"


@pytest.mark.parametrize(
    "data",
    [
        {"status": "active"},
        {
            "status": "active",
            "session_id": "s-1",
            "session_type": "race",
            "driver": "Alice",
            "stint_number": 1,
            "session_start": T0,
            "stint_start": T0,
            "stints": [{"driver": "Alice"}],
        },
    ],
)
def test_restore_rejects_structurally_invalid_state(data):
    with pytest.raises((TypeError, ValueError)):
        SessionState.from_dict(data)
