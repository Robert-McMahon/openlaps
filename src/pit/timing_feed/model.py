"""The ``field_*`` schema in memory, and how each source's documents land in it.

The stable part of P7.10 is the schema: ``field_session`` (one row per
change of flag, clock or track temperature), ``field_cars`` (one row per
car per change of anything the timing screen shows for it), ``field_laps``
(derived: one row each time a car's lap count increments), and
``field_passings`` (every transponder crossing the feed reports). The
column vocabulary is a superset of Timing71's Common Timing Data columns
(locked decision 8), so a Timing71 state maps onto it with some columns
NULL, a browser relay's snapshot maps onto it directly, and the Natsoft
feed -- the richest source -- fills all of it.

``FieldState`` holds the merged standings and the competitor registry and
turns each incoming document into the rows that changed. It writes nothing
itself; the service hands the rows to the database in one transaction per
document. Nothing here knows which source it is fed by beyond the label it
stamps on every row.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Natsoft event/flag status -> the flag vocabulary the schema stores, which is
# Timing71's (lower case, UK spelling) so a relay and the feed agree.
FLAG_STATES: Mapping[str, str] = {
    "waitstart": "none",
    "green": "green",
    "yellow": "yellow",
    "red": "red",
    "checkered": "chequered",
    "chequered": "chequered",
    "end": "ended",
    "ended": "ended",
    "none": "none",
    "white": "white",
}
SUB_STATUSES: Mapping[str, str] = {
    "safetycar": "sc",
    "sc": "sc",
    "fcy": "fcy",
    "vsc": "vsc",
    "code_60": "code_60",
    "caution": "caution",
    "slow_zone": "slow_zone",
}
# Timing71 folds the safety-car states into the flag itself; the schema keeps
# the flag and the sub-status apart, the way the feed does.
T71_FLAG_SPLIT: Mapping[str, tuple[str, str | None]] = {
    "sc": ("yellow", "sc"),
    "fcy": ("yellow", "fcy"),
    "vsc": ("yellow", "vsc"),
    "code_60": ("yellow", "code_60"),
    "code_60_zone": ("yellow", "code_60"),
    "slow_zone": ("yellow", "slow_zone"),
    "caution": ("yellow", "caution"),
}

# Natsoft passing type codes -> the line they name.
PASSING_LINES: Mapping[int, str] = {
    1: "main",
    41: "main",
    2: "pit_main",
    3: "pit_entry",
    4: "pit_exit",
    5: "int1",
    45: "int1",
    6: "int2",
    46: "int2",
    11: "main",
    20: "pit_main",
}

MAX_CARS = 200


@dataclass(frozen=True, slots=True)
class SessionRow:
    """One ``field_session`` row."""

    time: datetime
    source: str
    session_name: str | None = None
    event_type: str | None = None
    flag_state: str | None = None
    sub_status: str | None = None
    time_remaining_s: float | None = None
    laps_remaining: int | None = None
    time_elapsed_s: float | None = None
    track_temp: float | None = None

    def same_as(self, other: SessionRow | None) -> bool:
        """Equal in everything but the timestamp."""
        return other is not None and replace(self, time=other.time) == other


@dataclass(frozen=True, slots=True)
class CarRow:
    """One ``field_cars`` row: the timing screen's line for one car."""

    time: datetime
    source: str
    epoch: datetime
    car_number: str
    competitor_id: str | None = None
    car_class: str | None = None
    position: int | None = None
    class_position: int | None = None
    laps: int | None = None
    last_lap_s: float | None = None
    best_lap_s: float | None = None
    gap_lead_s: float | None = None
    gap_next_s: float | None = None
    sec1_s: float | None = None
    sec2_s: float | None = None
    sec3_s: float | None = None
    pit_count: int | None = None
    in_pit: bool | None = None
    pit_flag: str | None = None
    driver: str | None = None
    state: str | None = None

    def same_as(self, other: CarRow | None) -> bool:
        """Equal in everything but the timestamps."""
        return other is not None and replace(self, time=other.time, epoch=other.epoch) == other


@dataclass(frozen=True, slots=True)
class LapRow:
    """One ``field_laps`` row, derived when a car's lap count increments."""

    time: datetime
    source: str
    car_number: str
    competitor_id: str | None
    lap_number: int
    lap_time_s: float | None
    position: int | None
    class_position: int | None
    gap_lead_s: float | None
    gap_next_s: float | None
    pit_count: int | None
    sec1_s: float | None
    sec2_s: float | None
    sec3_s: float | None
    flag_state: str | None
    sub_status: str | None


