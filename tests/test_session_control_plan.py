"""The race plan (P7.8): validation, the HTTP surface, and the database."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from pit.db.migrate import apply_migrations
from pit.session_control.database import PlanUnavailable, SessionDatabase
from pit.session_control.plan import PlanError, RacePlan, validate_plan
from pit.session_control.service import SessionController, StateFile, serve_http

VEHICLE = "example-club-racer"
T0 = 1_789_500_000_000  # epoch ms
GOOD = {
    "race_end_at_ms": T0 + 6 * 3600 * 1000,
    "race_end_laps": None,
    "end_authority": "time",
    "tank_l": 60.0,
    "usable_fuel_l": 55.0,
    "refuel_min_s": 480,
    "service_typical_s": 120,
    "driver_limits": {"max_continuous_min": 120, "max_total_min": 360, "min_rest_min": 60},
    "planned_stops": [
        {"at_lap": 45, "type": "refuel", "driver_in": "Driver B"},
        {"at_ms": T0 + 3 * 3600 * 1000, "type": "service"},
    ],
    "car_number": "7",
    "updated_by": "Rob",
}


# --- validation ------------------------------------------------------------------


def test_a_complete_plan_validates_and_round_trips_its_shape():
    plan = validate_plan(GOOD)
    assert isinstance(plan, RacePlan)
    assert plan.end_authority == "time" and plan.race_end_laps is None
    assert plan.driver_limits == {
        "max_continuous_min": 120,
        "max_total_min": 360,
        "min_rest_min": 60,
    }
    assert plan.to_dict()["planned_stops"] == [
        {"type": "refuel", "at_lap": 45, "driver_in": "Driver B"},
        {"type": "service", "at_ms": T0 + 3 * 3600 * 1000},
    ]


def test_the_authority_defaults_to_whichever_end_condition_is_given():
    laps_only = validate_plan(
        {**GOOD, "race_end_at_ms": None, "race_end_laps": 200, "end_authority": None}
    )
    assert laps_only.end_authority == "laps"
    assert validate_plan({**GOOD, "end_authority": ""}).end_authority == "time"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"race_end_at_ms": None, "race_end_laps": None}, "needs an end"),
        ({"end_authority": "laps"}, "race_end_laps is missing"),
        ({"usable_fuel_l": 61}, "cannot exceed tank"),
        ({"tank_l": 0}, "tank_l must be positive"),
        ({"refuel_min_s": -1}, "cannot be negative"),
        ({"driver_limits": {"max_lunch_min": 30}}, "not a known limit"),
        ({"planned_stops": [{"type": "refuel"}]}, "exactly one of at_lap / at_ms"),
        ({"planned_stops": [{"type": "nap", "at_lap": 3}]}, "refuel or service"),
        ({"planned_stops": [{"type": "refuel", "at_lap": 0}]}, "must be positive"),
        ({"tank_l": "sixty"}, "must be a number"),
        ({"race_end_laps": 12.5}, "whole number"),
    ],
)
def test_plans_the_team_could_not_race_to_are_refused(override: dict, message: str):
    with pytest.raises(PlanError, match=message):
        validate_plan({**GOOD, **override})


# --- HTTP ---------------------------------------------------------------------------


class _FakeDatabase:
    connected = True
    pending_count = 0
    errors = 0

    def __init__(self, *, unavailable: bool = False) -> None:
        self.saved: dict[str, list[dict]] = {}
        self.unavailable = unavailable

    async def record(self, state: dict[str, object]) -> bool:
        return True

    async def save_plan(self, session_id: str, plan: RacePlan) -> dict[str, object]:
        if self.unavailable:
            raise PlanUnavailable("database unavailable; the plan was not saved")
        revisions = self.saved.setdefault(session_id, [])
        row = {
            **plan.to_dict(),
            "session_id": session_id,
            "revision": len(revisions) + 1,
            "updated_at_ms": T0,
        }
        revisions.append(row)
        return row

    async def load_plan(self, session_id: str) -> dict[str, object] | None:
        if self.unavailable:
            raise PlanUnavailable("database unavailable; the plan cannot be read")
        revisions = self.saved.get(session_id)
        return revisions[-1] if revisions else None


class _Publisher:
    connected = True
    pending_count = 0
    published = 0
    errors = 0

    def submit(self, payload: dict[str, object]) -> None:
        self.published += 1


def _request(url: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _serve(tmp_path: Path, database: _FakeDatabase):
    async def exercise(fn):
        controller = SessionController(StateFile(tmp_path / "session.json"), database, _Publisher())
        roster = tmp_path / "roster.json"
        roster.write_text(json.dumps({"drivers": ["Driver A"], "session_types": ["race"]}))
        server = serve_http(
            asyncio.get_running_loop(),
            controller,
            roster,
            database,
            _Publisher(),
            port=0,
            host="127.0.0.1",
        )
        try:
            await asyncio.to_thread(fn, f"http://127.0.0.1:{server.server_port}")
        finally:
            server.shutdown()
            server.server_close()

    return exercise


def test_a_plan_needs_a_session_then_saves_as_numbered_revisions(tmp_path: Path):
    database = _FakeDatabase()

    def steps(base: str) -> None:
        code, body = _request(f"{base}/session/plan")
        assert code == 200 and body == {"session_id": None, "plan": None}
        code, body = _request(f"{base}/session/plan", GOOD)
        assert code == 409 and "start one" in body["error"]

        code, _ = _request(f"{base}/session/start", {"session_type": "race", "driver": "Driver A"})
        assert code == 200
        code, body = _request(f"{base}/session/plan", GOOD)
        assert code == 200 and body["plan"]["revision"] == 1
        code, body = _request(f"{base}/session/plan", {**GOOD, "usable_fuel_l": 50})
        assert code == 200 and body["plan"]["revision"] == 2
        code, body = _request(f"{base}/session/plan")
        assert code == 200 and body["plan"]["usable_fuel_l"] == 50

        code, body = _request(f"{base}/session/plan", {**GOOD, "usable_fuel_l": 999})
        assert code == 400 and "cannot exceed tank" in body["error"]
        code, body = _request(f"{base}/session/plan", {**GOOD, "race_end_at_ms": None})
        assert code == 400 and "needs an end" in body["error"]

    asyncio.run(_serve(tmp_path, database)(steps))


def test_an_unreachable_database_is_a_503_not_a_promise(tmp_path: Path):
    database = _FakeDatabase(unavailable=True)

    def steps(base: str) -> None:
        _request(f"{base}/session/start", {"session_type": "race", "driver": "Driver A"})
        code, body = _request(f"{base}/session/plan", GOOD)
        assert code == 503 and "not saved" in body["error"]
        code, body = _request(f"{base}/session/plan")
        assert code == 503

    asyncio.run(_serve(tmp_path, database)(steps))


# --- the database ---------------------------------------------------------------


def test_migration_009_stores_revisions_and_the_view_shows_the_latest(timescale_dsn, monkeypatch):
    monkeypatch.setenv("GRAFANA_DB_USER", "grafana_ro")
    monkeypatch.setenv("GRAFANA_DB_PASSWORD", "ro-secret")
    with psycopg.connect(timescale_dsn, autocommit=True) as conn:
        applied = apply_migrations(conn)
        assert "009_race_plans.sql" in applied
        conn.execute(
            "INSERT INTO sessions (session_id, vehicle_id, session_type, started, status) "
            "VALUES ('s1', %s, 'race', now(), 'active')",
            (VEHICLE,),
        )

    async def exercise() -> None:
        database = SessionDatabase(timescale_dsn, VEHICLE)
        assert await database.connect_once()
        assert await database.load_plan("s1") is None
        first = await database.save_plan("s1", validate_plan(GOOD))
        second = await database.save_plan("s1", validate_plan({**GOOD, "usable_fuel_l": 50}))
        assert (first["revision"], second["revision"]) == (1, 2)
        latest = await database.load_plan("s1")
        assert latest is not None
        assert latest["revision"] == 2 and latest["usable_fuel_l"] == 50
        assert latest["race_end_at_ms"] == GOOD["race_end_at_ms"]
        assert latest["planned_stops"][0] == {
            "type": "refuel",
            "at_lap": 45,
            "driver_in": "Driver B",
        }
        assert latest["driver_limits"]["max_total_min"] == 360
        with pytest.raises(PlanUnavailable):
            await database.save_plan("no-such-session", validate_plan(GOOD))
        await database.close()

    asyncio.run(exercise())

    with psycopg.connect(timescale_dsn) as conn:
        assert conn.execute("SELECT count(*) FROM v_race_plan_history").fetchone()[0] == 2
        assert conn.execute("SELECT revision FROM v_race_plan").fetchone()[0] == 2
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO race_plans (session_id, revision, race_end_laps, end_authority, "
                "tank_l, usable_fuel_l, refuel_min_s, service_typical_s) "
                "VALUES ('s1', 9, 100, 'laps', 60, 70, 0, 0)"
            )

    parts = dict(psycopg.conninfo.conninfo_to_dict(timescale_dsn))
    parts.update(user="grafana_ro", password="ro-secret")
    with psycopg.connect(make_conninfo(**parts)) as conn:
        assert conn.execute("SELECT count(*) FROM v_race_plan").fetchone()[0] == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM race_plans")
