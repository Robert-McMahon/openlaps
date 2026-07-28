"""Historical InfluxDB line-protocol importer tests (P3.8)."""

from __future__ import annotations

import asyncio
import gzip
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import import_legacy as legacy  # noqa: E402

from core.pb import telemetry_pb2 as pb  # noqa: E402
from pit.db.migrate import apply_migrations  # noqa: E402

MAP_PATH = Path(__file__).parents[1] / "tools" / "legacy_channel_map.yaml"


def test_line_protocol_parser_handles_observed_can_shape_without_a_value():
    point = legacy.parse_line_protocol(
        "can,message=ambient,signal=ambient_air_temperature,topic=telemetry/can/ambient "
        'units="K" 1749784516615000000'
    )

    assert point.measurement == "can"
    assert point.tags["signal"] == "ambient_air_temperature"
    assert point.fields == {"units": "K"}
    assert point.timestamp_ns == 1_749_784_516_615_000_000


def test_line_protocol_parser_handles_numeric_gps_and_lap_shapes():
    gps = legacy.parse_line_protocol(
        "gps,topic=telemetry/gps/data,track_name=Wanneroo heading=240.7 1749784529601615234"
    )
    lap = legacy.parse_line_protocol(
        "lap,crossed_line=PitEntry,lap_number=14,sector=3,track_name=Wanneroo "
        "lap_time=108842.29711914062 1749788158462718017"
    )

    assert gps.fields == {"heading": pytest.approx(240.7)}
    assert lap.tags["lap_number"] == "14"
    assert lap.fields == {"lap_time": pytest.approx(108842.29711914062)}


def test_line_protocol_parser_unescapes_tags_and_string_fields():
    point = legacy.parse_line_protocol(
        r'meas,tag=a\,b\=c\ d text="quote\" and slash\\",count=7i,ok=true 42'
    )

    assert point.tags == {"tag": "a,b=c d"}
    assert point.fields == {"text": 'quote" and slash\\', "count": 7, "ok": True}
    assert point.timestamp_ns == 42


def test_checked_in_mapping_covers_representative_catalog_entries_and_exceptions():
    mapping = legacy.load_channel_map(MAP_PATH)

    assert mapping.can["ENGINE_SPEED"].name == "car.rpm"
    assert mapping.can["WHEEL_SPEED_REAR_RIGHT"].name == "car.wheel_speed_rr"
    assert mapping.can["PD16A_TOTAL_CURRENT"].name == "car.pd16_total_current"
    assert "GEARBOX_TEMPERATURE" in mapping.unmapped
    assert "PD16A_HCO*_CURRENT" in mapping.unmapped


def test_can_units_rows_are_ignored_and_values_use_the_canonical_channel():
    mapping = legacy.load_channel_map(MAP_PATH)
    units = legacy.convert_point(
        legacy.parse_line_protocol(
            'can,message=engine1,signal=engine_speed units="RPM" 1749784516615000000'
        ),
        mapping,
    )
    value = legacy.convert_point(
        legacy.parse_line_protocol(
            "can,message=engine1,signal=engine_speed value=4500 1749784516615000000"
        ),
        mapping,
    )

    assert units.samples == ()
    assert value.samples[0].channel.name == "car.rpm"
    assert value.samples[0].value == pytest.approx(4500.0)


def test_gps_speed_is_converted_from_legacy_knots_to_canonical_kmh():
    mapping = legacy.load_channel_map(MAP_PATH)
    converted = legacy.convert_point(
        legacy.parse_line_protocol(
            "gps,topic=telemetry/gps/data,track_name=Wanneroo speed=60 1749784529601615234"
        ),
        mapping,
    )

    assert converted.samples[0].channel.name == "position.speed"
    assert converted.samples[0].channel.units == "km/h"
    assert converted.samples[0].value == pytest.approx(111.12)


