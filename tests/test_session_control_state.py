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


def test_amend_stint_start_moves_the_boundary_both_directions():
    state = started()
    state.change_driver("Bob", now_ms=T0 + 30 * MIN)

    payload = state.amend_stint_start(T0 + 20 * MIN)
    assert payload["driver"] == "Bob"
    assert payload["stint_number"] == 2
    assert payload["stint_start"] == T0 + 20 * MIN
    assert payload["timestamp"] == T0 + 20 * MIN
    assert state.stints[-1]["end_ms"] == T0 + 20 * MIN

    payload = state.amend_stint_start(T0 + 40 * MIN)
    assert payload["stint_start"] == T0 + 40 * MIN
    assert state.stints[-1]["end_ms"] == T0 + 40 * MIN

    # The corrected state still satisfies the strict restore validator.
    restored = SessionState.from_dict(state.to_dict())
    assert restored.stint_start_ms == T0 + 40 * MIN
    assert restored.stints[-1]["end_ms"] == T0 + 40 * MIN


def test_amend_stint_start_validation():
    with pytest.raises(SessionError, match="no active session"):
        SessionState().amend_stint_start(T0)

    state = started()
    with pytest.raises(SessionError, match="first stint"):
        state.amend_stint_start(T0 + MIN)

    state.change_driver("Bob", now_ms=T0 + 30 * MIN)
    with pytest.raises(SessionError, match="previous stint started"):
        state.amend_stint_start(T0 - MIN)
    # A zero-length previous stint is allowed, matching change_driver's bound.
    assert state.amend_stint_start(T0)["stint_start"] == T0


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


def _stint(driver: str, start: int, end: int | None) -> dict[str, object]:
    return {"driver": driver, "start_ms": start, "end_ms": end}


def test_driver_change_can_leave_a_gap_for_the_stop():
    state = started()

    payload = state.change_driver("Bob", now_ms=T0 + 34 * MIN, out_ms=T0 + 30 * MIN)

    assert payload["stint_start"] == T0 + 34 * MIN
    assert state.stints[-1]["end_ms"] == T0 + 30 * MIN
    assert SessionState.from_dict(state.to_dict()).stints == state.stints

    with pytest.raises(SessionError, match="before the old one got out"):
        state.change_driver("Cara", now_ms=T0 + 50 * MIN, out_ms=T0 + 60 * MIN)
    with pytest.raises(SessionError, match="before the active stint"):
        state.change_driver("Cara", now_ms=T0 + 50 * MIN, out_ms=T0 + 33 * MIN)


def test_amend_timeline_adds_a_missed_driver_change_to_an_active_session():
    state = started()
    session_id = state.session_id

    payload = state.amend_timeline(
        [
            _stint("Alice", T0 - 10 * MIN, T0 + 120 * MIN),
            _stint("Bob", T0 + 124 * MIN, T0 + 240 * MIN),
            _stint("Alice", T0 + 250 * MIN, None),
        ],
        now_ms=T0 + 300 * MIN,
    )

    assert payload["session_id"] == session_id
    assert payload["status"] == "active"
    assert payload["driver"] == "Alice"
    assert payload["stint_number"] == 3
    assert payload["session_start"] == T0 - 10 * MIN
    assert payload["stint_start"] == T0 + 250 * MIN
    assert payload["timestamp"] == T0 + 300 * MIN
    assert [(s["stint_number"], s["driver"], s["start_ms"], s["end_ms"]) for s in state.stints] == [
        (1, "Alice", T0 - 10 * MIN, T0 + 120 * MIN),
        (2, "Bob", T0 + 124 * MIN, T0 + 240 * MIN),
    ]
    restored = SessionState.from_dict(state.to_dict())
    assert restored.stints == state.stints
    assert restored.stint_number == 3


def test_amend_timeline_rewrites_an_ended_session_and_can_remove_a_stint():
    state = started()
    state.change_driver("Bob", now_ms=T0 + 30 * MIN)
    state.change_driver("Cara", now_ms=T0 + 60 * MIN)
    state.end_session(now_ms=T0 + 3 * 24 * 60 * MIN)  # left open for days

    payload = state.amend_timeline(
        [_stint("Alice", T0 + 5 * MIN, T0 + 40 * MIN), _stint("Dee", T0 + 44 * MIN, T0 + 300 * MIN)]
    )

    assert payload["status"] == "ended"
    assert payload["driver"] == "Dee"
    assert payload["stint_number"] == 2
    assert payload["session_start"] == T0 + 5 * MIN
    assert payload["stint_start"] == T0 + 44 * MIN
    assert [(s["driver"], s["start_ms"], s["end_ms"]) for s in state.stints] == [
        ("Alice", T0 + 5 * MIN, T0 + 40 * MIN),
        ("Dee", T0 + 44 * MIN, T0 + 300 * MIN),
    ]
    assert SessionState.from_dict(state.to_dict()).stints == state.stints


