"""Pit schema: the migration applier, and what 001_init actually builds."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from pit.db.dsn import dsn_from_env, redacted
from pit.db.migrate import apply_migrations, discover, pending

VEHICLE = "example-club-racer"
# openlaps.v1.ValueType numbers.
DOUBLE, UINT, STRING = 1, 6, 4


def _register(
    conn: psycopg.Connection,
    *,
    name: str,
    registry_seq: int,
    wire_id: int,
    units: str = "",
    value_type: int = DOUBLE,
    scale: float = 0.0,
    offset: float = 0.0,
) -> int:
    """Resolve one wire id in one generation to a stable channel_key.

    The same three upserts the ingest-writer will do: generation, stable
    channel identity, then the per-generation wire-id mapping.
    """
    conn.execute(
        "INSERT INTO channel_registry (vehicle_id, registry_seq, created) VALUES (%s, %s, now()) "
        "ON CONFLICT (vehicle_id, registry_seq) DO NOTHING",
        (VEHICLE, registry_seq),
    )
    channel_key = conn.execute(
        "INSERT INTO channels (vehicle_id, name, units, value_type) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (vehicle_id, name) DO UPDATE "
        "SET units = EXCLUDED.units, value_type = EXCLUDED.value_type "
        "RETURNING channel_key",
        (VEHICLE, name, units, value_type),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO channel_map (vehicle_id, registry_seq, wire_id, channel_key, source_ref, "
        'units, value_type, scale, "offset") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)',
        (
            VEHICLE,
            registry_seq,
            wire_id,
            channel_key,
            f"can0:test.{name}",
            units,
            value_type,
            scale,
            offset,
        ),
    )
    return channel_key


@pytest.fixture
def migrated(timescale_dsn):
    """A connection to a database with every migration applied."""
    with psycopg.connect(timescale_dsn, autocommit=False) as conn:
        apply_migrations(conn)
        yield conn


# --- the applier, without a database --------------------------------------


def test_discover_returns_migrations_in_filename_order():
    names = [path.name for path in discover()]
    assert names == sorted(names)
    assert "001_init.sql" in names
    assert "002_trace_read_surface.sql" in names


def test_dsn_from_env_prefers_an_explicit_dsn():
    env = {"TIMESCALE_DSN": "postgresql://x/y", "TIMESCALE_HOST": "ignored"}
    assert dsn_from_env(env) == "postgresql://x/y"


def test_dsn_from_env_builds_from_parts():
    dsn = dsn_from_env(
        {
            "TIMESCALE_HOST": "pit-db",
            "TIMESCALE_DB": "openlaps",
            "TIMESCALE_USER": "writer",
            "TIMESCALE_PASSWORD": "hunter2",
        }
    )
    assert "host=pit-db" in dsn
    assert "port=5432" in dsn
    assert "dbname=openlaps" in dsn


def test_dsn_from_env_names_what_is_missing():
    with pytest.raises(ValueError, match="TIMESCALE_HOST, TIMESCALE_DB, TIMESCALE_USER"):
        dsn_from_env({})


def test_redacted_hides_the_password():
    dsn = dsn_from_env(
        {
            "TIMESCALE_HOST": "pit-db",
            "TIMESCALE_DB": "openlaps",
            "TIMESCALE_USER": "writer",
            "TIMESCALE_PASSWORD": "hunter2",
        }
    )
    assert "hunter2" not in redacted(dsn)
    assert "user=writer" in redacted(dsn)


# --- migrations against a real Timescale ----------------------------------


def test_migrations_apply_from_empty_and_are_idempotent(timescale_dsn):
    with psycopg.connect(timescale_dsn) as conn:
        assert [path.name for path in pending(conn)] == [path.name for path in discover()]
        applied = apply_migrations(conn)
        assert applied == [path.name for path in discover()]

        # Second run sees nothing to do and changes nothing.
        assert apply_migrations(conn) == []
        assert pending(conn) == []
        recorded = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        assert recorded == len(applied)


def test_002_is_pending_once_on_a_database_with_001(tmp_path, timescale_dsn):
    first = next(path for path in discover() if path.name == "001_init.sql")
    (tmp_path / first.name).write_text(first.read_text(encoding="utf-8"), encoding="utf-8")

    with psycopg.connect(timescale_dsn) as conn:
        assert apply_migrations(conn, tmp_path) == ["001_init.sql"]
        assert [path.name for path in pending(conn)] == ["002_trace_read_surface.sql"]
        assert apply_migrations(conn) == ["002_trace_read_surface.sql"]
        assert pending(conn) == []


def test_samples_is_an_hourly_hypertable(migrated):
    row = migrated.execute(
        "SELECT column_name, time_interval FROM timescaledb_information.dimensions "
        "WHERE hypertable_name = 'samples'"
    ).fetchall()
    assert row == [("time", timedelta(hours=1))]


def test_numeric_and_string_values_round_trip_through_the_named_view(migrated):
    rpm = _register(migrated, name="car.rpm", registry_seq=1, wire_id=7, units="rpm")
    event = _register(migrated, name="lap.event", registry_seq=1, wire_id=8, value_type=STRING)
    stamp = datetime(2026, 7, 27, 4, 30, tzinfo=UTC)
    migrated.execute(
        "INSERT INTO samples (time, channel_key, value, value_text) VALUES (%s, %s, %s, NULL)",
        (stamp, rpm, 6421.5),
    )
    migrated.execute(
        "INSERT INTO samples (time, channel_key, value, value_text) VALUES (%s, %s, NULL, %s)",
        (stamp, event, '{"type":"lap_completed"}'),
    )

    rows = migrated.execute(
        "SELECT channel, units, value, value_text FROM v_samples_named "
        "WHERE vehicle_id = %s ORDER BY channel",
        (VEHICLE,),
    ).fetchall()
    assert rows == [
        ("car.rpm", "rpm", 6421.5, None),
        ("lap.event", "", None, '{"type":"lap_completed"}'),
    ]


def test_samples_1s_buckets_numeric_rows_with_extrema_and_count(migrated):
    rpm = _register(migrated, name="car.rpm", registry_seq=1, wire_id=7, units="rpm")
    event = _register(migrated, name="lap.event", registry_seq=1, wire_id=8, value_type=STRING)
    bucket = datetime(2026, 7, 27, 4, 30, tzinfo=UTC)
    with migrated.cursor() as cur:
        cur.executemany(
            "INSERT INTO samples (time, channel_key, value, value_text) VALUES (%s, %s, %s, %s)",
            [
                (bucket + timedelta(milliseconds=100), rpm, 6100.0, None),
                (bucket + timedelta(milliseconds=500), rpm, 6500.0, None),
                (bucket + timedelta(milliseconds=900), rpm, 7000.0, None),
                (bucket + timedelta(milliseconds=200), event, None, '{"type":"lap_completed"}'),
            ],
        )
    migrated.commit()
    migrated.autocommit = True
    migrated.execute(
        "CALL refresh_continuous_aggregate('samples_1s', %s, %s)",
        (bucket, bucket + timedelta(seconds=1)),
    )
    migrated.autocommit = False

    rows = migrated.execute(
        "SELECT time, vehicle_id, channel, units, avg, min, max, count "
        "FROM v_samples_1s_named WHERE time = %s ORDER BY channel",
        (bucket,),
    ).fetchall()
    assert rows == [(bucket, VEHICLE, "car.rpm", "rpm", 6533.333333333333, 6100.0, 7000.0, 3)]


def test_samples_1s_policy_covers_late_data_and_keeps_recent_data_live(migrated):
    schedule, start_offset, end_offset = migrated.execute(
        "SELECT schedule_interval, (config ->> 'start_offset')::interval, "
        "(config ->> 'end_offset')::interval FROM timescaledb_information.jobs "
        "WHERE proc_name = 'policy_refresh_continuous_aggregate' "
        "AND hypertable_name = 'samples_1s'"
    ).fetchone()
    materialized_only = migrated.execute(
        "SELECT materialized_only FROM timescaledb_information.continuous_aggregates "
        "WHERE view_name = 'samples_1s'"
    ).fetchone()[0]

    assert schedule == timedelta(seconds=30)
    assert start_offset is None
    assert end_offset == timedelta(seconds=2)
    assert materialized_only is False


def test_grafana_role_reads_every_view_but_not_base_tables(migrated, timescale_dsn):
    migrated.commit()
    grafana_dsn = make_conninfo(
        timescale_dsn,
        user="grafana_ro",
        password="openlaps-grafana-test",
    )
    with psycopg.connect(grafana_dsn) as reader:
        for view in ("v_samples_named", "v_samples_1s_named", "v_laps"):
            reader.execute(sql.SQL("SELECT * FROM {} LIMIT 0").format(sql.Identifier(view)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            reader.execute("SELECT * FROM samples LIMIT 0")


def test_one_channel_key_survives_a_registry_rollover(migrated):
    """The whole point of channel_map: wire ids renumber, history doesn't."""
    first = _register(migrated, name="car.coolant_temp", registry_seq=1, wire_id=3, units="K")
    second = _register(
        migrated,
        name="car.coolant_temp",
        registry_seq=2,
        wire_id=91,
        units="K",
        value_type=UINT,
        scale=0.1,
    )
    assert first == second

    stamp = datetime(2026, 7, 27, 5, 0, tzinfo=UTC)
    for index, generation in enumerate((1, 2)):
        wire_id, raw = (3, 355.0) if generation == 1 else (91, 3560)
        key, scale, offset = migrated.execute(
            'SELECT channel_key, scale, "offset" FROM channel_map '
            "WHERE vehicle_id = %s AND registry_seq = %s AND wire_id = %s",
            (VEHICLE, generation, wire_id),
        ).fetchone()
        physical = raw * scale + offset if scale else raw
        migrated.execute(
            "INSERT INTO samples (time, channel_key, value) VALUES (%s, %s, %s)",
            (stamp + timedelta(seconds=index), key, physical),
        )

    rows = migrated.execute(
        "SELECT channel, value FROM v_samples_named ORDER BY time",
    ).fetchall()
    assert rows == [("car.coolant_temp", 355.0), ("car.coolant_temp", 356.0)]
    assert migrated.execute("SELECT count(*) FROM channels").fetchone()[0] == 1


