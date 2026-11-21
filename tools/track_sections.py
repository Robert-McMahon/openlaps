#!/usr/bin/env python3
"""Named lap sections (corners, straights) for the driver dashboard.

The car times lines and measures distance; it has no idea where "T6" is.
That comes from a track map someone has already marked up. Two steps:

    # 1. Copy the sections out of a MoTeC i2 track file into the track's
    #    JSON sidecar (the file the agent already reads for length_m).
    uv run tools/track_sections.py from-mt2 Collie-Long-BEERS.mt2 \\
        profiles/example-club-racer/tracks/Collie-Long-BEERS.json

    # 2. Load one or more sidecars into the pit database (TIMESCALE_* or
    #    --dsn), replacing that track's sections.
    uv run tools/track_sections.py load \\
        profiles/example-club-racer/tracks/Collie-Long-BEERS.json

A sidecar's sections can equally be typed by hand:

    "sections_length_m": 2553.4,
    "sections": [{"name": "T1", "kind": "corner", "start_m": 59, "end_m": 175}, ...]

**The i2 file.** `.mt2` ("MoTeCTrackV2") is binary but plain: every key is a
u32 length and ASCII bytes, every value a u32 length and its bytes. Names are
UTF-16LE and distances float64. A section group holds `Marker`s (a `Dist`
and a `Name`) and `Section`s (`Marker1`, `Marker2`, a `Name` and a
`SectionType` whose name is "Corners" or "Straights"). This reads those and
the track's own `Dist` (its lap length, a float32) and ignores the rest --
the outline points are not needed.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path

_KINDS = {"Corners": "corner", "Straights": "straight"}


def _value(data: bytes, offset: int) -> tuple[bytes, int]:
    """The length-prefixed value at ``offset`` and the offset after it."""
    (length,) = struct.unpack_from("<I", data, offset)
    start = offset + 4
    return data[start : start + length], start + length


def _key(name: str) -> bytes:
    return struct.pack("<I", len(name)) + name.encode("ascii")


def parse_mt2(data: bytes) -> tuple[float, list[dict[str, object]]]:
    """Lap length (m) and sections, in lap order, from an i2 track file."""
    if data[4:16] != b"MoTeCTrackV2":
        raise ValueError("not a MoTeC i2 track file (no MoTeCTrackV2 header)")
    groups = data.find(_key("SectionGroups"))
    if groups < 0:
        raise ValueError("the track file has no section groups: mark up sections in i2 first")
    length_at = data.find(_key("Dist"), 0, groups)
    if length_at < 0:
        raise ValueError("the track file does not state its lap length")
    (lap_length,) = struct.unpack("<f", _value(data, length_at + 8)[0])

    markers: dict[str, float] = {}
    for match in re.finditer(re.escape(_key("Dist") + struct.pack("<I", 8)), data[groups:]):
        at = groups + match.end()
        (distance,) = struct.unpack_from("<d", data, at)
        name, _ = _value(data, at + 8 + len(_key("Name")))
        markers[name.decode("utf-16le")] = distance

    sections: list[dict[str, object]] = []
    for match in re.finditer(re.escape(_key("Marker1")), data[groups:]):
        first, after = _value(data, groups + match.end())
        second, after = _value(data, after + len(_key("Marker2")))
        name, after = _value(data, after + len(_key("Name")))
        kind_at = data.find(_key("SectionType") + struct.pack("<I", 1) + _key("Name"), after)
        kind = ""
        if 0 <= kind_at < after + 200:
            raw, _ = _value(data, kind_at + len(_key("SectionType")) + 4 + len(_key("Name")))
            kind = raw.decode("utf-16le")
        if kind not in _KINDS:
            raise ValueError(f"section {name.decode('utf-16le')!r} has unknown type {kind!r}")
        sections.append(
            {
                "name": name.decode("utf-16le"),
                "kind": _KINDS[kind],
                "start_m": round(markers[first.decode("utf-16le")], 1),
                "end_m": round(markers[second.decode("utf-16le")], 1),
            }
        )
    if not sections:
        raise ValueError("the track file has no sections: mark them up in i2 first")
    sections.sort(key=lambda section: section["start_m"])
    return round(float(lap_length), 1), sections


def read_sidecar(path: Path) -> tuple[str, float, list[dict[str, object]]]:
    """Track name, map lap length and validated sections from a JSON sidecar."""
    document = json.loads(path.read_text(encoding="utf-8"))
    sections = document.get("sections")
    length = document.get("sections_length_m")
    if not sections or not length:
        raise ValueError(f"{path}: needs both `sections` and `sections_length_m`")
    for section in sections:
        if section.get("kind") not in _KINDS.values():
            raise ValueError(f"{path}: section {section.get('name')!r} needs kind corner|straight")
        for bound in ("start_m", "end_m"):
            if not 0 <= float(section[bound]) <= float(length):
                raise ValueError(f"{path}: {section['name']} {bound} is outside the lap")
    return path.stem, float(length), sections


def _from_mt2(args: argparse.Namespace) -> int:
    lap_length, sections = parse_mt2(Path(args.mt2).read_bytes())
    sidecar = Path(args.sidecar)
    document = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else {}
    document["sections_length_m"] = lap_length
    document["sections"] = sections
    sidecar.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    corners = sum(1 for section in sections if section["kind"] == "corner")
    print(f"{sidecar}: {corners} corners, {len(sections) - corners} straights, lap {lap_length} m")
    return 0


def _load(args: argparse.Namespace) -> int:
    import psycopg

    from pit.db.dsn import dsn_from_env

    tracks = [read_sidecar(Path(path)) for path in args.sidecars]
    with psycopg.connect(args.dsn or dsn_from_env()) as conn:
        for track, lap_length, sections in tracks:
            conn.execute("DELETE FROM track_sections WHERE track_name = %s", (track,))
            for seq, section in enumerate(sections, start=1):
                conn.execute(
                    "INSERT INTO track_sections "
                    "(track_name, seq, name, kind, start_m, end_m, lap_length_m) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        track,
                        seq,
                        section["name"],
                        section["kind"],
                        section["start_m"],
                        section["end_m"],
                        lap_length,
                    ),
                )
            print(f"{track}: loaded {len(sections)} sections")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    from_mt2 = commands.add_parser("from-mt2", help="copy sections from an i2 track file")
    from_mt2.add_argument("mt2")
    from_mt2.add_argument("sidecar")
    from_mt2.set_defaults(run=_from_mt2)
    load = commands.add_parser("load", help="load sidecar sections into the pit database")
    load.add_argument("sidecars", nargs="+")
    load.add_argument("--dsn", default=None, help="connection string (default: TIMESCALE_*)")
    load.set_defaults(run=_load)
    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except (ValueError, OSError) as exc:
        print(f"track_sections: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
