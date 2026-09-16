"""The two other ingest shapes, mapped onto the schema.

``POST /ingest/snapshot`` carries one standings snapshot as JSON in the
``field_*`` column names -- what the browser relay (``tools/timing_relay``)
reads off a rendered timing page. ``WS /ingest/t71`` carries Timing71's
standalone message protocol: a ``MANIFEST_UPDATE`` whose ``colSpec`` names
the columns, then ``STATE_UPDATE`` messages whose ``cars`` are rows against
it (locked decision 8). Both become a ``Snapshot`` and nothing downstream
knows which.

Everything here is bounded: a snapshot has at most ``MAX_CARS`` cars, every
string is clipped, and an unknown column is ignored rather than refused so
a provider that adds a column does not break the relay.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from pit.timing_feed.model import (
    MAX_CARS,
    T71_FLAG_SPLIT,
    CarRow,
    SessionRow,
    Snapshot,
    _int,
    _seconds,
    _text,
    flag_state,
    sub_status,
)

_MAX_TEXT = 80

# Timing71 column headings (Stat in its common library) -> CarRow fields.
T71_COLUMNS: Mapping[str, str] = {
    "num": "car_number",
    "state": "state",
    "class": "car_class",
    "pic": "class_position",
    "driver": "driver",
    "laps": "laps",
    "gap": "gap_lead_s",
    "int": "gap_next_s",
    "last": "last_lap_s",
    "best": "best_lap_s",
    "s1": "sec1_s",
    "s2": "sec2_s",
    "s3": "sec3_s",
    "pits": "pit_count",
}

_CAR_FIELDS = {
    "car_number": "text",
    "competitor_id": "text",
    "car_class": "text",
    "class": "text",
    "position": "int",
    "class_position": "int",
    "laps": "int",
    "last_lap_s": "seconds",
    "best_lap_s": "seconds",
    "gap_lead_s": "delta",
    "gap_next_s": "delta",
    "sec1_s": "seconds",
    "sec2_s": "seconds",
    "sec3_s": "seconds",
    "pit_count": "int",
    "in_pit": "bool",
    "pit_flag": "text",
    "driver": "text",
    "state": "text",
}


class ShapeError(ValueError):
    """The body is not a snapshot this service accepts."""


def _clip(value: object) -> str | None:
    text = _text(str(value)) if value is not None else None
    return text[:_MAX_TEXT] if text else None


def _delta(value: object) -> float | None:
    """A gap: seconds, or NULL for "1 lap", "2L" and other lap-count gaps."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip().lower()
    if not text or "lap" in text or text.endswith("l"):
        return None
    return _seconds(text.lstrip("+"))


def _coerce(kind: str, value: object) -> object:
    if isinstance(value, list | tuple):
        # Timing71's [value, flags] pairs; the flag is display metadata.
        value = value[0] if value else None
    if kind == "text":
        return _clip(value)
    if kind == "int":
        return _int(value)
    if kind == "seconds":
        return _seconds(value)
    if kind == "delta":
        return _delta(value)
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "y", "1", "p")
        return bool(value) if value is not None else None
    return value


def _session_from(mapping: Mapping[str, object], at: datetime, source: str) -> SessionRow | None:
    if not mapping:
        return None
    flag = mapping.get("flag_state")
    sub = mapping.get("sub_status")
    flag_text = flag_state(str(flag)) if flag is not None else None
    if isinstance(flag, str) and flag.strip().lower() in T71_FLAG_SPLIT and sub is None:
        sub = T71_FLAG_SPLIT[flag.strip().lower()][1]
    laps_remaining = _int(mapping.get("laps_remaining"))
    return SessionRow(
        time=at,
        source=source,
        session_name=_clip(mapping.get("session_name")),
        event_type=_clip(mapping.get("event_type")),
        flag_state=flag_text,
        sub_status=sub_status(str(sub)) if sub is not None else None,
        time_remaining_s=_seconds(mapping.get("time_remaining_s")),
        laps_remaining=laps_remaining,
        time_elapsed_s=_seconds(mapping.get("time_elapsed_s")),
        track_temp=_seconds(mapping.get("track_temp")),
    )


def _car_from(mapping: Mapping[str, object], at: datetime, source: str, index: int) -> CarRow:
    values: dict[str, object] = {}
    for key, raw in mapping.items():
        kind = _CAR_FIELDS.get(key)
        if kind is None:
            continue
        values["car_class" if key == "class" else key] = _coerce(kind, raw)
    number = values.get("car_number")
    if not number:
        raise ShapeError(f"cars[{index}] has no car_number")
    if values.get("position") is None:
        values["position"] = index + 1
    if values.get("in_pit") is None:
        state = values.get("state")
        values["in_pit"] = state == "PIT" if isinstance(state, str) else None
    return CarRow(time=at, source=source, epoch=at, **values)  # type: ignore[arg-type]


