"""tools/track_sections.py: i2 track files, JSON sidecars and the section view."""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import track_sections  # noqa: E402

TRACKS = Path(__file__).parents[1] / "profiles/example-club-racer/tracks"


def _key(name: str) -> bytes:
    return struct.pack("<I", len(name)) + name.encode("ascii")


def _text(value: str) -> bytes:
    raw = value.encode("utf-16le")
    return struct.pack("<I", len(raw)) + raw


def _mt2(markers: dict[str, float], sections: list[tuple[str, str, str, str]]) -> bytes:
    """The parts of a MoTeCTrackV2 file the parser reads, in file order."""
    out = _key("MoTeCTrackV2") + struct.pack("<I", 8)
    out += _key("Dist") + struct.pack("<I", 4) + struct.pack("<f", 2553.37)
    out += _key("Name") + _text("Somewhere")
    # The outline: binary the parser must step over, not into.
    out += _key("Data") + struct.pack("<I", 12) + bytes(range(12))
    out += _key("SectionGroups") + struct.pack("<I", 1)
    for name, distance in markers.items():
        out += _key("Marker") + struct.pack("<I", 2)
        out += _key("Dist") + struct.pack("<I", 8) + struct.pack("<d", distance)
        out += _key("Name") + _text(name) + bytes(4)
    for first, second, name, kind in sections:
        out += _key("Section") + struct.pack("<I", 3)
        out += _key("Marker1") + _text(first) + _key("Marker2") + _text(second)
        out += _key("Name") + _text(name) + struct.pack("<I", 1)
        out += _key("SectionTypes") + struct.pack("<II", 0, 1)
        out += _key("SectionType") + struct.pack("<I", 1) + _key("Name") + _text(kind) + bytes(4)
    return out


def test_an_i2_track_file_yields_sections_in_lap_order_with_the_map_length() -> None:
    data = _mt2(
        {"Marker (0)": 59.0, "Marker (12)": 175.0, "Marker (1)": 304.0, "Marker (11)": 2410.0},
        [
            ("Marker (11)", "Marker (0)", "Str 0-1", "Straights"),
            ("Marker (12)", "Marker (1)", "T2", "Corners"),
            ("Marker (0)", "Marker (12)", "T1", "Corners"),
        ],
    )

    lap_length, sections = track_sections.parse_mt2(data)

    assert lap_length == 2553.4
    assert sections == [
        {"name": "T1", "kind": "corner", "start_m": 59.0, "end_m": 175.0},
        {"name": "T2", "kind": "corner", "start_m": 175.0, "end_m": 304.0},
        # Spans the start line: it ends before it begins.
        {"name": "Str 0-1", "kind": "straight", "start_m": 2410.0, "end_m": 59.0},
    ]


def test_files_that_are_not_marked_up_track_maps_are_refused() -> None:
    with pytest.raises(ValueError, match="MoTeCTrackV2"):
        track_sections.parse_mt2(b"not a track file at all")
    with pytest.raises(ValueError, match="section groups"):
        track_sections.parse_mt2(_key("MoTeCTrackV2") + _key("Dist") + bytes(8))


def test_from_mt2_keeps_what_the_sidecar_already_says(tmp_path, capsys) -> None:
    mt2 = tmp_path / "track.mt2"
    mt2.write_bytes(_mt2({"A": 10.0, "B": 90.0}, [("A", "B", "T1", "Corners")]))
    sidecar = tmp_path / "Track.json"
    sidecar.write_text(json.dumps({"length_m": 2550, "mini_sectors": 20}))

    assert track_sections.main(["from-mt2", str(mt2), str(sidecar)]) == 0

    document = json.loads(sidecar.read_text())
    assert document["length_m"] == 2550 and document["mini_sectors"] == 20
    assert document["sections_length_m"] == 2553.4
    assert document["sections"] == [
        {"name": "T1", "kind": "corner", "start_m": 10.0, "end_m": 90.0}
    ]
    assert "1 corners, 0 straights" in capsys.readouterr().out


def test_a_sidecar_with_a_section_outside_the_lap_is_refused(tmp_path) -> None:
    sidecar = tmp_path / "Track.json"
    sidecar.write_text(
        json.dumps(
            {
                "sections_length_m": 1000,
                "sections": [{"name": "T1", "kind": "corner", "start_m": 900, "end_m": 1100}],
            }
        )
    )
    with pytest.raises(ValueError, match="outside the lap"):
        track_sections.read_sidecar(sidecar)


def test_the_shipped_collie_sidecar_still_loads_as_a_track_and_names_thirteen_corners() -> None:
    from timing.tracks import load_track

    track = load_track(TRACKS / "Collie-Long-BEERS.kml")
    assert track.length_m == 2550 and track.mini_sectors == 20

    name, lap_length, sections = track_sections.read_sidecar(TRACKS / "Collie-Long-BEERS.json")
    assert name == "Collie-Long-BEERS"
    assert lap_length == 2553.4
    corners = [section["name"] for section in sections if section["kind"] == "corner"]
    assert corners == [f"T{number}" for number in range(1, 14)]
    # Sections tile the lap: each begins where the one before it ended.
    for before, after in zip(sections, sections[1:] + sections[:1], strict=True):
        assert before["end_m"] == after["start_m"]


def test_load_replaces_a_tracks_sections_and_the_view_gives_lap_fractions(
    timescale_dsn, tmp_path
) -> None:
    import psycopg

    from pit.db.migrate import apply_migrations

    with psycopg.connect(timescale_dsn) as conn:
        apply_migrations(conn)
    sidecar = tmp_path / "Track.json"
    for sections in (
        [{"name": "Old", "kind": "corner", "start_m": 0, "end_m": 100}] * 2,
        [
            {"name": "T1", "kind": "corner", "start_m": 100, "end_m": 250},
            {"name": "Back", "kind": "straight", "start_m": 250, "end_m": 100},
        ],
    ):
        sidecar.write_text(json.dumps({"sections_length_m": 1000, "sections": sections}))
        assert track_sections.main(["load", str(sidecar), "--dsn", timescale_dsn]) == 0

    with psycopg.connect(timescale_dsn) as conn:
        rows = conn.execute(
            "SELECT track_name, seq, name, kind, start_fraction, end_fraction "
            "FROM v_track_sections ORDER BY seq"
        ).fetchall()
    assert rows == [
        ("Track", 1, "T1", "corner", 0.1, 0.25),
        ("Track", 2, "Back", "straight", 0.25, 0.1),
    ]
