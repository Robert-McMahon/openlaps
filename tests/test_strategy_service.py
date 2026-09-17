"""P7.9: the strategy service against a real schema, end to end.

Seeds a session, a race plan, a stint, laps with counter samples and a
refuel stop into a throwaway TimescaleDB, runs the service's step through
the real reader and writer, and reads back through the views a dashboard
would use. The driver-time finding is checked all the way to the rendered
Grafana rule: the `strategy-warning` rule's own SQL, run as the Grafana
role, returns a firing value with the finding open and a resting value once
it clears. Both, or it is decoration.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import LiteralString, cast

import psycopg
import pytest
import yaml
from psycopg.conninfo import make_conninfo

from pit.db.migrate import apply_migrations
from pit.strategy.database import StrategyDatabase
from pit.strategy.health import HealthState, serve_health
from pit.strategy.service import StrategyService, StrategySettings

REPO = Path(__file__).resolve().parents[1]
ALERTING = REPO / "deploy/pit-config/grafana/provisioning/alerting/endurance.yaml"
VEHICLE = "example-club-racer"
T0 = datetime(2026, 9, 16, 4, 0, tzinfo=UTC)


def _rule_sql(uid: str) -> str:
    document = yaml.safe_load(ALERTING.read_text(encoding="utf-8"))
    for group in document["groups"]:
        for rule in group["rules"]:
            if rule["uid"] == uid:
                return rule["data"][0]["model"]["rawSql"]
    raise KeyError(uid)


def _seed(conn: psycopg.Connection, *, max_continuous_min: int) -> None:
    """A race with one stint, twelve laps, a refuel stop and a plan."""
    driver_a = conn.execute(
        "INSERT INTO drivers (name) VALUES ('Driver A') RETURNING driver_id"
    ).fetchone()[0]
    driver_b = conn.execute(
        "INSERT INTO drivers (name) VALUES ('Driver B') RETURNING driver_id"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO sessions (session_id, vehicle_id, session_type, track_name, car, started, "
        "status) VALUES ('s-1', %s, 'race', 'Wanneroo', 'club-racer', %s, 'active')",
        (VEHICLE, T0),
    )
    # Stint 1 ends inside the refuel stop; stint 2 begins in it, which is
    # what makes its first level reading the key-on re-base.
    stint_1 = conn.execute(
        "INSERT INTO stints (session_id, stint_number, driver_id, started, ended) "
        "VALUES ('s-1', 1, %s, %s, %s) RETURNING stint_id",
        (driver_a, T0, T0 + timedelta(seconds=1000)),
    ).fetchone()[0]
    stint_2 = conn.execute(
        "INSERT INTO stints (session_id, stint_number, driver_id, started) "
        "VALUES ('s-1', 2, %s, %s) RETURNING stint_id",
        (driver_b, T0 + timedelta(seconds=1000)),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO race_plans (session_id, revision, race_end_laps, end_authority, tank_l, "
        "usable_fuel_l, refuel_min_s, service_typical_s, driver_limits, planned_stops) "
        "VALUES ('s-1', 1, 200, 'laps', 60, 55, 480, 120, %s, %s)",
        (
            json.dumps({"max_continuous_min": max_continuous_min}),
            json.dumps([{"type": "refuel", "at_lap": 40, "driver_in": "Driver A"}]),
        ),
    )

    keys: dict[str, int] = {}
    for name, value_type in (
        ("car.fuel_total_used", 1),
        ("car.fuel_level", 1),
        ("car.battery_v", 1),
        ("lap.event", 4),
    ):
        keys[name] = conn.execute(
            "INSERT INTO channels (vehicle_id, name, units, value_type) "
            "VALUES (%s, %s, '', %s) RETURNING channel_key",
            (VEHICLE, name, value_type),
        ).fetchone()[0]

    # Laps 1-9 on track at 100 s and 1,000 cc each (a monotonic counter),
    # lap 10 the in-lap crossed in the lane, lap 11 the out-lap with the
    # counter restarted from zero, lap 12 clean again.
    samples: list[tuple[datetime, int, float]] = []
    for lap in range(1, 10):
        crossed = T0 + timedelta(seconds=100 * lap)
        base = 1000.0 * (lap - 1)
        samples += [
            (crossed - timedelta(seconds=99), keys["car.fuel_total_used"], base),
            (crossed - timedelta(seconds=50), keys["car.fuel_total_used"], base + 500.0),
            (crossed, keys["car.fuel_total_used"], base + 1000.0),
        ]
    # The in-lap: still counting up to the entry, and once more crossing the
    # line in the lane with the ECU still on, which puts a pre-reset sample
    # inside the out-lap's window.
    samples += [
        (T0 + timedelta(seconds=901), keys["car.fuel_total_used"], 9000.0),
        (T0 + timedelta(seconds=940), keys["car.fuel_total_used"], 9400.0),
        (T0 + timedelta(seconds=1000), keys["car.fuel_total_used"], 9400.0),
    ]
    # Key-on after the stop: the counter restarts, so the out-lap resets.
    samples += [
        (T0 + timedelta(seconds=1430), keys["car.fuel_total_used"], 0.0),
        (T0 + timedelta(seconds=1500), keys["car.fuel_total_used"], 300.0),
        (T0 + timedelta(seconds=1550), keys["car.fuel_total_used"], 800.0),
        (T0 + timedelta(seconds=1600), keys["car.fuel_total_used"], 1300.0),
    ]
    # Level: 58 L falling through stint 1 to 49 L at the entry, 59.5 L at
    # key-on (a full fill) and dithering after; battery healthy throughout.
    for second in range(0, 950, 10):
        samples.append(
            (T0 + timedelta(seconds=second), keys["car.fuel_level"], 58.0 - 9.0 * second / 940)
        )
    for second in range(1430, 1610, 10):
        samples.append((T0 + timedelta(seconds=second), keys["car.fuel_level"], 59.5))
    for second in range(0, 1610, 10):
        samples.append((T0 + timedelta(seconds=second), keys["car.battery_v"], 13.8))
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO samples (time, channel_key, value) VALUES (%s, %s, %s)", samples
        )
        cur.executemany(
            "INSERT INTO samples (time, channel_key, value_text) VALUES (%s, %s, %s)",
            [
                (
                    T0 + timedelta(seconds=950),
                    keys["lap.event"],
                    json.dumps(
                        {"type": "pit_entry", "line": "PitEntryRefuel", "pit_status": "pit"}
                    ),
                ),
                (
                    T0 + timedelta(seconds=1440),
                    keys["lap.event"],
                    json.dumps(
                        {"type": "pit_exit", "line": "PitExitRefuel", "pit_status": "track"}
                    ),
                ),
            ],
        )
        laps = [(lap, 100 * lap, stint_1, "track", 100.0) for lap in range(1, 10)]
        laps += [(10, 1000, stint_1, "pit", 100.0), (11, 1500, stint_2, "track", 500.0)]
        laps += [(12, 1600, stint_2, "track", 100.0)]
        cur.executemany(
            "INSERT INTO laps (vehicle_id, session_id, stint_id, track_name, lap_number, "
            "crossed_at, lap_time_s, valid, pit_status, direction) "
            "VALUES (%s, 's-1', %s, 'Wanneroo', %s, %s, %s, true, %s, 'forward')",
            [
                (VEHICLE, stint, number, T0 + timedelta(seconds=at), lap_time, status)
                for number, at, stint, status, lap_time in laps
            ],
        )
    conn.commit()


def _settings(dsn: str, **overrides) -> StrategySettings:
    return StrategySettings(dsn=dsn, vehicle_id=VEHICLE, mqtt_host="127.0.0.1", **overrides)


def _ticking(start: datetime):
    """A clock that reads ``start`` first and advances a second per call.

    Evaluations are seconds apart in life; a frozen clock would give two
    rows the same `time` and make the latest-per-vehicle view a coin toss.
    """
    calls = iter(range(10_000))

    def clock() -> datetime:
        return start + timedelta(seconds=next(calls))

    return clock


@pytest.fixture
def seeded(timescale_dsn):
    with psycopg.connect(timescale_dsn) as conn:
        apply_migrations(conn)
        _seed(conn, max_continuous_min=20)
    return timescale_dsn


def test_one_step_writes_the_state_its_findings_and_the_views_read_back(seeded):
    now = T0 + timedelta(seconds=1650)
    database = StrategyDatabase(seeded, VEHICLE)
    service = StrategyService(_settings(seeded), database=database, clock=_ticking(now))
    state = service.step()
    assert state is not None and state.trigger == "start"

    # Fuel: the re-base is stint 2's first accepted level (59.5 L at key-on),
    # confidence key_on, fuel added 59.5 - 49.0; the burn is 1.0 L/lap over
    # the clean laps; the out-lap's reset is substituted for its 60 driven
    # seconds after the exit and lap 12 measured at 1.0 L.
    assert state.rebase_confidence == "key_on"
    assert state.rebase_level_l == pytest.approx(59.5)
    assert state.fuel_added_l == pytest.approx(59.5 - 49.0, abs=0.05)
    assert state.burn_l_per_lap == pytest.approx(1.0)
    assert state.burn_laps == 5
    assert state.fuel_remaining_l == pytest.approx(59.5 - 1.0 - 0.6, abs=0.01)
    # Usable fuel 57.9 - 5 at its low end (0.3 L key-on uncertainty plus a
    # quarter-lap for the substituted lap) over a burn of exactly 1.0 L/lap.
    usable_lo = 57.9 - 0.55 - 5.0
    assert state.laps_to_dry_lo == pytest.approx(usable_lo, abs=0.02)
    assert state.laps_to_dry_hi == pytest.approx(57.9 + 0.55 - 5.0, abs=0.02)
    assert state.laps_remaining == 200 - 12
    assert state.stops_needed == 3  # ceil((188 - 52.35) / 55)
    assert state.window_close_lap == 12 + 52
    assert state.window_open_lap == max(12, 200 - 3 * 55)
    assert state.driver == "Driver B"
    # Driver B started 650 s ago against a 20-minute continuous limit: within
    # the 10-minute margin, so the finding is open; the first computed stop
    # is therefore driver-limited five laps out, refuelling while stopped,
    # and 23 laps ahead of the operator's lap-40 plan, which is drift.
    assert state.driver_time_remaining_s == pytest.approx(1200 - 650)
    assert state.stop_plan[0]["lap"] == 12 + 5
    assert state.stop_plan[0]["reason"] == "driver" and state.stop_plan[0]["type"] == "refuel"
    assert state.stop_plan[0]["driver_in"] == "Driver A"
    assert state.stop_plan[0]["delta_laps"] == 17 - 40
    assert state.plan_drift["diverged"] is True
    assert sorted(f.monitor for f in state.findings) == [
        "strategy.driver_time",
        "strategy.plan_drift",
    ]
    assert database.open_findings.keys() == {"strategy.driver_time", "strategy.plan_drift"}
    assert service.health.snapshot()["findings_open"] == 2

    with psycopg.connect(seeded) as conn:
        latest = conn.execute(
            "SELECT session_id, trigger, lap_number, rebase_confidence, driver, "
            "jsonb_array_length(stop_plan) > 0, plan_drift ->> 'diverged' "
            "FROM v_strategy_latest WHERE vehicle_id = %s",
            (VEHICLE,),
        ).fetchone()
        assert latest == ("s-1", "start", 12, "key_on", "Driver B", True, "true")
        assert conn.execute("SELECT count(*) FROM v_strategy_history").fetchone()[0] == 1
        finding = conn.execute(
            "SELECT monitor, severity, closed_at, summary ->> 'driver' FROM v_watch_findings "
            "WHERE monitor = 'strategy.driver_time'"
        ).fetchone()
        assert finding == ("strategy.driver_time", "warning", None, "Driver B")


def test_the_driver_time_finding_fires_the_rendered_rule_and_clears_when_it_should(seeded):
    now = T0 + timedelta(seconds=1650)
    database = StrategyDatabase(seeded, VEHICLE)
    service = StrategyService(_settings(seeded), database=database, clock=_ticking(now))
    assert service.step() is not None

    grafana = make_conninfo(seeded, user="grafana_ro", password="openlaps-grafana-test")
    with psycopg.connect(grafana) as reader:
        firing = reader.execute(cast(LiteralString, _rule_sql("strategy-warning"))).fetchone()
        assert firing[1] == 2.0, "the open strategy warnings must fire the warning rule"
        quiet = reader.execute(cast(LiteralString, _rule_sql("strategy-critical"))).fetchone()
        assert quiet[1] == 0.0, "a warning must not fire the critical rule"

    # A new plan revision with a generous limit is a trigger; the finding
    # closes in the same transaction as the next state row.
    with psycopg.connect(seeded) as conn:
        conn.execute(
            "INSERT INTO race_plans (session_id, revision, race_end_laps, end_authority, tank_l, "
            "usable_fuel_l, refuel_min_s, service_typical_s, driver_limits) "
            "VALUES ('s-1', 2, 60, 'laps', 60, 55, 480, 120, %s)",
            (json.dumps({"max_continuous_min": 120}),),
        )
        conn.commit()
    state = service.step()
    assert state is not None and state.trigger == "plan" and state.findings == ()
    assert database.open_findings == {}
    with psycopg.connect(grafana) as reader:
        resting = reader.execute(cast(LiteralString, _rule_sql("strategy-warning"))).fetchone()
        assert resting[1] == 0.0
        closed = reader.execute(
            "SELECT count(*) FROM v_watch_findings WHERE closed_at IS NOT NULL"
        ).fetchone()[0]
        assert closed == 2
    assert service.step() is None, "nothing changed: no evaluation, no row"


def test_ending_the_session_writes_one_idle_row_and_closes_every_finding(seeded):
    now = T0 + timedelta(seconds=1650)
    database = StrategyDatabase(seeded, VEHICLE)
    service = StrategyService(_settings(seeded), database=database, clock=_ticking(now))
    assert service.step() is not None
    with psycopg.connect(seeded) as conn:
        conn.execute("UPDATE sessions SET status = 'ended', ended = now() WHERE session_id = 's-1'")
        conn.commit()
    idle = service.step()
    assert idle is not None and idle.trigger == "idle" and idle.session_id is None
    assert service.step() is None
    with psycopg.connect(seeded) as conn:
        assert conn.execute("SELECT session_id FROM v_strategy_latest").fetchone() == (None,)
        still_open = conn.execute(
            "SELECT count(*) FROM v_watch_findings WHERE closed_at IS NULL"
        ).fetchone()[0]
        assert still_open == 0


def test_a_restart_adopts_the_open_findings_instead_of_opening_a_second_row(seeded):
    now = T0 + timedelta(seconds=1650)
    first = StrategyService(
        _settings(seeded), database=StrategyDatabase(seeded, VEHICLE), clock=_ticking(now)
    )
    assert first.step() is not None
    second_db = StrategyDatabase(seeded, VEHICLE)
    second = StrategyService(_settings(seeded), database=second_db, clock=_ticking(now))
    assert second.step() is not None
    assert second_db.open_findings.keys() == {"strategy.driver_time", "strategy.plan_drift"}
    with psycopg.connect(seeded) as conn:
        assert conn.execute("SELECT count(*) FROM watch_findings").fetchone()[0] == 2


def test_settings_come_from_the_environment_and_refuse_nonsense():
    env = {
        "TIMESCALE_DSN": "postgresql://x/y",
        "OPENLAPS_VEHICLE_ID": "car-7",
        "OPENLAPS_MQTT_HOST": "mosquitto",
        "OPENLAPS_STRATEGY_BURN_WINDOW_LAPS": "7",
        "OPENLAPS_STRATEGY_DRIVER_MARGIN_S": "900",
        "OPENLAPS_STRATEGY_HEALTH_PORT": "8088",
    }
    settings = StrategySettings.from_env(env)
    assert settings.vehicle_id == "car-7" and settings.mqtt_host == "mosquitto"
    assert settings.policy.burn_window_laps == 7 and settings.policy.driver_margin_s == 900.0
    assert settings.health_port == 8088
    with pytest.raises(ValueError, match="OPENLAPS_VEHICLE_ID"):
        StrategySettings.from_env({"TIMESCALE_DSN": "postgresql://x/y"})
    with pytest.raises(ValueError, match="must be positive"):
        StrategySettings.from_env({**env, "OPENLAPS_STRATEGY_POLL_S": "0"})


def test_health_endpoint_reports_the_last_evaluation_and_open_findings():
    state = HealthState()
    state.observe_evaluation("s-1", "lap", 2)
    server = serve_health(state, 0, host="127.0.0.1")
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/health", timeout=2
        ) as response:
            payload = json.loads(response.read())
    finally:
        server.shutdown()
        server.server_close()
    assert payload["evaluations"] == 1 and payload["findings_open"] == 2
    assert payload["session_id"] == "s-1" and payload["last_trigger"] == "lap"
    assert payload["last_evaluation_age_s"] is not None
    assert threading.active_count() >= 1


def test_entrypoint_compose_env_and_health_probe_wire_the_separate_service():
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    compose = (REPO / "deploy" / "pit-compose.yaml").read_text(encoding="utf-8")
    env = (REPO / "example.env").read_text(encoding="utf-8")
    operations = (REPO / "docs" / "operations" / "verification.md").read_text(encoding="utf-8")

    assert 'openlaps-strategy = "pit.strategy.__main__:main"' in pyproject
    assert "\n  strategy:\n" in compose
    assert 'command: ["openlaps-strategy"]' in compose
    assert '"8088:8088"' in compose
    assert "OPENLAPS_STRATEGY_HEALTH_PORT=8088" in env
    assert "OPENLAPS_STRATEGY_BURN_WINDOW_LAPS" in env
    assert "| 8088 | strategy |" in operations
