"""P7.10: the timing feed against a real schema, end to end.

Replays the hand-built capture through the real reader and writer into a
throwaway TimescaleDB, then reads back through the views a dashboard or
the race forecast would use: the latest standings, the derived laps, the
passings, the flag intervals, and the gap to our car once a session and a
race plan name it. Our-car reconciliation is checked the whole way: a
vehicle whose lap table disagrees with the feed opens a ``field.lap_count``
finding, and a crossing near a main-line passing writes the clock offset.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
import yaml
from natsoft_docs import FIXTURE, OUR_CAR, T0, our_main_line_passings
from psycopg.conninfo import make_conninfo

from pit.db.migrate import apply_migrations
from pit.timing_feed.database import FieldDatabase
from pit.timing_feed.service import TimingFeedService, TimingFeedSettings
from pit.timing_feed.shapes import snapshot_from_json

VEHICLE = "example-club-racer"
REPO = Path(__file__).resolve().parents[1]
ALERTING = REPO / "deploy/pit-config/grafana/provisioning/alerting/endurance.yaml"


def _rule_sql(uid: str) -> str:
    document = yaml.safe_load(ALERTING.read_text(encoding="utf-8"))
    for group in document["groups"]:
        for rule in group["rules"]:
            if rule["uid"] == uid:
                return rule["data"][0]["model"]["rawSql"]
    raise KeyError(uid)


def _rule_value(dsn: str, uid: str) -> float:
    """What Grafana's read-only role sees when it evaluates the rule now."""
    grafana = make_conninfo(dsn, user="grafana_ro", password="openlaps-grafana-test")
    with psycopg.connect(grafana) as reader:
        return float(reader.execute(_rule_sql(uid)).fetchone()[1])


@pytest.fixture
def migrated(timescale_dsn):
    with psycopg.connect(timescale_dsn) as conn:
        apply_migrations(conn)
        conn.commit()
    return timescale_dsn


def _seed_session(conn: psycopg.Connection, *, crossings: list[datetime]) -> None:
    conn.execute(
        "INSERT INTO sessions (session_id, vehicle_id, session_type, track_name, car, started, "
        "status) VALUES ('s-1', %s, 'race', 'Wanneroo', 'club-racer', %s, 'active')",
        (VEHICLE, T0),
    )
    conn.execute(
        "INSERT INTO race_plans (session_id, revision, race_end_laps, end_authority, tank_l, "
        "usable_fuel_l, refuel_min_s, service_typical_s, car_number) "
        "VALUES ('s-1', 1, 200, 'laps', 60, 55, 480, 120, %s)",
        (OUR_CAR,),
    )
    for number, crossed_at in enumerate(crossings, start=1):
        conn.execute(
            "INSERT INTO laps (vehicle_id, session_id, track_name, lap_number, crossed_at, "
            "lap_time_s, valid) VALUES (%s, 's-1', 'Wanneroo', %s, %s, 95.0, true)",
            (VEHICLE, number, crossed_at),
        )


def _run_replay(dsn: str, *, reconcile: bool = True) -> TimingFeedService:
    settings = TimingFeedSettings(
        dsn=dsn,
        vehicle_id=VEHICLE,
        source_kind="replay",
        replay_file=str(FIXTURE),
        replay_paced=False,
        reconcile_s=0.2,
    )
    service = TimingFeedService(settings)

    async def scenario():
        stop = asyncio.Event()
        runner = asyncio.create_task(service.run(stop))
        await asyncio.wait_for(service.source_finished.wait(), timeout=30)
        if reconcile:
            await service.reconcile()
        stop.set()
        await runner

    asyncio.run(scenario())
    return service


def test_replay_drives_every_table_and_view(migrated):
    service = _run_replay(migrated)
    assert service.health.db_errors == 0 and service.health.batches_dropped == 0
    with psycopg.connect(migrated) as conn:
        standings = conn.execute(
            "SELECT car_number, position, laps, driver, class, pit_count, state "
            "FROM v_field_standings ORDER BY position"
        ).fetchall()
        assert [row[0] for row in standings] == ["27", "7", "99", "14"]
        assert standings[0] == ("27", 1, 4, "Driver A", "A", 0, "RUN")
        assert standings[3][5] == 1, "car 14 stopped once"

        laps = conn.execute(
            "SELECT car_number, lap_number, lap_time_s, flag_state, sub_status FROM v_field_laps "
            "ORDER BY time, car_number"
        ).fetchall()
        assert len(laps) == 13
        under_sc = [row for row in laps if row[3] == "yellow"]
        assert {row[4] for row in under_sc} == {"sc"} and len(under_sc) == 2

        passings = conn.execute(
            "SELECT line, count(*) FROM v_field_passings GROUP BY line ORDER BY line"
        ).fetchall()
        assert dict(passings) == {"main": 10, "pit_main": 1}
        (our_passings,) = conn.execute(
            "SELECT count(*) FROM v_field_passings WHERE car_number = %s AND tod IS NOT NULL",
            (OUR_CAR,),
        ).fetchone()
        assert our_passings == 4

        flags = conn.execute(
            "SELECT flag_state, sub_status, ended_at IS NULL, duration_s FROM v_field_flags "
            "ORDER BY started_at"
        ).fetchall()
        assert [(row[0], row[1]) for row in flags] == [
            ("none", None),
            ("green", None),
            ("yellow", "sc"),
            ("green", None),
            ("chequered", None),
            ("ended", None),
        ]
        assert flags[2][3] == pytest.approx(190.0)
        assert flags[-1][2] is True and all(row[2] is False for row in flags[:-1])

        # Nobody has named our car yet: no gaps, no finding, but no error either.
        assert conn.execute("SELECT count(*) FROM v_field_gaps").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM v_watch_findings").fetchone()[0] == 0
    assert service.health.our_car is None


