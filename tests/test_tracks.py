"""Tests for pure KML and JSON-sidecar track definition loading."""

import xml.etree.ElementTree as ET
from pathlib import Path

from timing.timing_core import EventType, GPSPoint, LineType, TimingEngine, classify_line
from timing.tracks import load_track, load_tracks, parse_kml_file

ROOT = Path(__file__).resolve().parents[1]
TRACKS_DIR = ROOT / "profiles" / "example-club-racer" / "tracks"
PIT_LINE_NAMES = {
    "PitEntryRefuel",
    "PitExitRefuel",
    "PitEntryService",
    "PitExitService",
}

# Real StartFinish line from Wanneroo.kml (lon,lat pairs)
SF_LON_MID = (115.7863013154122 + 115.7864928902992) / 2
SF_LAT = -31.66416612312083  # ~ line latitude


def test_wanneroo_loads_and_classifies():
    tracks = load_tracks(TRACKS_DIR)
    track = tracks["Wanneroo"]
    line_types = {line.line_type for line in track.lines}

    assert LineType.START_FINISH in line_types
    assert sum(line.line_type == LineType.SECTOR for line in track.lines) == 2
    assert {LineType.PIT_ENTRY, LineType.PIT_EXIT} <= line_types


def test_every_shipped_kml_placemark_has_a_known_line_type():
    kml_paths = sorted((ROOT / "profiles").glob("*/tracks/*.kml"))

    assert kml_paths
    for kml_path in kml_paths:
        root = ET.parse(kml_path).getroot()
        names = [
            name.text.strip()
            for name in root.findall(
                ".//{http://www.opengis.net/kml/2.2}Placemark/{http://www.opengis.net/kml/2.2}name"
            )
            if name.text
        ]
        assert names, kml_path
        unknown = [name for name in names if classify_line(name) == LineType.UNKNOWN]
        assert unknown == [], f"{kml_path}: unclassified placemarks: {unknown}"


def test_wanneroo_has_distinct_refuel_and_service_pit_lines():
    for kml_path in sorted((ROOT / "profiles").glob("*/tracks/Wanneroo.kml")):
        pit_lines = {
            line.name
            for line in parse_kml_file(kml_path)
            if line.line_type in {LineType.PIT_ENTRY, LineType.PIT_EXIT}
        }
        assert pit_lines == PIT_LINE_NAMES


def test_real_wanneroo_pit_crossings_preserve_the_exact_line_names():
    track = load_track(TRACKS_DIR / "Wanneroo.kml")
    expected_types = {
        "PitEntryRefuel": EventType.PIT_ENTRY,
        "PitExitRefuel": EventType.PIT_EXIT,
        "PitEntryService": EventType.PIT_ENTRY,
        "PitExitService": EventType.PIT_EXIT,
    }

    for line in track.lines:
        if line.name not in expected_types:
            continue
        # Cross the real KML segment at its midpoint, perpendicular to it.
        dlat = line.end.lat - line.start.lat
        dlon = line.end.lon - line.start.lon
        mid_lat = (line.start.lat + line.end.lat) / 2
        mid_lon = (line.start.lon + line.end.lon) / 2
        before = GPSPoint(mid_lat - dlon, mid_lon + dlat, 100.0)
        after = GPSPoint(mid_lat + dlon, mid_lon - dlat, 101.0)
        engine = TimingEngine([line])

        engine.process_point(before)
        events = engine.process_point(after)

        assert [(event.type, event.line) for event in events] == [
            (expected_types[line.name], line.name)
        ]


def test_wanneroo_sidecar_loads():
    track = load_track(TRACKS_DIR / "Wanneroo.kml")

    assert track.name == "Wanneroo"
    assert track.length_m == 2411
    assert track.mini_sectors == 20


def test_real_startfinish_crossing_starts_lap():
    track = load_track(TRACKS_DIR / "Wanneroo.kml")
    engine = TimingEngine(track.lines)
    south = GPSPoint(lat=SF_LAT - 0.0002, lon=SF_LON_MID, timestamp=100.0)
    north = GPSPoint(lat=SF_LAT + 0.0002, lon=SF_LON_MID, timestamp=100.1)

    engine.process_point(south)
    engine.process_point(north)

    assert engine.state.lap_number == 1


def test_malformed_sidecar_uses_predecessor_defaults(tmp_path):
    kml_path = tmp_path / "BrokenMeta.kml"
    kml_path.write_text((TRACKS_DIR / "Wanneroo.kml").read_text())
    kml_path.with_suffix(".json").write_text("{not valid json")

    track = load_track(kml_path)

    assert track.length_m == 0.0
    assert track.mini_sectors == 20


def test_load_tracks_skips_malformed_kml_like_predecessor(tmp_path):
    (tmp_path / "Broken.kml").write_text("not XML")
    valid_path = tmp_path / "Wanneroo.kml"
    valid_path.write_text((TRACKS_DIR / "Wanneroo.kml").read_text())

    tracks = load_tracks(tmp_path)

    assert set(tracks) == {"Wanneroo"}