def snapshot_from_json(body: Mapping[str, object], at: datetime, source: str) -> Snapshot:
    """A relay's ``POST /ingest/snapshot`` body, checked and bounded."""
    cars = body.get("cars")
    if not isinstance(cars, list):
        raise ShapeError("cars must be a list")
    if len(cars) > MAX_CARS:
        raise ShapeError(f"snapshot has {len(cars)} cars; the cap is {MAX_CARS}")
    session = body.get("session")
    if session is not None and not isinstance(session, dict):
        raise ShapeError("session must be an object")
    label = _clip(body.get("source")) or source
    rows = []
    seen: set[str] = set()
    for index, entry in enumerate(cars):
        if not isinstance(entry, dict):
            raise ShapeError(f"cars[{index}] must be an object")
        row = _car_from(entry, at, label, index)
        if row.car_number in seen:
            raise ShapeError(f"car {row.car_number} appears twice")
        seen.add(row.car_number)
        rows.append(row)
    return Snapshot(
        at=at, source=label, session=_session_from(session or {}, at, label), cars=tuple(rows)
    )


class T71Translator:
    """Holds the latest manifest so each ``STATE_UPDATE`` can be read against it."""

    def __init__(self, source: str = "t71") -> None:
        self.source = source
        self.columns: list[str | None] = []
        self.track_temp_index: int | None = None
        self.session_name: str | None = None

    def manifest(self, manifest: Mapping[str, object]) -> None:
        """Adopt a ``MANIFEST_UPDATE``: which column is which."""
        spec = manifest.get("colSpec")
        if not isinstance(spec, list):
            raise ShapeError("manifest.colSpec must be a list")
        columns: list[str | None] = []
        for column in spec:
            heading = column[0] if isinstance(column, list | tuple) and column else column
            key = str(heading).strip().lower() if heading is not None else ""
            columns.append(T71_COLUMNS.get(key))
        self.columns = columns
        self.track_temp_index = None
        track_spec = manifest.get("trackDataSpec")
        if isinstance(track_spec, list):
            for index, heading in enumerate(track_spec):
                if "track" in str(heading).lower() and "temp" in str(heading).lower():
                    self.track_temp_index = index
                    break
        name = _clip(manifest.get("description")) or _clip(manifest.get("name"))
        self.session_name = name

    def state(self, state: Mapping[str, object], at: datetime | None = None) -> Snapshot:
        """A ``STATE_UPDATE`` as a snapshot; needs a manifest first."""
        if not self.columns:
            raise ShapeError("STATE_UPDATE before any MANIFEST_UPDATE")
        at = at or datetime.now(UTC)
        cars = state.get("cars")
        if not isinstance(cars, list):
            raise ShapeError("state.cars must be a list")
        if len(cars) > MAX_CARS:
            raise ShapeError(f"state has {len(cars)} cars; the cap is {MAX_CARS}")
        rows: list[CarRow] = []
        seen: set[str] = set()
        for index, entry in enumerate(cars):
            if not isinstance(entry, list | tuple):
                raise ShapeError(f"cars[{index}] must be a row")
            mapping: dict[str, object] = {}
            for column, value in zip(self.columns, entry, strict=False):
                if column is not None:
                    mapping[column] = value
            row = _car_from(mapping, at, self.source, index)
            if row.car_number in seen:
                continue
            seen.add(row.car_number)
            rows.append(row)
        session_in = state.get("session")
        session = None
        if isinstance(session_in, Mapping):
            mapping = {
                "session_name": self.session_name,
                "flag_state": session_in.get("flagState"),
                "time_remaining_s": session_in.get("timeRemain"),
                "time_elapsed_s": session_in.get("timeElapsed"),
                "laps_remaining": session_in.get("lapsRemain"),
            }
            track = session_in.get("trackData")
            if (
                self.track_temp_index is not None
                and isinstance(track, Sequence)
                and len(track) > self.track_temp_index
            ):
                mapping["track_temp"] = _temperature(track[self.track_temp_index])
            session = _session_from(mapping, at, self.source)
        return Snapshot(at=at, source=self.source, session=session, cars=tuple(rows))


def _temperature(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        digits = "".join(ch for ch in value if ch.isdigit() or ch in ".-")
        return _seconds(digits)
    return None