def test_compression_policy_exists_and_a_compressed_chunk_still_answers(migrated):
    job = migrated.execute(
        "SELECT config ->> 'compress_after' FROM timescaledb_information.jobs "
        "WHERE proc_name = 'policy_compression' AND hypertable_name = 'samples'"
    ).fetchone()
    assert job == ("7 days",)

    key = _register(migrated, name="car.wheel_speed_fl", registry_seq=1, wire_id=1, units="km/h")
    base = datetime.now(tz=UTC) - timedelta(days=30)
    with migrated.cursor() as cur:
        with cur.copy("COPY samples (time, channel_key, value) FROM STDIN") as copy:
            for index in range(500):
                copy.write_row((base + timedelta(milliseconds=index * 20), key, float(index)))
    migrated.commit()

    compressed = migrated.execute(
        "SELECT count(*) FROM (SELECT compress_chunk(c) FROM show_chunks('samples') c) s"
    ).fetchone()[0]
    assert compressed >= 1

    total, largest = migrated.execute(
        "SELECT count(*), max(value) FROM samples "
        "WHERE channel_key = %s AND time >= %s AND time < %s",
        (key, base, base + timedelta(minutes=1)),
    ).fetchone()
    assert (total, largest) == (500, 499.0)


_LAP_UPSERT = (
    "INSERT INTO laps (vehicle_id, session_id, stint_id, track_name, lap_number, crossed_at, "
    "lap_time_s, valid, pit_status, direction) "
    "VALUES (%s, %s, %s, 'Wanneroo', %s, %s, %s, TRUE, 'track', 'counterclockwise') "
    "ON CONFLICT (vehicle_id, crossed_at) DO UPDATE "
    "SET lap_time_s = EXCLUDED.lap_time_s, lap_number = EXCLUDED.lap_number, "
    "session_id = EXCLUDED.session_id, stint_id = EXCLUDED.stint_id"
)


