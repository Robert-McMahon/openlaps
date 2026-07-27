"""Tests for pure KML and JSON-sidecar track definition loading."""

from pathlib import Path

from timing.timing_core import GPSPoint, LineType, TimingEngine
from timing.tracks import load_track, load_tracks

TRACKS_DIR = Path(__file__).resolve().parents[1] / "profiles" / "example-club-racer" / "tracks"

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
