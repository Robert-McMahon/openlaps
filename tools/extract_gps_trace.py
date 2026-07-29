#!/usr/bin/env python3
"""Extract a `--gps-trace` CSV from the predecessor's InfluxDB `gps.lp` dump.

`tools/replay.py --gps-trace` takes a ``t_s,lat,lon,speed_kmh,heading_deg``
CSV and encodes each row to a synthetic RMC sentence through the real serial
collector (``tests/fixtures/gps/README.md``). That fixture is a 110 s slice
cut by hand; P4.6 needs the *whole* June-2025 event at native rate, so this
is the extraction as a repeatable tool rather than a one-off.

The line-protocol reader is `import_legacy.parse_line_protocol` -- the same
parser P3.8's importer streams 114 million lines through, rather than a
second parser that would drift from it.

**One point of care: the dump is field-major, not point-major.** The export
writes every `heading` line, then every `lat`, then every `lon`, then every
`speed` -- one field per line, four lines per fix, each block in timestamp
order. A fix is only whole once all four are in hand, so this reads the file
once into per-field columns and merges them on the timestamp afterwards. A
timestamp missing any field is not a fix and is counted, not guessed at.

Speed in the dump is knots (`pyubx2`'s native RMC unit); it converts to km/h
here so the CSV can be dropped straight into `rmc_sentence`, which expects
km/h and converts back. `t_s` is seconds from the first whole fix -- the
absolute instant that zero corresponds to is reported as `epoch_unix_s` and
belongs in the run manifest, because nothing downstream can recover it.

    uv run tools/extract_gps_trace.py --out /var/tmp/june2025-gps.csv
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from array import array
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from import_legacy import parse_line_protocol, parse_time_bound  # noqa: E402

DEFAULT_INPUT = Path("/mnt/data/logger/backups/backup_migration_tmp/gps.lp.gz")
FIELDS = ("lat", "lon", "speed", "heading")
KNOTS_TO_KMH = 1.852

_Fix = tuple[int, tuple[float, ...]]

# 12 significant digits is ~1e-10 degrees, about 0.01 mm -- four orders of
# magnitude finer than the 4-decimal arc-minutes (~0.19 m) `rmc_sentence` is
# about to quantise these to. Shortest-round-trip repr would be exact and
# would add ~35 MB to the event's trace for no reachable precision.
_COORD_FORMAT = "{:.12g}"
_T_FORMAT = "{:.9f}"


class _Column:
    """One field's (timestamp, value) column, kept in timestamp order."""

    __slots__ = ("_monotonic", "times", "values")

    def __init__(self) -> None:
        self.times = array("q")
        self.values = array("d")
        self._monotonic = True

    def append(self, timestamp_ns: int, value: float) -> None:
        if self.times and timestamp_ns < self.times[-1]:
            self._monotonic = False
        self.times.append(timestamp_ns)
        self.values.append(value)

    def ordered(self) -> tuple[array, array]:
        """The column in timestamp order, sorting only if the dump was not."""
        if self._monotonic:
            return self.times, self.values
        order = sorted(range(len(self.times)), key=self.times.__getitem__)
        return (
            array("q", (self.times[index] for index in order)),
            array("d", (self.values[index] for index in order)),
        )