def _open_session(conn: psycopg.Connection, session_id: str = "s-1") -> int:
    """A session with one stint; returns the stint_id."""
    driver = conn.execute(
        "INSERT INTO drivers (name) VALUES ('Driver A') "
        "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING driver_id"
    ).fetchone()[0]
    started = datetime(2026, 7, 27, 1, 0, tzinfo=UTC)
    conn.execute(
        "INSERT INTO sessions (session_id, vehicle_id, session_type, track_name, car, started, "
        "status) VALUES (%s, %s, 'practice', 'Wanneroo', 'club-racer', %s, 'active')",
        (session_id, VEHICLE, started),
    )
    return conn.execute(
        "INSERT INTO stints (session_id, stint_number, driver_id, started) "
        "VALUES (%s, 1, %s, %s) RETURNING stint_id",
        (session_id, driver, started),
    ).fetchone()[0]


def test_laps_upsert_converges_with_and_without_a_session(migrated):
    stint = _open_session(migrated)
    sessioned = datetime(2026, 7, 27, 1, 2, tzinfo=UTC)
    unsessioned = datetime(2026, 7, 27, 3, 40, tzinfo=UTC)
    # Redelivery presents the same crossing instant, so a replay converges.
    for _ in range(2):
        migrated.execute(_LAP_UPSERT, (VEHICLE, "s-1", stint, 4, sessioned, 108.842))
        # A lap with no session open is still a lap, and must also converge.
        migrated.execute(_LAP_UPSERT, (VEHICLE, None, None, 4, unsessioned, 109.101))

    rows = migrated.execute(
        "SELECT session_id, lap_number, lap_time_s, driver, session_type, stint_number "
        "FROM v_laps ORDER BY crossed_at"
    ).fetchall()
    assert rows == [
        ("s-1", 4, 108.842, "Driver A", "practice", 1),
        (None, 4, 109.101, None, None, None),
    ]