def test_a_named_car_gets_gaps_a_lap_count_finding_and_a_clock_offset(migrated):
    # The vehicle recorded two laps, each stamped 0.3 s after the timekeepers
    # stamped our transponder on the main line; the feed will say four.
    feed_stamps = our_main_line_passings()
    assert len(feed_stamps) == 4
    vehicle_stamps = [stamp + timedelta(seconds=0.3) for stamp in feed_stamps]
    with psycopg.connect(migrated) as conn:
        _seed_session(conn, crossings=vehicle_stamps[:2])
        conn.commit()
    service = _run_replay(migrated)
    assert service.health.our_car == OUR_CAR
    assert service.health.feed_laps == 4 and service.health.vehicle_laps == 2
    with psycopg.connect(migrated) as conn:
        findings = conn.execute(
            "SELECT monitor, severity, closed_at, summary FROM v_watch_findings"
        ).fetchall()
        assert len(findings) == 1
        monitor, severity, closed_at, summary = findings[0]
        assert monitor == "field.lap_count" and severity == "warning" and closed_at is None
        assert summary["higher"] == "feed" and summary["delta"] == 2

        offsets = conn.execute(
            "SELECT metric, value FROM v_pit_metrics WHERE source = 'timing-feed' ORDER BY time"
        ).fetchall()
        by_metric: dict[str, list[float]] = {}
        for metric, value in offsets:
            by_metric.setdefault(metric, []).append(value)
        # Two of the four passings have a vehicle crossing to pair with.
        assert by_metric["clock_offset_s"] == pytest.approx([0.3, 0.3])
        assert by_metric["feed_latency_s"] == pytest.approx([0.0, 0.0])

        gaps = conn.execute(
            "SELECT lap_number, car_number, gap_to_us_s, laps_to_us FROM v_field_gaps "
            "WHERE lap_number = 2 ORDER BY car_number"
        ).fetchall()
        assert [(row[1], row[3]) for row in gaps] == [("14", 0), ("7", 0), ("99", 0)]
        assert all(row[2] is not None and row[2] > 0 for row in gaps), "everyone is behind us"
        assert (
            conn.execute("SELECT count(DISTINCT lap_number) FROM v_field_gaps").fetchone()[0] == 4
        )

    # The rendered rule fires on it as the Grafana role -- two laps apart is
    # the warning rule's, not the critical one's.
    assert _rule_value(migrated, "field-warning") == 1.0
    assert _rule_value(migrated, "field-critical") == 0.0

    # The vehicle catches up: the finding closes on the next reconciliation.
    with psycopg.connect(migrated) as conn:
        for number, crossed_at in enumerate(vehicle_stamps[2:], start=3):
            conn.execute(
                "INSERT INTO laps (vehicle_id, session_id, track_name, lap_number, crossed_at, "
                "lap_time_s) VALUES (%s, 's-1', 'Wanneroo', %s, %s, 95.0)",
                (VEHICLE, number, crossed_at),
            )
        conn.commit()
    database = FieldDatabase(migrated, VEHICLE)
    assert database.adopt_open_findings() == 1
    again = TimingFeedService(
        TimingFeedSettings(dsn=migrated, vehicle_id=VEHICLE, source_kind="none"),
        database=database,
    )
    again.state = service.state
    assert asyncio.run(again.reconcile()) == []
    with psycopg.connect(migrated) as conn:
        (closed,) = conn.execute(
            "SELECT count(*) FROM v_watch_findings WHERE closed_at IS NOT NULL"
        ).fetchone()
        assert closed == 1
    assert _rule_value(migrated, "field-warning") == 0.0, "resting once the finding closes"


def test_a_relay_snapshot_lands_in_the_same_schema(migrated):
    database = FieldDatabase(migrated, VEHICLE)
    service = TimingFeedService(
        TimingFeedSettings(dsn=migrated, vehicle_id=VEHICLE, source_kind="none"),
        database=database,
    )
    at = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)
    body = {
        "session": {"flag_state": "green", "time_remaining_s": 3600},
        "cars": [
            {"car_number": "27", "laps": 40, "last_lap_s": "1:35.1"},
            {"car_number": "14", "laps": 40, "last_lap_s": "1:36.0", "gap_lead_s": "2.5"},
        ],
    }

    async def scenario():
        await service.handle_snapshot(snapshot_from_json(body, at, "relay"))
        body["cars"][1]["laps"] = 41
        await service.handle_snapshot(snapshot_from_json(body, at + timedelta(seconds=90), "relay"))

    asyncio.run(scenario())
    database.close()
    with psycopg.connect(migrated) as conn:
        rows = conn.execute(
            "SELECT car_number, laps, position, source FROM v_field_standings ORDER BY position"
        ).fetchall()
        assert rows == [("27", 40, 1, "relay"), ("14", 41, 2, "relay")]
        assert conn.execute("SELECT count(*) FROM field_cars").fetchone()[0] == 3
        assert conn.execute("SELECT car_number, lap_number FROM v_field_laps").fetchall() == [
            ("14", 41)
        ]


def test_the_grafana_role_reads_the_field_views_and_not_the_tables(migrated):
    _run_replay(migrated, reconcile=False)
    grafana = make_conninfo(migrated, user="grafana_ro", password="openlaps-grafana-test")
    with psycopg.connect(grafana) as reader:
        assert reader.execute("SELECT count(*) FROM v_field_standings").fetchone()[0] == 4
        assert reader.execute("SELECT count(*) FROM v_field_flags").fetchone()[0] == 6
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            reader.execute("SELECT * FROM field_laps LIMIT 1")
