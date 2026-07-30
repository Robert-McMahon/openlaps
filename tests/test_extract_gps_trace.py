"""tools/extract_gps_trace.py: the field-major dump becomes a replayable trace.

The dump this reads is 7 M lines of one field each, grouped by field name --
so "did the merge put the right lat with the right lon" is not a formality,
and neither is what happens to a timestamp that is missing one of the four.
Everything here runs against a synthetic dump of the same shape; the real one
is 83 MB and lives outside the repository.
"""

from __future__ import annotations

import csv
import gzip
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import extract_gps_trace as extract  # noqa: E402
from _bench import rmc_sentence  # noqa: E402

from collectors.serial.nmea import NmeaDecoder  # noqa: E402

BASE_NS = 1_749_784_529_601_615_234
SERIES = "gps,topic=telemetry/gps/data,track_name=Wanneroo"


def _write_dump(path: Path, points: list[tuple[int, dict[str, float]]]) -> Path:
    """Write a field-major dump: every `lat` line, then every `lon`, and so on."""
    with gzip.open(path, "wt") as handle:
        for field in ("heading", "lat", "lon", "speed"):
            for timestamp_ns, fields in points:
                if field in fields:
                    handle.write(f"{SERIES} {field}={fields[field]} {timestamp_ns}\n")
    return path


def _fix(index: int, **overrides: float) -> tuple[int, dict[str, float]]:
    fields = {
        "lat": -31.6636 - index * 1e-5,
        "lon": 115.7867 + index * 1e-5,
        "speed": 50.0 + index,
        "heading": 120.0 + index,
    }
    fields.update(overrides)
    return BASE_NS + index * 50_000_000, fields


def _extract(path: Path, **kwargs) -> tuple[list[dict[str, str]], dict, dict]:
    columns, counts = extract.read_columns(path, **kwargs)
    buffer = io.StringIO()
    report = extract.write_trace(columns, buffer, counts)
    buffer.seek(0)
    return list(csv.DictReader(buffer)), report, counts


def test_field_major_dump_merges_into_whole_fixes(tmp_path: Path):
    path = _write_dump(tmp_path / "gps.lp.gz", [_fix(index) for index in range(4)])

    rows, report, counts = _extract(path)

    assert report["fixes"] == 4
    assert counts["partial_fixes"] == 0
    assert report["epoch_unix_s"] == BASE_NS / 1e9
    assert [row["t_s"] for row in rows] == [
        "0.000000000",
        "0.050000000",
        "0.100000000",
        "0.150000000",
    ]
    # Each row must carry *its own* fix's fields, not a neighbour's: the four
    # values arrive 1.8 M lines apart in the real dump.
    for index, row in enumerate(rows):
        assert abs(float(row["lat"]) - (-31.6636 - index * 1e-5)) < 1e-9
        assert abs(float(row["lon"]) - (115.7867 + index * 1e-5)) < 1e-9
    assert [float(row["heading_deg"]) for row in rows] == [120.0 + index for index in range(4)]


def test_speed_converts_from_knots_to_kmh(tmp_path: Path):
    path = _write_dump(tmp_path / "gps.lp.gz", [_fix(0, speed=10.0)])

    rows, _, _ = _extract(path)

    assert float(rows[0]["speed_kmh"]) == 10.0 * 1.852


def test_heading_of_exactly_360_is_wrapped_not_dropped(tmp_path: Path):
    """The real dump holds 956 of these, and they are ordinary fixes."""
    path = _write_dump(tmp_path / "gps.lp.gz", [_fix(0), _fix(1, heading=360.0), _fix(2)])

    rows, report, _ = _extract(path)

    assert report["fixes"] == 3
    assert report["normalised_headings"] == 1
    assert report["rejected_fixes"] == 0
    assert float(rows[1]["heading_deg"]) == 0.0


def test_a_timestamp_missing_a_field_is_counted_not_guessed(tmp_path: Path):
    incomplete = _fix(1)
    del incomplete[1]["lon"]
    path = _write_dump(tmp_path / "gps.lp.gz", [_fix(0), incomplete, _fix(2)])

    rows, report, counts = _extract(path)

    assert report["fixes"] == 2
    assert counts["partial_fixes"] == 3
    assert [row["t_s"] for row in rows] == ["0.000000000", "0.100000000"]