def test_an_agent_restart_replaying_lap_numbers_does_not_overwrite(migrated):
    """lap_number restarts at 1 on an agent restart; the session_id does not."""
    stint = _open_session(migrated)
    first_run = datetime(2026, 7, 27, 1, 0, tzinfo=UTC)
    for lap in range(1, 4):
        migrated.execute(
            _LAP_UPSERT, (VEHICLE, "s-1", stint, lap, first_run + timedelta(minutes=2 * lap), 108.8)
        )
    # Same session, engine rebuilt, numbering back to 1.
    second_run = datetime(2026, 7, 27, 1, 30, tzinfo=UTC)
    for lap in range(1, 4):
        migrated.execute(
            _LAP_UPSERT,
            (VEHICLE, "s-1", stint, lap, second_run + timedelta(minutes=2 * lap), 107.4),
        )

    laps = migrated.execute(
        "SELECT lap_number, lap_time_s FROM laps ORDER BY crossed_at"
    ).fetchall()
    assert laps == [(1, 108.8), (2, 108.8), (3, 108.8), (1, 107.4), (2, 107.4), (3, 107.4)]


def test_two_vehicles_may_cross_at_the_same_instant(migrated):
    crossed = datetime(2026, 7, 27, 1, 2, tzinfo=UTC)
    migrated.execute(_LAP_UPSERT, (VEHICLE, None, None, 4, crossed, 108.8))
    migrated.execute(_LAP_UPSERT, ("other-car", None, None, 9, crossed, 95.2))
    assert migrated.execute("SELECT count(*) FROM laps").fetchone()[0] == 2


def test_samples_has_no_foreign_key(migrated):
    """Deliberate: the FK check is real cost on the COPY path."""
    constraints = migrated.execute(
        "SELECT count(*) FROM pg_constraint WHERE conrelid = 'samples'::regclass AND contype = 'f'"
    ).fetchone()[0]
    assert constraints == 0


def test_session_type_and_status_are_constrained(migrated):
    with pytest.raises(psycopg.errors.CheckViolation):
        migrated.execute(
            "INSERT INTO sessions (session_id, vehicle_id, session_type, started, status) "
            "VALUES ('bad', %s, 'endurance', now(), 'active')",
            (VEHICLE,),
        )
