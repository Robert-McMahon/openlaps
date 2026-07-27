"""Pure loading of timing-line KML files and JSON metadata sidecars."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from timing.timing_core import GPSPoint, TimingLine, classify_line

DEFAULT_MINI_SECTORS = 20
_KML_NAMESPACE = {"kml": "http://www.opengis.net/kml/2.2"}


@dataclass(frozen=True, slots=True)
class TrackDefinition:
    """Timing lines and distance metadata loaded for one track."""

    name: str
    lines: list[TimingLine]
    length_m: float = 0.0
    mini_sectors: int = DEFAULT_MINI_SECTORS


def parse_kml_file(kml_file: str | Path) -> list[TimingLine]:
    """Parse timing lines from a Google Earth KML track definition."""
    root = ET.parse(kml_file).getroot()
    lines: list[TimingLine] = []
    for placemark in root.findall(".//kml:Placemark", _KML_NAMESPACE):
        name_element = placemark.find("kml:name", _KML_NAMESPACE)
        coordinate_element = placemark.find(".//kml:LineString/kml:coordinates", _KML_NAMESPACE)
        if name_element is None or coordinate_element is None:
            continue
        name = name_element.text.strip()
        coordinates = coordinate_element.text.strip().split()
        if len(coordinates) < 2:
            continue
        start = coordinates[0].split(",")
        end = coordinates[1].split(",")
        if len(start) < 2 or len(end) < 2:
            continue
        lines.append(
            TimingLine(
                name=name,
                start=GPSPoint(lat=float(start[1]), lon=float(start[0]), timestamp=0.0),
                end=GPSPoint(lat=float(end[1]), lon=float(end[0]), timestamp=0.0),
                line_type=classify_line(name),
            )
        )
    return lines


def load_track(kml_file: str | Path) -> TrackDefinition:
    """Load one KML track and its optional same-stem JSON metadata sidecar."""
    path = Path(kml_file)
    metadata: dict[str, object] = {
        "length_m": 0.0,
        "mini_sectors": DEFAULT_MINI_SECTORS,
    }
    sidecar = path.with_suffix(".json")
    try:
        if sidecar.exists():
            metadata.update(json.loads(sidecar.read_text()))
    except Exception:
        pass
    return TrackDefinition(
        name=path.stem,
        lines=parse_kml_file(path),
        length_m=float(metadata["length_m"]),
        mini_sectors=int(metadata["mini_sectors"]),
    )


def load_tracks(tracks_directory: str | Path) -> dict[str, TrackDefinition]:
    """Load every KML track in a directory, keyed by file stem."""
    directory = Path(tracks_directory)
    if not directory.exists():
        return {}
    tracks: dict[str, TrackDefinition] = {}
    for path in directory.glob("*.kml"):
        try:
            tracks[path.stem] = load_track(path)
        except Exception:
            pass
    return tracks