def test_unencodable_values_are_rejected_rather_than_written(tmp_path: Path):
    path = _write_dump(tmp_path / "gps.lp.gz", [_fix(0), _fix(1, lat=91.0), _fix(2)])

    rows, report, _ = _extract(path)

    assert report["fixes"] == 2
    assert report["rejected_fixes"] == 1
    assert all(-90.0 <= float(row["lat"]) <= 90.0 for row in rows)


def test_time_bounds_filter_before_merging(tmp_path: Path):
    path = _write_dump(tmp_path / "gps.lp.gz", [_fix(index) for index in range(6)])

    rows, report, counts = _extract(
        path, since_ns=BASE_NS + 100_000_000, until_ns=BASE_NS + 200_000_000
    )

    assert report["fixes"] == 3
    assert counts["filtered"] == 12
    # t_s re-anchors on the first fix that survived the filter.
    assert rows[0]["t_s"] == "0.000000000"


def test_unsorted_columns_are_sorted_before_merging(tmp_path: Path):
    out_of_order = [_fix(2), _fix(0), _fix(1)]
    path = _write_dump(tmp_path / "gps.lp.gz", out_of_order)

    rows, report, _ = _extract(path)

    assert report["fixes"] == 3
    assert [row["t_s"] for row in rows] == ["0.000000000", "0.050000000", "0.100000000"]


def test_rows_survive_the_rmc_round_trip_the_replay_puts_them_through(tmp_path: Path):
    """A trace this tool writes has to decode back through the real NMEA decoder."""
    path = _write_dump(tmp_path / "gps.lp.gz", [_fix(index) for index in range(3)])
    rows, _, _ = _extract(path)
    decoder = NmeaDecoder("serial0", "um980")

    for row in rows:
        values = dict(
            decoder.decode(
                rmc_sentence(
                    float(row["lat"]),
                    float(row["lon"]),
                    float(row["speed_kmh"]),
                    float(row["heading_deg"]),
                )
            )
        )
        assert values, "every extracted fix must decode as an active RMC"
        assert values["serial0:um980.RMC.lat"] == pytest.approx(float(row["lat"]), abs=1e-11)
        assert values["serial0:um980.RMC.lon"] == pytest.approx(float(row["lon"]), abs=1e-11)


def test_the_rmc_round_trip_adds_no_position_error_of_its_own():
    """P4.6's parity figure is only as good as this encoder is transparent.

    `rmc_sentence` used to write 4 decimal places of arc-minutes -- 0.185 m in
    latitude -- and that rounding turned out to be essentially the whole of
    openlaps' disagreement with the predecessor's replay of the same event
    (`docs/bench/timing-parity.md`). At `COORD_DECIMALS` the encode/decode
    pair is transparent to float64, so what the parity run measures is the
    timing engine rather than a format string.
    """
    decoder = NmeaDecoder("serial0", "um980")
    # Wanneroo, and the awkward hemispheres: negative latitude and a longitude
    # needing all three degree digits are exactly what the event uses.
    for lat, lon in (
        (-31.6636216573, 115.786789209),
        (-31.66365182734, 115.78679120983),
        (31.6636216573, -115.786789209),
        (0.0, 0.0),
    ):
        values = dict(decoder.decode(rmc_sentence(lat, lon, 97.96, 148.7)))

        assert values["serial0:um980.RMC.lat"] == pytest.approx(lat, abs=1e-11)
        assert values["serial0:um980.RMC.lon"] == pytest.approx(lon, abs=1e-11)


def test_the_widened_sentence_still_fits_the_nmea_length_limit():
    """82 bytes including CRLF, and the widened fields land exactly on it.

    Nothing in this repository enforces the limit -- these sentences are built
    and consumed in-process by `NmeaDecoder` -- but sitting one byte inside a
    real standard's ceiling is worth knowing before someone adds a decimal or
    a field and puts it outside.
    """
    fastest = rmc_sentence(-31.66398764417, 115.78895628766, 210.0, 269.9)

    assert len(fastest) <= 82