def test_unmapped_names_are_reported_instead_of_silently_dropped():
    mapping = legacy.load_channel_map(MAP_PATH)
    converted = legacy.convert_point(
        legacy.parse_line_protocol(
            "can,message=temperature2,signal=gearbox_temperature value=350 1749784516615000000"
        ),
        mapping,
    )

    assert converted.samples == ()
    assert converted.unmapped == ("can:GEARBOX_TEMPERATURE",)


def test_legacy_lap_times_and_sector_splits_convert_milliseconds_to_seconds():
    completed = legacy.convert_lap_point(
        legacy.parse_line_protocol(
            "lap,crossed_line=StartFinish,crossing_direction=counterclockwise,"
            "event_type=line_crossing,lap_number=14,pit_status=track,sector=1,"
            "track_name=Wanneroo last_lap_time=108842.29711914062 1749788158462718017"
        ),
        "example-club-racer",
    )
    assert completed.laps[0].lap_time_s == pytest.approx(108.84229711914062)
    assert completed.laps[0].crossed_at == datetime.fromtimestamp(1_749_788_158.462718, tz=UTC)


def test_legacy_cumulative_lap_times_become_three_sector_splits():
    def point(line: str, lap_time_ms: float, timestamp_ns: int, lap_number: int):
        return legacy.LinePoint(
            measurement="lap",
            tags={
                "crossed_line": line,
                "crossing_direction": "clockwise" if line == "Sector1" else "counterclockwise",
                "lap_number": str(lap_number),
                "pit_status": "track",
                "track_name": "Wanneroo",
            },
            fields={
                "lap_time": lap_time_ms,
                **({"last_lap_time": 108_842.3} if line == "StartFinish" else {}),
            },
            timestamp_ns=timestamp_ns,
        )

    converted = legacy.materialize_legacy_laps(
        [
            point("Sector1", 40_123.5, 100_000_000_000, 13),
            point("Sector2", 70_000.0, 130_000_000_000, 13),
            point("StartFinish", 0.0, 168_842_300_000, 14),
        ],
        "example-club-racer",
    )

    assert len(converted.laps) == 1
    assert [sector.sector for sector in converted.sectors] == [1, 2, 3]
    assert [sector.split_time_s for sector in converted.sectors] == pytest.approx(
        [40.1235, 29.8765, 38.8423]
    )


@pytest.mark.parametrize(
    ("sector_two_tags", "finish_tags"),
    [
        ({"track_name": "Barbagallo"}, {}),
        ({}, {"lap_number": "15"}),
    ],
)
def test_legacy_sector_assembly_rejects_discontinuous_crossings(
    sector_two_tags: dict[str, str], finish_tags: dict[str, str]
):
    def point(line: str, elapsed_ms: float, timestamp_ns: int, lap_number: int):
        tags = {
            "crossed_line": line,
            "crossing_direction": "counterclockwise",
            "lap_number": str(lap_number),
            "pit_status": "track",
            "track_name": "Wanneroo",
        }
        if line == "Sector2":
            tags.update(sector_two_tags)
        elif line == "StartFinish":
            tags.update(finish_tags)
        return legacy.LinePoint(
            measurement="lap",
            tags=tags,
            fields={
                "lap_time": elapsed_ms,
                **({"last_lap_time": 108_842.3} if line == "StartFinish" else {}),
            },
            timestamp_ns=timestamp_ns,
        )

    converted = legacy.materialize_legacy_laps(
        [
            point("Sector1", 40_000.0, 100_000_000_000, 13),
            point("Sector2", 70_000.0, 130_000_000_000, 13),
            point("StartFinish", 0.0, 168_842_300_000, 14),
        ],
        "example-club-racer",
    )

    assert len(converted.laps) == 1
    assert converted.sectors == ()


def test_legacy_pit_crossings_become_pit_status_updates():
    point = legacy.parse_line_protocol(
        "lap,crossed_line=PitEntry,crossing_direction=counterclockwise,event_type=line_crossing,"
        "lap_number=14,pit_status=pit_entry,sector=3,track_name=Wanneroo "
        "lap_time=108842.29711914062 1749788158462718017"
    )

    converted = legacy.convert_lap_point(point, "example-club-racer")

    assert len(converted.pit_updates) == 1
    assert converted.pit_updates[0].pit_status == "pit_entry"


