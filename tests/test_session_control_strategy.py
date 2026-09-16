"""The session UI's strategy card (P7.8 deferred it to P7.9): read-only, one route.

GET /session/strategy answers with the strategy service's latest evaluation
for the current session, straight from `v_strategy_latest`, so the operator
sees the radio numbers next to the plan they entered without opening a
dashboard. The UI never computes anything and never writes.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from test_session_control_plan import VEHICLE, _FakeDatabase, _request, _serve

from pit.db.migrate import apply_migrations
from pit.session_control.database import PlanUnavailable, SessionDatabase

STATIC = Path(__file__).resolve().parents[1] / "src/pit/session_control/static"
T0 = datetime(2026, 9, 16, 4, 0, tzinfo=UTC)
ROW = {
    "time_ms": 1_789_500_000_000,
    "session_id": "s-1",
    "trigger": "lap",
    "lap_number": 12,
    "laps_to_dry_lo": 41.2,
    "laps_to_dry_hi": 47.9,
    "window_open_lap": 35,
    "window_close_lap": 53,
    "driver": "Driver B",
    "driver_time_remaining_s": 540.0,
    "stop_plan": [{"lap": 53, "type": "refuel", "driver_in": "Driver A", "delta_laps": 13}],
}


class _StrategyDatabase(_FakeDatabase):
    def __init__(self, *, row: dict | None = None, unavailable: bool = False) -> None:
        super().__init__(unavailable=unavailable)
        self.row = row

    async def load_strategy(self, session_id: str) -> dict[str, object] | None:
        if self.unavailable:
            raise PlanUnavailable("database unavailable; strategy cannot be read")
        return dict(self.row, session_id=session_id) if self.row else None


def test_the_strategy_route_needs_a_session_and_reads_the_latest_row(tmp_path: Path):
    database = _StrategyDatabase(row=ROW)

    def steps(base: str) -> None:
        code, body = _request(f"{base}/session/strategy")
        assert code == 200 and body == {"session_id": None, "strategy": None}
        code, _ = _request(f"{base}/session/start", {"session_type": "race", "driver": "Driver A"})
        assert code == 200
        code, body = _request(f"{base}/session/strategy")
        assert code == 200
        assert body["strategy"]["laps_to_dry_lo"] == 41.2
        assert body["strategy"]["stop_plan"][0]["driver_in"] == "Driver A"
        code, body = _request(f"{base}/session/strategy", {"nope": 1})
        assert code == 409 and "unknown" in body["error"].lower(), "the card is read-only"

    asyncio.run(_serve(tmp_path, database)(steps))


def test_no_evaluation_yet_is_a_null_not_an_error_and_an_outage_is_a_503(tmp_path: Path):
    def steps_empty(base: str) -> None:
        _request(f"{base}/session/start", {"session_type": "race", "driver": "Driver A"})
        code, body = _request(f"{base}/session/strategy")
        assert code == 200 and body["strategy"] is None

    asyncio.run(_serve(tmp_path, _StrategyDatabase())(steps_empty))

    def steps_down(base: str) -> None:
        _request(f"{base}/session/start", {"session_type": "race", "driver": "Driver A"})
        code, body = _request(f"{base}/session/strategy")
        assert code == 503 and "strategy" in body["error"]

    (tmp_path / "down").mkdir()
    asyncio.run(_serve(tmp_path / "down", _StrategyDatabase(unavailable=True))(steps_down))


def test_the_page_carries_a_read_only_strategy_card_fed_by_the_route():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'id="strategy-section"' in html and 'id="strategy-grid"' in html
    assert "<form" not in html.split('id="strategy-section"')[1].split("</section>")[0]
    assert '"/session/strategy"' in script
    assert "Laps to dry (lower bound)" in script
    assert "refreshStrategy" in script and "setInterval(refreshStrategy" in script


def test_load_strategy_reads_v_strategy_latest_for_the_session(timescale_dsn, monkeypatch):
    monkeypatch.setenv("GRAFANA_DB_USER", "grafana_ro")
    monkeypatch.setenv("GRAFANA_DB_PASSWORD", "ro-secret")
    with psycopg.connect(timescale_dsn, autocommit=True) as conn:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO sessions (session_id, vehicle_id, session_type, started, status) "
            "VALUES ('s1', %s, 'race', now(), 'active')",
            (VEHICLE,),
        )
        for offset, laps_lo in ((0, 30.0), (100, 28.5)):
            conn.execute(
                "INSERT INTO strategy_state (time, vehicle_id, session_id, trigger, lap_number, "
                "rebase_confidence, burn_laps, laps_to_dry_lo, laps_to_dry_hi, driver, "
                "refuel_release_at, stop_plan, plan_drift) "
                "VALUES (%s + %s * interval '1 second', %s, 's1', 'lap', %s, 'key_on', 5, %s, "
                "%s, 'Driver A', %s, %s, '{}')",
                (
                    T0,
                    offset,
                    VEHICLE,
                    offset // 100 + 1,
                    laps_lo,
                    laps_lo + 5,
                    T0,
                    json.dumps([{"lap": 40, "type": "refuel"}]),
                ),
            )

    async def exercise() -> None:
        database = SessionDatabase(timescale_dsn, VEHICLE)
        assert await database.connect_once()
        assert await database.load_strategy("other") is None
        latest = await database.load_strategy("s1")
        assert latest is not None
        assert latest["laps_to_dry_lo"] == 28.5 and latest["lap_number"] == 2
        assert latest["time_ms"] == int(T0.timestamp() * 1000) + 100_000
        assert latest["refuel_release_at_ms"] == int(T0.timestamp() * 1000)
        assert latest["stop_plan"] == [{"lap": 40, "type": "refuel"}]
        assert "time" not in latest and "refuel_release_at" not in latest
        await database.close()

    asyncio.run(exercise())