def read_columns(
    path: Path, *, since_ns: int | None = None, until_ns: int | None = None
) -> tuple[dict[str, _Column], dict[str, int]]:
    """Stream one `gps.lp.gz`, returning a column per field plus line counters."""
    columns = {name: _Column() for name in FIELDS}
    counts = {"lines": 0, "filtered": 0, "malformed": 0, "ignored": 0, "partial_fixes": 0}
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as handle:
        while raw_line := handle.readline():
            counts["lines"] += 1
            try:
                point = parse_line_protocol(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                counts["malformed"] += 1
                continue
            if (since_ns is not None and point.timestamp_ns < since_ns) or (
                until_ns is not None and point.timestamp_ns > until_ns
            ):
                counts["filtered"] += 1
                continue
            if point.measurement != "gps":
                counts["ignored"] += 1
                continue
            recognised = False
            for name, value in point.fields.items():
                column = columns.get(name)
                if column is None or isinstance(value, (bool, str)):
                    continue
                column.append(point.timestamp_ns, float(value))
                recognised = True
            if not recognised:
                counts["ignored"] += 1
    return columns, counts


def merge_fixes(columns: dict[str, _Column], counts: dict[str, int]) -> Iterator[_Fix]:
    """Yield ``(timestamp_ns, values)`` for every timestamp carrying all four fields.

    A four-way merge rather than a dictionary: the columns are already in
    timestamp order and each holds ~1.8 M points, so walking them in step
    costs four indices instead of a hash entry per fix. Timestamps that
    carry only some of the fields are counted as partial, never guessed at.
    """
    ordered = {name: columns[name].ordered() for name in FIELDS}
    indices = dict.fromkeys(FIELDS, 0)
    lengths = {name: len(ordered[name][0]) for name in FIELDS}
    while all(indices[name] < lengths[name] for name in FIELDS):
        stamps = [ordered[name][0][indices[name]] for name in FIELDS]
        newest = max(stamps)
        if any(stamp != newest for stamp in stamps):
            # Advance only the laggards; a field with no point at this
            # instant simply means the instant is not a whole fix.
            for name, stamp in zip(FIELDS, stamps, strict=True):
                if stamp < newest:
                    indices[name] += 1
                    counts["partial_fixes"] += 1
            continue
        yield newest, tuple(ordered[name][1][indices[name]] for name in FIELDS)
        for name in FIELDS:
            indices[name] += 1
    for name in FIELDS:
        counts["partial_fixes"] += lengths[name] - indices[name]


def _encodable(lat: float, lon: float, speed_kmh: float, heading_deg: float) -> bool:
    """Whether `rmc_sentence` can encode this fix and the NMEA decoder accept it."""
    return (
        -90.0 <= lat <= 90.0
        and -180.0 <= lon <= 180.0
        and speed_kmh >= 0.0
        and 0.0 <= heading_deg < 360.0
    )


def write_trace(
    columns: dict[str, _Column],
    handle,
    counts: dict[str, int],
    *,
    epoch_ns: int | None = None,
) -> dict[str, object]:
    """Write the `t_s,lat,lon,speed_kmh,heading_deg` rows; report what was written."""
    handle.write("t_s,lat,lon,speed_kmh,heading_deg\n")
    origin_ns = epoch_ns
    written = 0
    normalised = 0
    rejected = 0
    last_ns = 0
    for timestamp_ns, (lat, lon, speed_kn, heading) in merge_fixes(columns, counts):
        speed_kmh = speed_kn * KNOTS_TO_KMH
        # The dump carries 956 headings of exactly 360.0, which the NMEA
        # decoder rejects (it requires 0 <= heading < 360). They are real
        # fixes the predecessor timed against, and heading plays no part in
        # line crossing at all, so they are wrapped rather than dropped —
        # losing 956 position samples to a units convention would be a
        # fidelity loss with nothing to show for it.
        if heading >= 360.0 or heading < 0.0:
            heading %= 360.0
            normalised += 1
        if not _encodable(lat, lon, speed_kmh, heading):
            rejected += 1
            continue
        if origin_ns is None:
            origin_ns = timestamp_ns
        t_s = (timestamp_ns - origin_ns) / 1e9
        handle.write(
            f"{_T_FORMAT.format(t_s)},"
            f"{_COORD_FORMAT.format(lat)},{_COORD_FORMAT.format(lon)},"
            f"{_COORD_FORMAT.format(speed_kmh)},{_COORD_FORMAT.format(heading)}\n"
        )
        written += 1
        last_ns = timestamp_ns
    duration_s = (last_ns - origin_ns) / 1e9 if written and origin_ns is not None else 0.0
    return {
        "fixes": written,
        "normalised_headings": normalised,
        "rejected_fixes": rejected,
        "epoch_unix_s": None if origin_ns is None else origin_ns / 1e9,
        "duration_s": duration_s,
        "mean_rate_hz": written / duration_s if duration_s > 0 else 0.0,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "path", nargs="?", type=Path, default=DEFAULT_INPUT, help="gps line-protocol dump"
    )
    parser.add_argument("--out", type=Path, required=True, help="destination CSV ('-' for stdout)")
    parser.add_argument("--since", type=parse_time_bound, help="inclusive ISO time or unix seconds")
    parser.add_argument("--until", type=parse_time_bound, help="inclusive ISO time or unix seconds")
    parser.add_argument(
        "--epoch",
        type=parse_time_bound,
        help="anchor t_s=0 here instead of at the first fix (ISO time or unix seconds)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        columns, counts = read_columns(args.path, since_ns=args.since, until_ns=args.until)
        if str(args.out) == "-":
            report = write_trace(columns, sys.stdout, counts, epoch_ns=args.epoch)
        else:
            with args.out.open("w", newline="") as handle:
                report = write_trace(columns, handle, counts, epoch_ns=args.epoch)
    except OSError as exc:
        print(f"extract_gps_trace: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"source": str(args.path), **counts, **report}, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