@dataclass(frozen=True, slots=True)
class PassingRow:
    """One ``field_passings`` row: a transponder crossing a timing line."""

    time: datetime
    source: str
    competitor_id: str
    line: str
    passing_type: int | None
    active: str | None
    tod: datetime | None
    car_number: str | None


@dataclass(frozen=True, slots=True)
class Batch:
    """Everything one document changed; empty batches are not written."""

    session: SessionRow | None = None
    cars: tuple[CarRow, ...] = ()
    laps: tuple[LapRow, ...] = ()
    passings: tuple[PassingRow, ...] = ()
    document: str = ""

    def __bool__(self) -> bool:
        return bool(self.session or self.cars or self.laps or self.passings)

    @property
    def rows(self) -> int:
        return (1 if self.session else 0) + len(self.cars) + len(self.laps) + len(self.passings)


@dataclass(slots=True)
class Competitor:
    """What ``CompetitorList`` says about one entry."""

    competitor_id: str
    number: str
    car_class: str | None = None
    drivers: dict[str, str] = field(default_factory=dict)

    def driver_name(self, driver_id: str | None) -> str | None:
        if driver_id is not None and driver_id in self.drivers:
            return self.drivers[driver_id]
        return next(iter(self.drivers.values()), None)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One standings snapshot in the ``field_*`` shape, from a relay or Timing71."""

    at: datetime
    source: str
    session: SessionRow | None
    cars: tuple[CarRow, ...]


class DocumentError(ValueError):
    """A document the feed sent could not be understood."""


class FieldState:
    """The merged standings, the registry, the session, and the diff on each update."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.competitors: dict[str, Competitor] = {}
        # Natsoft lines are the merge key; each holds the raw attributes of
        # the Position and its "All" Detail, merged across part updates.
        self._lines: dict[int, dict[str, str]] = {}
        # What was last emitted per car number, for the change diff and the
        # lap derivation.
        self.cars: dict[str, CarRow] = {}
        self.session: SessionRow | None = None
        self.epoch: datetime | None = None
        self.documents = 0
        self.unknown_tags: dict[str, int] = {}

    # -- Natsoft documents

    def apply_document(self, text: str, at: datetime) -> Batch:
        """Apply one Natsoft XML document; return the rows it changed."""
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise DocumentError(f"not XML: {exc}") from exc
        self.documents += 1
        session: SessionRow | None = None
        cars: list[CarRow] = []
        laps: list[LapRow] = []
        passings: list[PassingRow] = []
        elements = list(root) if root.tag == "New" else [root]
        for element in elements:
            tag = element.tag
            if tag == "Leaderboard":
                changed, new_laps = self._leaderboard(element, at)
                cars.extend(changed)
                laps.extend(new_laps)
            elif tag == "CompetitorList":
                self._competitor_list(element)
                if self._lines:
                    # A new registry can rename cars and drivers without a
                    # leaderboard update; re-derive the rows from the lines held.
                    changed, new_laps = self._diff(self._rows_from_lines(at), at, full=False)
                    cars.extend(changed)
                    laps.extend(new_laps)
            elif tag in ("Counters", "Status", "Heartbeat", "Flag", "Event"):
                session = self._session_update(element, at) or session
            elif tag in ("Passing", "Seen"):
                passing = self._passing(element, at)
                if passing is not None:
                    passings.append(passing)
            else:
                self.unknown_tags[tag] = self.unknown_tags.get(tag, 0) + 1
        return Batch(session, tuple(cars), tuple(laps), tuple(passings), text)

    def _competitor_list(self, element: ET.Element) -> None:
        registry: dict[str, Competitor] = {}
        for entry in element.iter("Competitor"):
            competitor_id = entry.get("ID")
            if competitor_id is None:
                continue
            competitor = Competitor(
                competitor_id=competitor_id,
                number=(entry.get("Number") or competitor_id).strip(),
                car_class=_text(entry.get("Class")),
            )
            for driver in entry.iter("Driver"):
                driver_id = driver.get("ID")
                name = _text(driver.get("Name"))
                if driver_id is not None and name:
                    competitor.drivers[driver_id] = name
            registry[competitor_id] = competitor
        self.competitors = registry

    def _leaderboard(self, element: ET.Element, at: datetime) -> tuple[list[CarRow], list[LapRow]]:
        kind = (element.get("Type") or "full").lower()
        incoming: dict[int, dict[str, str]] = {}
        for position in element.iter("Position"):
            line = _int(position.get("Line"))
            if line is None:
                continue
            attributes = dict(position.attrib)
            for detail in position.iter("Detail"):
                if (detail.get("Driv") or "All") == "All":
                    attributes.update({k: v for k, v in detail.attrib.items() if k != "Driv"})
            incoming[line] = attributes
        if kind == "part":
            for line, attributes in incoming.items():
                self._lines.setdefault(line, {}).update(attributes)
        else:
            self._lines = incoming
        if len(self._lines) > MAX_CARS:
            raise DocumentError(f"leaderboard has {len(self._lines)} lines; the cap is {MAX_CARS}")
        rows = self._rows_from_lines(at)
        return self._diff(rows, at, full=kind != "part")

    def _rows_from_lines(self, at: datetime) -> list[CarRow]:
        rows: list[CarRow] = []
        class_counts: dict[str | None, int] = {}
        for line in sorted(self._lines):
            attributes = self._lines[line]
            competitor_id = attributes.get("Comp")
            competitor = self.competitors.get(competitor_id or "")
            car_number = competitor.number if competitor else (competitor_id or str(line))
            position = _int(attributes.get("Pos"))
            car_class = competitor.car_class if competitor else None
            class_position = None
            if position is not None:
                class_counts[car_class] = class_counts.get(car_class, 0) + 1
                class_position = class_counts[car_class]
            pit_flag = _text(attributes.get("PitLaneFlag"))
            in_pit = pit_flag == "P"
            rows.append(
                CarRow(
                    time=at,
                    source=self.source,
                    epoch=self.epoch or at,
                    car_number=car_number,
                    competitor_id=competitor_id,
                    car_class=car_class,
                    position=position,
                    class_position=class_position,
                    laps=_int(attributes.get("LastLap")),
                    last_lap_s=_seconds(attributes.get("LastTime")),
                    best_lap_s=_seconds(attributes.get("FastTime")),
                    gap_lead_s=_seconds(attributes.get("GapLeadTime")),
                    gap_next_s=_seconds(attributes.get("GapNextTime")),
                    sec1_s=_seconds(attributes.get("LastSec1Time")),
                    sec2_s=_seconds(attributes.get("LastSec2Time")),
                    sec3_s=_seconds(attributes.get("LastSec3Time")),
                    pit_count=_int(attributes.get("PitStops")),
                    in_pit=in_pit,
                    pit_flag=pit_flag,
                    driver=competitor.driver_name(attributes.get("Driv")) if competitor else None,
                    state=_car_state(attributes.get("Pos"), in_pit, attributes.get("OutLap")),
                )
            )
        return rows

    def _diff(
        self, rows: Iterable[CarRow], at: datetime, *, full: bool
    ) -> tuple[list[CarRow], list[LapRow]]:
        """Rows that changed against what was last emitted, plus derived laps."""
        rows = list(rows)
        if full:
            numbers = {row.car_number for row in rows}
            if self.epoch is None or numbers != set(self.cars):
                # A different set of cars is a different standings table:
                # the view that shows "the latest snapshot" keys on this.
                self.epoch = at
                self.cars = {}
        epoch = self.epoch or at
        changed: list[CarRow] = []
        laps: list[LapRow] = []
        for row in rows:
            row = replace(row, epoch=epoch)
            previous = self.cars.get(row.car_number)
            if row.same_as(previous):
                continue
            # A lap is derived only from an increment against a count already
            # held: first sight of the field adopts the counts, so a restart
            # mid-race does not invent laps.
            if previous is not None and row.laps is not None and previous.laps is not None:
                if row.laps > previous.laps:
                    laps.append(self._lap(row))
            self.cars[row.car_number] = row
            changed.append(row)
        return changed, laps

    def _lap(self, row: CarRow) -> LapRow:
        flag = self.session.flag_state if self.session else None
        sub = self.session.sub_status if self.session else None
        return LapRow(
            time=row.time,
            source=self.source,
            car_number=row.car_number,
            competitor_id=row.competitor_id,
            lap_number=int(row.laps or 0),
            lap_time_s=row.last_lap_s,
            position=row.position,
            class_position=row.class_position,
            gap_lead_s=row.gap_lead_s,
            gap_next_s=row.gap_next_s,
            pit_count=row.pit_count,
            sec1_s=row.sec1_s,
            sec2_s=row.sec2_s,
            sec3_s=row.sec3_s,
            flag_state=flag,
            sub_status=sub,
        )

    def _session_update(self, element: ET.Element, at: datetime) -> SessionRow | None:
        current = self.session or SessionRow(time=at, source=self.source)
        updated = replace(current, time=at)
        tag = element.tag
        status = element.get("Status")
        if status is not None:
            updated = replace(updated, flag_state=flag_state(status))
        if "SubStatus" in element.attrib:
            updated = replace(updated, sub_status=sub_status(element.get("SubStatus")))
        temp = _seconds(element.get("TrackTemp"))
        if temp is not None:
            updated = replace(updated, track_temp=temp)
        if tag == "Counters":
            kind = (element.get("Type") or "").lower()
            count = _seconds(element.get("Count"))
            elapsed = _seconds(element.get("Elapsed"))
            if elapsed is not None:
                updated = replace(updated, time_elapsed_s=elapsed)
            if kind in ("time", "countdown") and count is not None:
                updated = replace(updated, time_remaining_s=count)
            elif kind == "laps" and count is not None:
                updated = replace(updated, laps_remaining=int(count))
            elif kind == "elapsed" and count is not None and elapsed is None:
                updated = replace(updated, time_elapsed_s=count)
        elif tag == "Event":
            name = _text(element.get("Description1")) or _text(element.get("Code"))
            updated = replace(
                updated,
                session_name=name or updated.session_name,
                event_type=_text(element.get("Type")) or updated.event_type,
            )
        if updated.same_as(self.session):
            return None
        self.session = updated
        return updated

    def _passing(self, element: ET.Element, at: datetime) -> PassingRow | None:
        competitor_id = _text(element.get("ID"))
        if competitor_id is None:
            return None
        code = _int(element.get("Type"))
        tod = _seconds(element.get("Time"))
        competitor = self.competitors.get(competitor_id)
        return PassingRow(
            time=at,
            source=self.source,
            competitor_id=competitor_id,
            line=PASSING_LINES.get(code or -1, str(code) if code is not None else "unknown"),
            passing_type=code,
            active=_text(element.get("Active")),
            tod=datetime.fromtimestamp(tod, tz=UTC) if tod and tod > 1e9 else None,
            car_number=competitor.number if competitor else None,
        )

    # -- snapshots from the other shapes

    def apply_snapshot(self, snapshot: Snapshot) -> Batch:
        """Apply a whole-standings snapshot (relay or Timing71); rows that changed."""
        self.documents += 1
        session = None
        if snapshot.session is not None:
            merged = replace(snapshot.session, time=snapshot.at, source=self.source)
            if not merged.same_as(self.session):
                self.session = merged
                session = merged
        rows = [replace(car, time=snapshot.at, source=self.source) for car in snapshot.cars]
        if len(rows) > MAX_CARS:
            raise DocumentError(f"snapshot has {len(rows)} cars; the cap is {MAX_CARS}")
        changed, laps = self._diff(rows, snapshot.at, full=True)
        return Batch(session, tuple(changed), tuple(laps), ())


