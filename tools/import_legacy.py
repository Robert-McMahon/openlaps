#!/usr/bin/env python3
"""Stream legacy InfluxDB line-protocol dumps into the pit TimescaleDB (P3.8)."""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import sqlite3
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from core.pb import telemetry_pb2 as pb
from pit.db.dsn import dsn_from_env
from pit.ingest_writer.laps import LapRow, PitStatusUpdate
from pit.ingest_writer.store import (
    LegacySectorRow,
    SampleRow,
    SyntheticChannel,
    TimescaleStore,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAP = Path(__file__).with_name("legacy_channel_map.yaml")
DEFAULT_INPUT_DIR = Path("/mnt/data/logger/backups/backup_migration_tmp")
DEFAULT_INPUTS = tuple(
    DEFAULT_INPUT_DIR / name for name in ("can.lp.gz", "gps.lp.gz", "lap.lp.gz", "system.lp.gz")
)
DEFAULT_BATCH_ROWS = 5_000
CHECKPOINT_LINE_INTERVAL = 100_000

_VALUE_TYPES = {
    "double": pb.DOUBLE,
    "int": pb.INT64,
    "bool": pb.BOOL,
    "string": pb.STRING,
}


@dataclass(frozen=True, slots=True)
class LinePoint:
    """One parsed InfluxDB line-protocol point."""

    measurement: str
    tags: dict[str, str]
    fields: dict[str, float | int | bool | str]
    timestamp_ns: int


@dataclass(frozen=True, slots=True)
class LegacyChannel:
    """Canonical metadata and optional linear transform for one old name."""

    name: str
    units: str
    value_type: str
    factor: float = 1.0
    offset: float = 0.0


@dataclass(frozen=True, slots=True)
class LegacyChannelMap:
    can: dict[str, LegacyChannel]
    gps: dict[str, LegacyChannel]
    system: dict[str, LegacyChannel]
    unmapped: frozenset[str]


@dataclass(frozen=True, slots=True)
class LegacySample:
    timestamp_ns: int
    channel: LegacyChannel
    value: float | int | bool | str


@dataclass(frozen=True, slots=True)
class ConvertedPoint:
    samples: tuple[LegacySample, ...] = ()
    unmapped: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConvertedLapPoint:
    laps: tuple[LapRow, ...] = ()
    sectors: tuple[LegacySectorRow, ...] = ()
    pit_updates: tuple[PitStatusUpdate, ...] = ()


@dataclass(slots=True)
class ImportReport:
    files: int = 0
    lines: int = 0
    samples: int = 0
    laps: int = 0
    sectors: int = 0
    pit_updates: int = 0
    ignored: int = 0
    filtered: int = 0
    malformed: int = 0
    resumed_bytes: int = 0
    unmapped: Counter[str] = dataclass_field(default_factory=Counter)

    def merge(self, other: ImportReport) -> None:
        for name in (
            "files",
            "lines",
            "samples",
            "laps",
            "sectors",
            "pit_updates",
            "ignored",
            "filtered",
            "malformed",
            "resumed_bytes",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.unmapped.update(other.unmapped)

    def as_dict(self) -> dict[str, object]:
        return {
            "files": self.files,
            "lines": self.lines,
            "samples": self.samples,
            "laps": self.laps,
            "sectors": self.sectors,
            "pit_updates": self.pit_updates,
            "ignored": self.ignored,
            "filtered": self.filtered,
            "malformed": self.malformed,
            "resumed_bytes": self.resumed_bytes,
            "unmapped": dict(self.unmapped.most_common()),
        }


def load_channel_map(path: str | Path) -> LegacyChannelMap:
    """Load the checked-in old-name mapping, rejecting malformed entries."""
    source = Path(path)
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"{source}: unable to load legacy channel map: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{source}: channel map must be a mapping")
    return LegacyChannelMap(
        can=_load_map_section(source, raw, "can"),
        gps=_load_map_section(source, raw, "gps"),
        system=_load_map_section(source, raw, "system"),
        unmapped=frozenset(str(name) for name in raw.get("unmapped", ())),
    )


def _load_map_section(
    source: Path, raw: dict[object, object], section: str
) -> dict[str, LegacyChannel]:
    entries = raw.get(section, {})
    if not isinstance(entries, dict):
        raise ValueError(f"{source}: {section} must be a mapping")
    result: dict[str, LegacyChannel] = {}
    for old_name, value in entries.items():
        if not isinstance(old_name, str) or not isinstance(value, dict):
            raise ValueError(f"{source}: {section} entries must map names to metadata")
        try:
            name = value["channel"]
            units = value.get("units", "")
            value_type = value.get("type", "double")
            factor = float(value.get("factor", 1.0))
            offset = float(value.get("offset", 0.0))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{source}: invalid {section}.{old_name}: {exc}") from exc
        if not all(isinstance(item, str) for item in (name, units, value_type)):
            raise ValueError(f"{source}: invalid string metadata for {section}.{old_name}")
        if value_type not in {"double", "int", "bool", "string"}:
            raise ValueError(f"{source}: unsupported type {value_type!r} for {section}.{old_name}")
        result[old_name] = LegacyChannel(name, units, value_type, factor, offset)
    return result


def convert_point(point: LinePoint, mapping: LegacyChannelMap) -> ConvertedPoint:
    """Map one parsed point to canonical samples, or name what could not map."""
    if point.measurement == "can":
        signal = point.tags.get("signal", "").upper()
        channel = mapping.can.get(signal)
        if channel is None:
            return ConvertedPoint(unmapped=(f"can:{signal or '<missing signal>'}",))
        if "value" not in point.fields:
            return ConvertedPoint()  # units-only metadata row
        return ConvertedPoint(samples=(_sample(point, channel, point.fields["value"]),))
    if point.measurement == "gps":
        return _convert_fields(point, mapping.gps, "gps")
    if point.measurement == "system":
        topic = point.tags.get("topic", "")
        channel = mapping.system.get(topic)
        if channel is None:
            return ConvertedPoint(unmapped=(f"system:{topic or '<missing topic>'}",))
        if "value" not in point.fields:
            return ConvertedPoint()
        return ConvertedPoint(samples=(_sample(point, channel, point.fields["value"]),))
    if point.measurement == "lap":
        return ConvertedPoint()
    return ConvertedPoint(unmapped=(f"measurement:{point.measurement}",))


def convert_lap_point(point: LinePoint, vehicle_id: str) -> ConvertedLapPoint:
    """Materialise the useful field from one legacy lap crossing row."""
    if point.measurement != "lap":
        return ConvertedLapPoint()
    crossed_line = point.tags.get("crossed_line")
    at = _timestamp(point.timestamp_ns)
    track_name = point.tags.get("track_name") or None
    if crossed_line in {"PitEntry", "PitExit"}:
        fallback_status = "pit_entry" if crossed_line == "PitEntry" else "pit_exit"
        status = point.tags.get("pit_status") or fallback_status
        return ConvertedLapPoint(
            pit_updates=(PitStatusUpdate(vehicle_id=vehicle_id, at=at, pit_status=status),)
        )
    if crossed_line == "StartFinish" and "last_lap_time" in point.fields:
        lap_time_ms = _numeric(point.fields["last_lap_time"])
        if lap_time_ms <= 0:
            return ConvertedLapPoint()
        try:
            lap_number = int(point.tags["lap_number"])
        except (KeyError, ValueError):
            return ConvertedLapPoint()
        lap = LapRow(
            vehicle_id=vehicle_id,
            session_id=None,
            stint_number=None,
            track_name=track_name,
            lap_number=lap_number,
            crossed_at=at,
            lap_time_s=lap_time_ms / 1000.0,
            valid=True,
            pit_status=point.tags.get("pit_status") or None,
            direction=point.tags.get("crossing_direction") or None,
            sectors=(),
        )
        return ConvertedLapPoint(laps=(lap,))
    sector_by_line = {"Sector1": 1, "Sector2": 2, "StartFinish": 3}
    if crossed_line in sector_by_line and "sector_time" in point.fields:
        split_ms = _numeric(point.fields["sector_time"])
        if split_ms <= 0:
            return ConvertedLapPoint()
        sector = LegacySectorRow(
            vehicle_id=vehicle_id,
            track_name=track_name,
            sector=sector_by_line[crossed_line],
            split_time_s=split_ms / 1000.0,
            crossed_at=at,
        )
        return ConvertedLapPoint(sectors=(sector,))
    return ConvertedLapPoint()


def materialize_legacy_laps(points: list[LinePoint], vehicle_id: str) -> ConvertedLapPoint:
    """Turn crossing points into laps and splits from cumulative lap times."""
    laps: list[LapRow] = []
    sectors: list[LegacySectorRow] = []
    pit_updates: list[PitStatusUpdate] = []
    ordered = sorted(points, key=lambda item: item.timestamp_ns)
    for row in _iter_legacy_lap_rows(ordered, vehicle_id):
        if isinstance(row, LapRow):
            laps.append(row)
        elif isinstance(row, LegacySectorRow):
            sectors.append(row)
        else:
            pit_updates.append(row)
    return ConvertedLapPoint(tuple(laps), tuple(sectors), tuple(pit_updates))


def _iter_legacy_lap_rows(
    points: Iterable[LinePoint], vehicle_id: str
) -> Iterator[LapRow | LegacySectorRow | PitStatusUpdate]:
    """Fold timestamp-ordered crossing points with constant working memory."""
    sector_one: tuple[LinePoint, float] | None = None
    sector_two: tuple[LinePoint, float] | None = None
    for point in points:
        line = point.tags.get("crossed_line")
        if line == "Sector1" and "lap_time" in point.fields:
            elapsed = _numeric(point.fields["lap_time"])
            sector_one = (point, elapsed) if elapsed > 0 and _lap_context(point) else None
            sector_two = None
            continue
        if line == "Sector2" and "lap_time" in point.fields:
            elapsed = _numeric(point.fields["lap_time"])
            if (
                sector_one is not None
                and _lap_context(point) == _lap_context(sector_one[0])
                and elapsed > sector_one[1]
            ):
                sector_two = (point, elapsed)
            else:
                sector_one = sector_two = None
            continue
        if line in {"PitEntry", "PitExit"}:
            yield from convert_lap_point(point, vehicle_id).pit_updates
            continue
        if line != "StartFinish" or "last_lap_time" not in point.fields:
            continue
        total_ms = _numeric(point.fields["last_lap_time"])
        converted = convert_lap_point(point, vehicle_id)
        if not converted.laps:
            sector_one = sector_two = None
            continue
        yield from converted.laps
        track_name = point.tags.get("track_name") or None
        finish_context = _lap_context(point)
        expected_sector_context = (
            (finish_context[0], finish_context[1] - 1) if finish_context is not None else None
        )
        sectors_are_continuous = (
            sector_one is not None
            and sector_two is not None
            and _lap_context(sector_one[0]) == expected_sector_context
            and _lap_context(sector_two[0]) == expected_sector_context
        )
        if sectors_are_continuous and sector_one is not None:
            yield _legacy_sector(vehicle_id, track_name, 1, sector_one[1], sector_one[0])
        if sectors_are_continuous and sector_one is not None and sector_two is not None:
            yield _legacy_sector(
                vehicle_id,
                track_name,
                2,
                sector_two[1] - sector_one[1],
                sector_two[0],
            )
            if total_ms > sector_two[1]:
                yield _legacy_sector(
                    vehicle_id,
                    track_name,
                    3,
                    total_ms - sector_two[1],
                    point,
                )
        sector_one = sector_two = None


def _lap_context(point: LinePoint) -> tuple[str | None, int] | None:
    """Continuity key shared by sector crossings belonging to one legacy lap."""
    try:
        lap_number = int(point.tags["lap_number"])
    except (KeyError, ValueError):
        return None
    return (
        point.tags.get("track_name") or None,
        lap_number,
    )


def _legacy_sector(
    vehicle_id: str,
    track_name: str | None,
    sector: int,
    split_ms: float,
    crossing: LinePoint,
) -> LegacySectorRow:
    return LegacySectorRow(
        vehicle_id=vehicle_id,
        track_name=track_name,
        sector=sector,
        split_time_s=split_ms / 1000.0,
        crossed_at=_timestamp(crossing.timestamp_ns),
    )


def _numeric(value: float | int | bool | str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"expected numeric legacy value, got {value!r}")
    return float(value)


def _timestamp(timestamp_ns: int) -> datetime:
    return datetime.fromtimestamp(timestamp_ns / 1_000_000_000, tz=UTC)


def _convert_fields(
    point: LinePoint, channels: dict[str, LegacyChannel], prefix: str
) -> ConvertedPoint:
    samples: list[LegacySample] = []
    unmapped: list[str] = []
    for field, value in point.fields.items():
        channel = channels.get(field)
        if channel is None:
            unmapped.append(f"{prefix}:{field}")
        else:
            samples.append(_sample(point, channel, value))
    return ConvertedPoint(tuple(samples), tuple(unmapped))


def _sample(
    point: LinePoint, channel: LegacyChannel, value: float | int | bool | str
) -> LegacySample:
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        value = value * channel.factor + channel.offset
    return LegacySample(point.timestamp_ns, channel, value)


async def import_paths(
    *,
    paths: list[Path],
    dsn: str | None,
    vehicle_id: str,
    mapping: LegacyChannelMap,
    since_ns: int | None = None,
    until_ns: int | None = None,
    dry_run: bool = False,
    resume: bool = False,
    batch_rows: int = DEFAULT_BATCH_ROWS,
) -> ImportReport:
    """Stream one or more dumps, committing each checkpoint with its rows."""
    if not vehicle_id.strip():
        raise ValueError("vehicle_id must not be empty")
    if batch_rows < 1:
        raise ValueError("batch_rows must be at least 1")
    if since_ns is not None and until_ns is not None and since_ns > until_ns:
        raise ValueError("--since must not be later than --until")
    if not dry_run and not dsn:
        raise ValueError("a TimescaleDB DSN is required unless --dry-run is used")

    store = None if dry_run else TimescaleStore(str(dsn))
    if store is not None:
        await store.connect()
    total = ImportReport()
    try:
        for path in paths:
            report = await _import_path(
                path=Path(path),
                store=store,
                vehicle_id=vehicle_id,
                mapping=mapping,
                since_ns=since_ns,
                until_ns=until_ns,
                resume=resume,
                batch_rows=batch_rows,
            )
            total.merge(report)
    finally:
        if store is not None:
            await store.close()
    return total


async def _import_path(
    *,
    path: Path,
    store: TimescaleStore | None,
    vehicle_id: str,
    mapping: LegacyChannelMap,
    since_ns: int | None,
    until_ns: int | None,
    resume: bool,
    batch_rows: int,
) -> ImportReport:
    resolved = path.resolve(strict=True)
    stream = str(resolved)
    job = f"{stream}\0{since_ns}\0{until_ns}"
    consumer = f"legacy-{vehicle_id}-{hashlib.sha256(job.encode()).hexdigest()[:16]}"
    cursor = await store.read_cursor(consumer) if store is not None else 0
    report = ImportReport(files=1)
    if _is_lap_dump(resolved):
        return await _import_lap_dump(
            path=resolved,
            store=store,
            vehicle_id=vehicle_id,
            stream=stream,
            consumer=consumer,
            cursor=cursor,
            since_ns=since_ns,
            until_ns=until_ns,
            resume=resume,
            batch_rows=batch_rows,
            report=report,
        )
    channel_keys: dict[str, int] = {}
    rows: list[SampleRow] = []
    laps: list[LapRow] = []
    sectors: list[LegacySectorRow] = []
    pit_updates: list[PitStatusUpdate] = []
    lines_since_flush = 0
    last_offset = cursor

    with gzip.open(resolved, "rb") as handle:
        if resume and cursor:
            handle.seek(cursor)
            report.resumed_bytes += cursor
        while raw_line := handle.readline():
            offset = handle.tell()
            report.lines += 1
            lines_since_flush += 1
            last_offset = offset
            if not resume and offset <= cursor:
                continue
            try:
                point = parse_line_protocol(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                report.malformed += 1
                continue
            if (since_ns is not None and point.timestamp_ns < since_ns) or (
                until_ns is not None and point.timestamp_ns > until_ns
            ):
                report.filtered += 1
            elif point.measurement == "lap":
                converted_lap = convert_lap_point(point, vehicle_id)
                laps.extend(converted_lap.laps)
                sectors.extend(converted_lap.sectors)
                pit_updates.extend(converted_lap.pit_updates)
                report.laps += len(converted_lap.laps)
                report.sectors += len(converted_lap.sectors)
                report.pit_updates += len(converted_lap.pit_updates)
                if (
                    not converted_lap.laps
                    and not converted_lap.sectors
                    and not converted_lap.pit_updates
                ):
                    report.ignored += 1
            else:
                converted = convert_point(point, mapping)
                report.unmapped.update(converted.unmapped)
                if not converted.samples and not converted.unmapped:
                    report.ignored += 1
                for sample in converted.samples:
                    channel_key = channel_keys.get(sample.channel.name)
                    if channel_key is None and store is not None:
                        registered = await store.upsert_synthetic_channels(
                            vehicle_id,
                            (
                                SyntheticChannel(
                                    name=sample.channel.name,
                                    units=sample.channel.units,
                                    value_type=_VALUE_TYPES[sample.channel.value_type],
                                    source_ref=f"legacy:{sample.channel.name}",
                                ),
                            ),
                        )
                        channel_key = registered[sample.channel.name]
                        channel_keys.update(registered)
                    if store is not None:
                        assert channel_key is not None
                        rows.append(_sample_row(sample, channel_key))
                    report.samples += 1

            work = len(rows) + len(laps) + len(sectors) + len(pit_updates)
            if store is not None and (
                work >= batch_rows or lines_since_flush >= CHECKPOINT_LINE_INTERVAL
            ):
                await store.flush(
                    rows=rows,
                    laps=laps,
                    pit_updates=pit_updates,
                    consumer=consumer,
                    stream=stream,
                    stream_seq=last_offset,
                    legacy_sectors=sectors,
                )
                rows.clear()
                laps.clear()
                sectors.clear()
                pit_updates.clear()
                lines_since_flush = 0

    if store is not None:
        await store.flush(
            rows=rows,
            laps=laps,
            pit_updates=pit_updates,
            consumer=consumer,
            stream=stream,
            stream_seq=last_offset,
            legacy_sectors=sectors,
        )
    return report


def _is_lap_dump(path: Path) -> bool:
    with gzip.open(path, "rb") as handle:
        return handle.read(4) == b"lap,"


async def _import_lap_dump(
    *,
    path: Path,
    store: TimescaleStore | None,
    vehicle_id: str,
    stream: str,
    consumer: str,
    cursor: int,
    since_ns: int | None,
    until_ns: int | None,
    resume: bool,
    batch_rows: int,
    report: ImportReport,
) -> ImportReport:
    """Disk-sort crossing fields, then materialise them in bounded batches."""
    last_offset = cursor
    with tempfile.TemporaryDirectory(prefix="openlaps-legacy-laps-") as temporary:
        connection = sqlite3.connect(Path(temporary) / "crossings.sqlite3")
        try:
            connection.execute(
                "CREATE TABLE point_fields ("
                "timestamp_ns INTEGER NOT NULL, tags TEXT NOT NULL, "
                "field_name TEXT NOT NULL, field_value TEXT NOT NULL)"
            )
            with gzip.open(path, "rb") as handle:
                if resume and cursor:
                    handle.seek(cursor)
                    report.resumed_bytes += cursor
                while raw_line := handle.readline():
                    last_offset = handle.tell()
                    report.lines += 1
                    if not resume and last_offset <= cursor:
                        continue
                    try:
                        point = parse_line_protocol(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        report.malformed += 1
                        continue
                    if (since_ns is not None and point.timestamp_ns < since_ns) or (
                        until_ns is not None and point.timestamp_ns > until_ns
                    ):
                        report.filtered += 1
                        continue
                    tags = json.dumps(point.tags, sort_keys=True, separators=(",", ":"))
                    connection.executemany(
                        "INSERT INTO point_fields VALUES (?, ?, ?, ?)",
                        (
                            (point.timestamp_ns, tags, name, json.dumps(value))
                            for name, value in point.fields.items()
                        ),
                    )
            connection.execute(
                "CREATE INDEX point_fields_order ON point_fields (timestamp_ns, tags)"
            )
            connection.commit()

            laps: list[LapRow] = []
            sectors: list[LegacySectorRow] = []
            pit_updates: list[PitStatusUpdate] = []
            for row in _iter_legacy_lap_rows(_ordered_lap_points(connection), vehicle_id):
                if isinstance(row, LapRow):
                    laps.append(row)
                    report.laps += 1
                elif isinstance(row, LegacySectorRow):
                    sectors.append(row)
                    report.sectors += 1
                else:
                    pit_updates.append(row)
                    report.pit_updates += 1
                if len(laps) + len(sectors) + len(pit_updates) >= batch_rows:
                    if store is not None:
                        await store.flush(
                            rows=(),
                            laps=laps,
                            pit_updates=pit_updates,
                            consumer=consumer,
                            stream=stream,
                            stream_seq=0,
                            legacy_sectors=sectors,
                        )
                    laps.clear()
                    sectors.clear()
                    pit_updates.clear()
            if store is not None:
                await store.flush(
                    rows=(),
                    laps=laps,
                    pit_updates=pit_updates,
                    consumer=consumer,
                    stream=stream,
                    stream_seq=last_offset,
                    legacy_sectors=sectors,
                )
        finally:
            connection.close()
    return report


def _ordered_lap_points(connection: sqlite3.Connection) -> Iterator[LinePoint]:
    """Merge field rows into timestamp-ordered points from the disk-backed sort."""
    current_key: tuple[int, str] | None = None
    fields: dict[str, float | int | bool | str] = {}
    for timestamp_ns, tags_json, field_name, field_json in connection.execute(
        "SELECT timestamp_ns, tags, field_name, field_value "
        "FROM point_fields ORDER BY timestamp_ns, tags"
    ):
        key = (int(timestamp_ns), str(tags_json))
        if current_key is not None and key != current_key:
            yield LinePoint("lap", json.loads(current_key[1]), fields, current_key[0])
            fields = {}
        current_key = key
        fields[str(field_name)] = json.loads(field_json)
    if current_key is not None:
        yield LinePoint("lap", json.loads(current_key[1]), fields, current_key[0])


def _sample_row(sample: LegacySample, channel_key: int) -> SampleRow:
    if isinstance(sample.value, str):
        return (_timestamp(sample.timestamp_ns), channel_key, None, sample.value)
    return (_timestamp(sample.timestamp_ns), channel_key, float(sample.value), None)


def parse_time_bound(value: str) -> int:
    """Parse unix seconds or an ISO-8601 instant to nanoseconds."""
    try:
        return round(float(value) * 1_000_000_000)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid time {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return round(parsed.timestamp() * 1_000_000_000)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import legacy telemetry into TimescaleDB")
    parser.add_argument("paths", nargs="*", type=Path, help=".lp.gz dumps (defaults to June 2025)")
    parser.add_argument("--vehicle", required=True, help="vehicle id for imported rows")
    parser.add_argument("--map", type=Path, default=DEFAULT_MAP, help="legacy mapping YAML")
    parser.add_argument("--since", type=parse_time_bound, help="inclusive ISO time or unix seconds")
    parser.add_argument("--until", type=parse_time_bound, help="inclusive ISO time or unix seconds")
    parser.add_argument(
        "--dry-run", action="store_true", help="parse and report without database writes"
    )
    parser.add_argument(
        "--resume", action="store_true", help="seek directly to the last committed file offset"
    )
    parser.add_argument(
        "--batch-rows", type=int, default=DEFAULT_BATCH_ROWS, help=argparse.SUPPRESS
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    paths = args.paths or list(DEFAULT_INPUTS)
    try:
        mapping = load_channel_map(args.map)
        dsn = None if args.dry_run else dsn_from_env()
        report = asyncio.run(
            import_paths(
                paths=paths,
                dsn=dsn,
                vehicle_id=args.vehicle,
                mapping=mapping,
                since_ns=args.since,
                until_ns=args.until,
                dry_run=args.dry_run,
                resume=args.resume,
                batch_rows=args.batch_rows,
            )
        )
    except (OSError, ValueError) as exc:
        print(f"import_legacy: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


def parse_line_protocol(line: str) -> LinePoint:
    """Parse the subset of InfluxDB line protocol used by the legacy dumps."""
    text = line.rstrip("\r\n")
    # The real exports have one field and no escaping on >99.99% of their
    # 114 million lines. Preserve the complete parser below for legal escaped
    # input, but do not pay a character-by-character state machine cost on
    # every ordinary numeric row.
    simple = "\\" not in text and text.count(" ") == 2
    if simple:
        series, field_text, timestamp_text = text.split(" ")
        series_parts = series.split(",")
        raw_fields = field_text.split(",")
    else:
        series, field_text, timestamp_text = _split_sections(text)
        series_parts = _split_unescaped(series, ",")
        raw_fields = _split_unescaped(field_text, ",", quoted=True)
    measurement = series_parts[0] if simple else _unescape(series_parts[0])
    tags: dict[str, str] = {}
    for raw_tag in series_parts[1:]:
        key, value = raw_tag.split("=", 1) if simple else _split_pair(raw_tag)
        tags[key if simple else _unescape(key)] = value if simple else _unescape(value)

    fields: dict[str, float | int | bool | str] = {}
    for raw_field in raw_fields:
        key, value = raw_field.split("=", 1) if simple else _split_pair(raw_field)
        fields[key if simple else _unescape(key)] = _parse_field(value)
    try:
        timestamp_ns = int(timestamp_text)
    except ValueError as exc:
        raise ValueError(f"invalid line-protocol timestamp {timestamp_text!r}") from exc
    return LinePoint(measurement, tags, fields, timestamp_ns)


def _split_sections(text: str) -> tuple[str, str, str]:
    sections = _split_unescaped(text, " ", quoted=True, maxsplit=2)
    if len(sections) != 3:
        raise ValueError("line protocol requires series, fields, and timestamp")
    return sections[0], sections[1], sections[2]


def _split_pair(text: str) -> tuple[str, str]:
    parts = _split_unescaped(text, "=", quoted=True, maxsplit=1)
    if len(parts) != 2 or not parts[0]:
        raise ValueError(f"invalid line-protocol key/value pair {text!r}")
    return parts[0], parts[1]


def _split_unescaped(
    text: str, separator: str, *, quoted: bool = False, maxsplit: int = -1
) -> list[str]:
    parts: list[str] = []
    start = 0
    escaped = False
    in_quotes = False
    splits = 0
    for index, character in enumerate(text):
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if quoted and character == '"':
            in_quotes = not in_quotes
            continue
        if character == separator and not in_quotes and (maxsplit < 0 or splits < maxsplit):
            parts.append(text[start:index])
            start = index + 1
            splits += 1
    if escaped or in_quotes:
        raise ValueError("unterminated escape or quoted field")
    parts.append(text[start:])
    return parts


def _unescape(text: str) -> str:
    output: list[str] = []
    escaped = False
    for character in text:
        if escaped:
            output.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        else:
            output.append(character)
    if escaped:
        raise ValueError("unterminated escape")
    return "".join(output)


def _parse_field(text: str) -> float | int | bool | str:
    if text.startswith('"'):
        if not text.endswith('"') or len(text) < 2:
            raise ValueError("unterminated string field")
        return _unescape(text[1:-1])
    lowered = text.lower()
    if lowered in {"true", "t"}:
        return True
    if lowered in {"false", "f"}:
        return False
    if text.endswith(("i", "u")):
        try:
            return int(text[:-1])
        except ValueError as exc:
            raise ValueError(f"invalid integer field {text!r}") from exc
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"invalid field value {text!r}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