def test_amend_timeline_validation():
    with pytest.raises(SessionError, match="no session to amend"):
        SessionState().amend_timeline([_stint("Alice", T0, None)])

    state = started()
    state.change_driver("Bob", now_ms=T0 + 30 * MIN)
    before = state.to_dict()
    for stints, message in [
        ([], "at least one stint"),
        ([_stint(" ", T0, None)], "needs a driver"),
        ([_stint("Alice", None, None)], "needs a start time"),  # type: ignore[arg-type]
        ([_stint("Alice", T0, T0 + MIN), _stint("Bob", T0, None)], "before stint 1 ended"),
        ([_stint("Alice", T0, None), _stint("Bob", T0 + MIN, None)], "needs an end time"),
        ([_stint("Alice", T0 + MIN, T0), _stint("Bob", T0 + MIN, None)], "before it started"),
        ([_stint("Alice", T0, T0 + MIN)], "still active"),
    ]:
        with pytest.raises(SessionError, match=message):
            state.amend_timeline(stints)
    after = state.to_dict()
    assert {**after, "timestamp": 0} == {**before, "timestamp": 0}

    state.end_session(now_ms=T0 + 60 * MIN)
    with pytest.raises(SessionError, match="needs an end time"):
        state.amend_timeline([_stint("Alice", T0, None)])


def test_reopen_undoes_an_end():
    state = started()
    state.change_driver("Bob", now_ms=T0 + 30 * MIN)
    before = {**state.to_dict(), "timestamp": 0}
    with pytest.raises(SessionError, match="only an ended session"):
        state.reopen()

    state.end_session(now_ms=T0 + 60 * MIN)
    payload = state.reopen(now_ms=T0 + 61 * MIN)

    assert payload["status"] == "active"
    assert payload["driver"] == "Bob"
    assert payload["stint_number"] == 2
    assert payload["stint_start"] == T0 + 30 * MIN
    assert {**state.to_dict(), "timestamp": 0} == before
    # And the session carries on as if nothing had happened.
    state.change_driver("Alice", now_ms=T0 + 90 * MIN)
    assert state.stint_number == 3


def test_red_flag_periods():
    state = started()
    with pytest.raises(SessionError, match="not under a red flag"):
        state.green_flag(now_ms=T0 + MIN)

    state.red_flag(now_ms=T0 + 10 * MIN)
    with pytest.raises(SessionError, match="already under a red flag"):
        state.red_flag(now_ms=T0 + 11 * MIN)
    with pytest.raises(SessionError, match="before the red flag came out"):
        state.green_flag(now_ms=T0 + 9 * MIN)
    state.green_flag(now_ms=T0 + 25 * MIN)
    with pytest.raises(SessionError, match="cannot start before"):
        state.red_flag(now_ms=T0 + 20 * MIN)
    state.red_flag(now_ms=T0 + 40 * MIN)

    assert state.reds == [
        {"start_ms": T0 + 10 * MIN, "end_ms": T0 + 25 * MIN},
        {"start_ms": T0 + 40 * MIN, "end_ms": None},
    ]
    assert SessionState.from_dict(state.to_dict()).reds == state.reds
    # The wire payload the car receives is unchanged by any of this.
    assert "reds" not in state.payload()

    # A race that ends under a red flag closes the period with it.
    state.end_session(now_ms=T0 + 50 * MIN)
    assert state.reds[-1]["end_ms"] == T0 + 50 * MIN

    state.amend_reds([{"start_ms": T0 + 12 * MIN, "end_ms": T0 + 20 * MIN}])
    assert state.reds == [{"start_ms": T0 + 12 * MIN, "end_ms": T0 + 20 * MIN}]
    with pytest.raises(SessionError, match="needs an end time"):
        state.amend_reds([{"start_ms": T0, "end_ms": None}])
    with pytest.raises(SessionError, match="before it started"):
        state.amend_reds([{"start_ms": T0 + MIN, "end_ms": T0}])


def test_fuel_marks_are_kept_in_time_order():
    state = started()
    state.mark_fuel(132.0, now_ms=T0 + 120 * MIN)
    state.mark_fuel(90, now_ms=T0 + 30 * MIN)

    assert state.fuel_marks == [
        {"at_ms": T0 + 30 * MIN, "level_l": 90.0},
        {"at_ms": T0 + 120 * MIN, "level_l": 132.0},
    ]
    assert SessionState.from_dict(state.to_dict()).fuel_marks == state.fuel_marks
    with pytest.raises(SessionError, match="level in litres"):
        state.mark_fuel(-1, now_ms=T0)
    with pytest.raises(SessionError, match="level in litres"):
        state.amend_fuel_marks([{"at_ms": T0, "level_l": "full"}])
    state.amend_fuel_marks([])
    assert state.fuel_marks == []


def test_a_bad_annotation_never_costs_the_session_on_restore():
    state = started()
    snapshot = state.to_dict()
    snapshot["reds"] = [{"start_ms": "noon"}]
    snapshot["fuel_marks"] = "lots"

    restored = SessionState.from_dict(snapshot)

    assert restored.session_id == state.session_id
    assert restored.reds == []
    assert restored.fuel_marks == []

    # A snapshot written before the annotations existed restores too.
    old = state.to_dict()
    del old["reds"], old["fuel_marks"]
    assert SessionState.from_dict(old).status == "active"