def test_small_import_registers_generation_zero_and_is_idempotent(
    timescale_dsn: str, tmp_path: Path
):
    with psycopg.connect(timescale_dsn) as conn:
        apply_migrations(conn)
        # Importing history after live ingest must not replace the live
        # registry's current metadata on the stable channel row.
        conn.execute(
            "INSERT INTO channels (vehicle_id, name, units, value_type) VALUES (%s, %s, %s, %s)",
            ("example-club-racer", "car.rpm", "live-rpm", pb.BOOL),
        )

    source = tmp_path / "legacy.lp.gz"
    lines = [
        "can,message=engine1,signal=engine_speed value=4500 1749784516615000000",
        "gps,topic=telemetry/gps/data,track_name=Wanneroo lat=-31.66 1749784516615000000",
        "gps,topic=telemetry/gps/data,track_name=Wanneroo speed=60 1749784516615000000",
        "lap,crossed_line=StartFinish,crossing_direction=counterclockwise,"
        "event_type=line_crossing,lap_number=14,pit_status=track,sector=1,"
        "track_name=Wanneroo last_lap_time=108842.29711914062 1749788158462718017",
        # The real dump is grouped by field: all last_lap_time rows precede
        # all sector_time rows, regardless of crossing timestamp.
        "lap,crossed_line=Sector1,crossing_direction=counterclockwise,event_type=line_crossing,"
        "lap_number=13,pit_status=track,sector=2,track_name=Wanneroo "
        "sector_time=40123.5 1749788100000000000",
        "lap,crossed_line=StartFinish,crossing_direction=counterclockwise,"
        "event_type=line_crossing,lap_number=14,pit_status=track,sector=1,"
        "track_name=Wanneroo sector_time=68718.8 1749788158462718017",
        "lap,crossed_line=PitEntry,crossing_direction=counterclockwise,event_type=line_crossing,"
        "lap_number=14,pit_status=pit_entry,sector=3,track_name=Wanneroo "
        "lap_time=109000 1749788160000000000",
    ]
    with gzip.open(source, "wt", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    kwargs = {
        "paths": [source],
        "dsn": timescale_dsn,
        "vehicle_id": "example-club-racer",
        "mapping": legacy.load_channel_map(MAP_PATH),
        "batch_rows": 2,
    }
    first = asyncio.run(legacy.import_paths(**kwargs))
    resumed = asyncio.run(legacy.import_paths(**kwargs, resume=True))
    rerun = asyncio.run(legacy.import_paths(**kwargs))

    assert first.samples == 3
    assert first.laps == 1
    assert first.sectors == 2
    assert first.pit_updates == 1
    assert resumed.samples == resumed.laps == resumed.sectors == 0
    assert resumed.pit_updates == 0
    assert resumed.resumed_bytes > 0
    assert rerun.samples == rerun.laps == rerun.sectors == 0
    assert rerun.pit_updates == 0
    with psycopg.connect(timescale_dsn) as conn:
        assert conn.execute("SELECT count(*) FROM samples").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM laps").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM lap_sectors").fetchone()[0] == 2
        assert (
            conn.execute(
                "SELECT count(*) FROM channel_registry WHERE vehicle_id = %s AND registry_seq = 0",
                ("example-club-racer",),
            ).fetchone()[0]
            == 1
        )
        channels = dict(
            conn.execute("SELECT channel, value FROM v_samples_named ORDER BY channel").fetchall()
        )
        rpm_metadata = conn.execute(
            "SELECT units, value_type FROM channels WHERE vehicle_id = %s AND name = 'car.rpm'",
            ("example-club-racer",),
        ).fetchone()
        lap_status = conn.execute("SELECT pit_status FROM laps").fetchone()[0]
    assert channels["car.rpm"] == pytest.approx(4500.0)
    assert channels["position.speed"] == pytest.approx(111.12)
    assert rpm_metadata == ("live-rpm", pb.BOOL)
    assert lap_status == "pit_entry"
