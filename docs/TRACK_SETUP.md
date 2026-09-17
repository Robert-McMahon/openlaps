# Track setup

openlaps detects laps, sectors, and pit movements by intersecting consecutive
GNSS positions with named lines in a KML file. A track definition consists of:

- `<TrackName>.kml` — start/finish, sector, and pit lines;
- optional `<TrackName>.json` — track length and mini-sector count; and
- `apps.lap_timing.track` in the profile catalog — the KML filename without
  `.kml`.

The example at
[`profiles/example-club-racer/tracks/Wanneroo.kml`](https://github.com/Robert-McMahon/openlaps/blob/main/profiles/example-club-racer/tracks/Wanneroo.kml)
is a complete working definition.

## File location and track name

Store track definitions in the profile's `tracks` directory:

```text
profiles/<profile>/tracks/
├── MyTrack.kml
└── MyTrack.json
```

The KML filename is significant: `MyTrack.kml` creates a track named
`MyTrack`. Select it in `catalog.yaml` using the same case:

```yaml
apps:
  lap_timing:
    position: position.*
    track: MyTrack
```

Session control may request another loaded track by name. Every `.kml` in the
profile's `tracks` directory is loaded at startup.

## Draw the lines

Google Earth Pro is a convenient editor:

1. Navigate to the circuit and create a folder for the track.
2. For each timing point, choose **Add → Path**.
3. Click once beyond one edge of the driven surface and once beyond the other.
   The resulting line should cross the track, not run along it.
4. Give the path one of the recognised names described below.
5. Repeat for the start/finish, sector boundaries, and each pit entry and exit.
6. Save the folder as **KML**, not KMZ, and place it in the profile's `tracks`
   directory.

Google Earth's style and camera metadata are harmless. Each timing placemark
must ultimately contain a KML 2.2 `LineString` with at least two coordinates.
openlaps uses only the first two, so use exactly two points for clarity.
Coordinates use KML order: `longitude,latitude,altitude`.

## Naming rules

Names are case-insensitive, but the recommended names below keep events and
operator displays consistent.

| Purpose | Recommended placemark name | Classification rule |
| --- | --- | --- |
| Start/finish | `StartFinish` | contains `start` or `finish` |
| First sector boundary | `Sector1` | contains `sector` |
| Second sector boundary | `Sector2` | contains `sector` |
| General pit entry | `PitEntry` | contains both `pit` and `entry` |
| General pit exit | `PitExit` | contains both `pit` and `exit` |
| Refuelling entry | `PitEntryRefuel` | contains both `pit` and `entry` |
| Refuelling exit | `PitExitRefuel` | contains both `pit` and `exit` |
| Service-pit entry | `PitEntryService` | contains both `pit` and `entry` |
| Service-pit exit | `PitExitService` | contains both `pit` and `exit` |

Use unique names. Unknown names are ignored by the timing engine. In
particular, `RefuelEntry` is invalid because it does not contain `pit`.

Only the first recognised start/finish line is used. Sector lines are sorted
by the trailing number in their names, not by their order in the KML file.
Number them consecutively as `Sector1`, `Sector2`, and so on.

A circuit divided into three timed sectors therefore needs two sector
boundaries: `Sector1` ends sector 1, `Sector2` ends sector 2, and the next
`StartFinish` crossing ends sector 3 and the lap.

Pit lines are independent of the lap-point sequence. A track may have one
`PitEntry`/`PitExit` pair or several distinctly named pairs, such as separate
refuelling and service areas. Entering any pit marks the current lap invalid;
exiting returns the timing state to `track`.

## Placement guidance

Line placement determines whether a crossing can be detected reliably:

- Put `StartFinish` on the circuit's official timing line where practical.
- Extend every line beyond both edges of every path the vehicle may take,
  including a wide or defensive line and expected GNSS position error.
- Draw lines approximately perpendicular to vehicle travel. A shallow crossing
  is more sensitive to GNSS noise and produces a less stable interpolated time.
- Place sector boundaries in sequential lap order and away from nearby pieces
  of track that could cross the same infinite-looking visual corridor. Only
  the finite two-point segment is active.
- Do not place two lap timing lines so close that one pair of consecutive GNSS
  fixes could cross both. The engine accepts at most one lap timing point per
  GNSS segment.
- Put pit entry and exit lines at the operational boundary you intend to time,
  and make sure the normal racing line cannot cross them.
- For separate pit routes, draw and name a complete entry/exit pair for each
  route.

The order of a line's two endpoints does not make it an entry or exit; its name
does. Endpoint order affects the reported crossing direction. The engine learns
the direction of the first accepted crossing for each line and rejects later
wrong-way crossings, so test the file using travel in the normal direction.

## Minimal KML structure

A KML exported by Google Earth will contain more styling, but this is all the
loader requires:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>MyTrack</name>

    <Placemark>
      <name>StartFinish</name>
      <LineString>
        <coordinates>
          115.000000,-31.000100,0 115.000200,-31.000100,0
        </coordinates>
      </LineString>
    </Placemark>

    <Placemark>
      <name>Sector1</name>
      <LineString>
        <coordinates>
          115.001000,-31.001100,0 115.001200,-31.001100,0
        </coordinates>
      </LineString>
    </Placemark>

    <Placemark>
      <name>PitEntry</name>
      <LineString>
        <coordinates>
          115.000300,-31.000300,0 115.000400,-31.000400,0
        </coordinates>
      </LineString>
    </Placemark>

    <Placemark>
      <name>PitExit</name>
      <LineString>
        <coordinates>
          115.000500,-31.000500,0 115.000600,-31.000600,0
        </coordinates>
      </LineString>
    </Placemark>
  </Document>
</kml>
```

Replace the example coordinates; they do not describe a real circuit.

## Optional JSON sidecar

A same-stem JSON file supplies distance metadata:

```json
{
  "length_m": 2411,
  "mini_sectors": 20
}
```

`length_m` is the official or measured lap length in metres. A value greater
than zero enables lap-fraction and mini-sector calculations. `mini_sectors` is
the number of equal-distance mini-sectors used by the distance model and
defaults to 20 when omitted. It is separate from the physical `SectorN` timing
lines in the KML.

## Validate before deployment

First inspect exactly what the loader recognises. Replace the path in this
command:

```bash
uv run python -c '
from timing.tracks import load_track
track = load_track("profiles/<profile>/tracks/MyTrack.kml")
print(f"track={track.name} length_m={track.length_m} mini_sectors={track.mini_sectors}")
for line in track.lines:
    print(f"{line.name}: {line.line_type.value} {line.start} -> {line.end}")
'
```

Check that:

- every intended placemark appears exactly once;
- no line is classified as `unknown`;
- there is exactly one start/finish line;
- sector numbers are consecutive and match their physical lap order;
- every pit route has both an entry and an exit; and
- the coordinates span the intended driven surface.

The repository test also rejects unclassified placemark names in any checked-in
profile:

```bash
uv run pytest tests/test_tracks.py::test_every_shipped_kml_placemark_has_a_known_line_type
```

Finally, replay recorded GNSS data or perform a low-speed survey lap in the
normal direction. Confirm that events arrive in this order:

```text
StartFinish → Sector1 → Sector2 → … → StartFinish
```

Exercise every pit route separately and confirm that the emitted event retains
the exact line name. A file that parses successfully is not proof that its
lines are correctly placed.

## Common failures

| Symptom | Likely cause |
| --- | --- |
| Track is not available | KML is outside the selected profile's `tracks` directory, is a KMZ, or has a different filename/case |
| A line never produces an event | Placemark name is unknown, geometry is not a `LineString`, or the segment does not span the driven path |
| Sectors are invalid or out of order | `SectorN` numbering does not match physical lap order, or a GNSS gap skipped a line |
| Pit events never fire | Name does not contain both `pit` and `entry`/`exit`, or the normal pit path misses the segment |
| Wrong-way crossings are ignored | The first accepted crossing taught the line the opposite direction; restart the agent and survey in the normal direction first |
| Duplicate or rapid events | Line lies along the driven path, is crossed during a spin, or is too close to another timing point |