# -- value parsing


def flag_state(value: str | None) -> str | None:
    """The schema's flag vocabulary for a feed or Timing71 status word."""
    if value is None:
        return None
    key = value.strip().lower()
    if not key:
        return None
    if key in T71_FLAG_SPLIT:
        return T71_FLAG_SPLIT[key][0]
    return FLAG_STATES.get(key, key)


def sub_status(value: str | None) -> str | None:
    """The schema's sub-status vocabulary; empty is NULL."""
    if value is None:
        return None
    key = value.strip().lower()
    if not key:
        return None
    return SUB_STATUSES.get(key, key)


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == int(value) else None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        try:
            number = float(text)
        except ValueError:
            return None
        return int(number) if number == int(number) else None


def _seconds(value: object) -> float | None:
    """A number of seconds from a number, ``m:ss.fff``, ``h:mm:ss`` or a plain float."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        try:
            total = 0.0
            for part in parts:
                total = total * 60 + float(part)
            return total
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def _car_state(pos: str | None, in_pit: bool, out_lap: str | None) -> str | None:
    if pos is not None and _int(pos) is None and pos.strip():
        # DNS, DNF, DSQ: the feed's own word.
        return pos.strip().upper()
    if in_pit:
        return "PIT"
    if (out_lap or "").strip().upper() == "Y":
        return "OUT"
    return "RUN"
