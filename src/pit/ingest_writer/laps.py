"""Turn `lap.event` payloads into relational lap and sector rows.

Pure and side-effect free: feed it the JSON payloads carried by `lap.event`
samples in stream order, get back rows to write. The schema it parses is
pinned in `docs/WIRE_FORMAT.md` -> `lap.event` payload schema; unknown keys
and unknown event types are tolerated by design, since that schema is
expected to grow without a wire-format version bump.

Two ordering facts from the timing engine shape this module:

- **Sector events arrive before the lap they belong to.** The final
  `SECTOR_COMPLETED` is emitted in the same list as `LAP_COMPLETED`, earlier
  ones during the lap, so sectors are buffered until their lap row exists.
- **`lap_number` is not a key.** It is in-memory timing-engine state scoped
  to one agent run and one track, so it restarts at 1 on an agent restart or
  a track switch — within the same session. Laps are keyed on the crossing
  instant instead (see `docs/PIT_SCHEMA.md`), and a `lap_number` that goes
  backwards is the signal that the engine was rebuilt: the sector buffer for
  the old run is stale and is discarded.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

EVENT_SECTOR_COMPLETED = "sector_completed"
EVENT_LAP_COMPLETED = "lap_completed"
EVENT_PIT_ENTRY = "pit_entry"
EVENT_PIT_EXIT = "pit_exit"

_MAX_BUFFERED_SECTORS = 64


@dataclass(frozen=True, slots=True)
class SectorRow:
    """One completed sector split, pending its lap row."""

    sector: int
    split_time_s: float | None
    crossed_at: datetime


@dataclass(frozen=True, slots=True)
class LapRow:
    """One completed lap plus whichever sectors were buffered for it."""

    vehicle_id: str
    session_id: str | None
    stint_number: int | None
    track_name: str | None
    lap_number: int
    crossed_at: datetime
    lap_time_s: float | None
    valid: bool
    pit_status: str | None
    direction: str | None
    sectors: tuple[SectorRow, ...]


@dataclass(frozen=True, slots=True)
class PitStatusUpdate:
    """A pit entry/exit, applied to the most recent lap at that instant."""

    vehicle_id: str
    at: datetime
    pit_status: str


class LapMaterialiser:
    """Fold `lap.event` payloads into lap rows, buffering sectors."""

    def __init__(self, vehicle_id: str) -> None:
        self._vehicle_id = vehicle_id
        self._sectors: dict[tuple[int, int], SectorRow] = {}
        self._highest_lap_number: int | None = None
        self.malformed_events = 0
        self.unknown_event_types: set[str] = set()
        self.engine_restarts = 0
        self.laps_materialised = 0
        self.sectors_materialised = 0

    def observe(self, payload: str) -> LapRow | PitStatusUpdate | None:
        """Fold one `lap.event` payload; returns a row when one completes.

        A payload that cannot be parsed is counted and dropped — the raw
        sample is still written to `samples.value_text` by the caller, so
        nothing is lost and the event can be re-materialised later.
        """
        event = self._parse(payload)
        if event is None:
            return None
        decoded, event_type, at, lap_number = event

        if self._highest_lap_number is not None and lap_number < self._highest_lap_number:
            # The engine was rebuilt (agent restart or track switch): sectors
            # buffered for the previous run will never see their lap row.
            self.engine_restarts += 1
            logger.info(
                "laps: lap_number went %d -> %d, discarding %d buffered sector(s)",
                self._highest_lap_number,
                lap_number,
                len(self._sectors),
            )
            self._sectors.clear()
        self._highest_lap_number = max(lap_number, self._highest_lap_number or 0)

        if event_type == EVENT_SECTOR_COMPLETED:
            self._buffer_sector(lap_number, decoded, at)
            return None
        if event_type == EVENT_LAP_COMPLETED:
            return self._complete_lap(lap_number, decoded, at)
        if event_type in (EVENT_PIT_ENTRY, EVENT_PIT_EXIT):
            status = decoded.get("pit_status")
            return PitStatusUpdate(
                vehicle_id=self._vehicle_id,
                at=at,
                pit_status=str(status) if status is not None else event_type,
            )
        if event_type not in self.unknown_event_types:
            self.unknown_event_types.add(event_type)
            logger.info("laps: ignoring unknown lap.event type %r", event_type)
        return None

    # -- internals -------------------------------------------------------------

    def _parse(self, payload: str) -> tuple[dict, str, datetime, int] | None:
        """The three fields every event must have to be usable, plus the object."""
        try:
            decoded = json.loads(payload)
            if not isinstance(decoded, dict):
                raise TypeError("lap.event payload must be a JSON object")
            event_type = str(decoded["type"])
            # docs/WIRE_FORMAT.md: `time` is the interpolated crossing time in
            # epoch *seconds*, unlike the millisecond epochs on the wire.
            at = datetime.fromtimestamp(float(decoded["time"]), tz=UTC)
            lap_number = int(decoded["lap_number"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError, OverflowError):
            self.malformed_events += 1
            logger.warning("laps: discarding malformed lap.event payload: %.200r", payload)
            return None
        return decoded, event_type, at, lap_number

    def _buffer_sector(self, lap_number: int, decoded: dict, at: datetime) -> None:
        sector = _as_int(decoded.get("sector"))
        if sector is None:
            self.malformed_events += 1
            return
        if len(self._sectors) >= _MAX_BUFFERED_SECTORS:
            # A lap that never completes (car parked mid-lap, engine rebuilt
            # without a lap_number regression) must not grow this unbounded.
            oldest = min(self._sectors)
            del self._sectors[oldest]
        self._sectors[(lap_number, sector)] = SectorRow(
            sector=sector,
            split_time_s=_as_float(decoded.get("split_time")),
            crossed_at=at,
        )

    def _complete_lap(self, lap_number: int, decoded: dict, at: datetime) -> LapRow:
        sectors = tuple(
            row
            for (buffered_lap, _), row in sorted(self._sectors.items())
            if buffered_lap == lap_number
        )
        for key in [key for key in self._sectors if key[0] == lap_number]:
            del self._sectors[key]
        self.laps_materialised += 1
        self.sectors_materialised += len(sectors)
        return LapRow(
            vehicle_id=self._vehicle_id,
            session_id=_as_str(decoded.get("session_id")),
            stint_number=_as_int(decoded.get("stint_number")),
            track_name=_as_str(decoded.get("track_name")),
            lap_number=lap_number,
            crossed_at=at,
            lap_time_s=_as_float(decoded.get("lap_time")),
            valid=bool(decoded.get("valid", True)),
            pit_status=_as_str(decoded.get("pit_status")),
            direction=_as_str(decoded.get("direction")),
            sectors=sectors,
        )


def _as_int(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _as_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
