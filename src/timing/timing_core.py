"""
timing_core — pure lap/sector timing engine.

Side-effect free (no MQTT, no I/O). Given a track definition (timing lines) and
a stream of GPS points, it detects line crossings and produces timing events
(sector completed, lap completed, pit entry/exit). State is serialisable so the
caller can persist it and survive a restart.

Design notes / fixes over the previous implementation:
- **Completed splits, correctly attributed.** A sector split is the time from
  the *previous* timing point to the crossing, emitted for the sector that just
  finished — not the freshly-reset running timer (which was always ~0 before).
- **Interpolated crossing time.** The exact crossing instant is interpolated
  along the GPS segment (parameter t), not quantised to the GPS fix timestamp.
- **Ordered sectors with skip detection.** Lap timing points are ordered
  (start/finish, then Sector1..N). Crossings are expected in order; a skipped
  point (GPS gap) invalidates the affected sectors instead of mis-attributing.
- **Anti-jitter.** Per-line debounce + a minimum lap time reject phantom
  crossings; per-line direction is learned and opposite crossings are ignored.
- **Validity.** Laps touching the pit lane, or with skipped/invalid sectors, are
  flagged invalid (excluded from best lap).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import NamedTuple


class GPSPoint(NamedTuple):
    lat: float
    lon: float
    timestamp: float  # epoch seconds (float); ms callers should convert once


class LineType(str, Enum):  # noqa: UP042 - preserve predecessor enum behaviour
    START_FINISH = "start_finish"
    SECTOR = "sector"
    PIT_ENTRY = "pit_entry"
    PIT_EXIT = "pit_exit"
    UNKNOWN = "unknown"


class TimingLine(NamedTuple):
    name: str
    start: GPSPoint
    end: GPSPoint
    line_type: LineType


class EventType(str, Enum):  # noqa: UP042 - preserve predecessor enum behaviour
    SECTOR_COMPLETED = "sector_completed"
    LAP_COMPLETED = "lap_completed"
    PIT_ENTRY = "pit_entry"
    PIT_EXIT = "pit_exit"


@dataclass
class TimingEvent:
    type: EventType
    time: float  # interpolated crossing time (epoch s)
    line: str  # timing line name
    lat: float
    lon: float
    lap_number: int
    sector: int = 0  # sector number this event pertains to
    split_time: float = 0.0  # completed sector split (s)
    lap_time: float = 0.0  # completed lap time (s), for LAP_COMPLETED
    valid: bool = True
    direction: str = ""
    pit_status: str = "track"


@dataclass
class TimingState:
    """Serialisable timing state (persist between restarts)."""

    lap_number: int = 0
    expected_idx: int = 0  # index into ordered lap timing points
    lap_start_time: float = 0.0
    segment_start_time: float = 0.0
    sectors_done: int = 0  # accepted sector crossings since lap start
    pit_status: str = "track"  # "track" | "pit"
    lap_valid: bool = True  # current lap eligible for best
    best_lap_time: float = 0.0  # 0 == none yet
    last_lap_time: float = 0.0
    # per-line: last accepted crossing time and learned direction sign (+1/-1)
    last_cross_time: dict[str, float] = field(default_factory=dict)
    line_direction: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> TimingState:
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


def _to_meters(lat: float, lon: float, ref_lat: float, ref_lon: float) -> tuple[float, float]:
    """Equirectangular projection to local metres about a reference point."""
    x = (lon - ref_lon) * 111320.0 * math.cos(math.radians(ref_lat))
    y = (lat - ref_lat) * 111320.0
    return x, y


def segment_intersection(x1, y1, x2, y2, x3, y3, x4, y4) -> tuple[float, float] | None:
    """
    Intersection of segment A(1->2) with segment B(3->4).
    Returns (t, cross) where t is the fraction along A at the crossing and
    `cross` is the signed cross product (B x A) giving crossing direction,
    or None if the segments do not cross.
    """
    denom = (x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3)
    if abs(denom) < 1e-12:
        return None  # parallel
    t = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / denom
    u = ((x3 - x1) * (y2 - y1) - (y3 - y1) * (x2 - x1)) / denom
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        cross = (x4 - x3) * (y2 - y1) - (y4 - y3) * (x2 - x1)
        return t, cross
    return None


class TimingEngine:
    """
    Drives lap/sector timing from a track definition and a GPS point stream.

    Parameters
    ----------
    lines : list[TimingLine]
    min_line_reentry_s : debounce — ignore re-crossing the same line within this.
    min_lap_time_s : reject a start/finish crossing yielding a shorter "lap".
    gate_direction : if True, learn each line's direction from its first accepted
        crossing and ignore opposite-direction crossings thereafter.
    """

    def __init__(
        self,
        lines: list[TimingLine],
        *,
        min_line_reentry_s: float = 10.0,
        min_lap_time_s: float = 20.0,
        gate_direction: bool = True,
        state: TimingState | None = None,
    ):
        self.lines = lines
        self.min_line_reentry_s = min_line_reentry_s
        self.min_lap_time_s = min_lap_time_s
        self.gate_direction = gate_direction
        self.state = state or TimingState()
        self._prev: GPSPoint | None = None
        self.revision = 0  # bumped whenever a crossing is accepted (state changed)

        # Ordered lap timing points: start/finish first, then sectors by number.
        sf = [ln for ln in lines if ln.line_type == LineType.START_FINISH]
        sectors = sorted(
            [ln for ln in lines if ln.line_type == LineType.SECTOR],
            key=lambda ln: _sector_num(ln.name),
        )
        self.lap_points: list[TimingLine] = (sf[:1] + sectors) if sf else sectors
        self.n_points = len(self.lap_points)
        # number of sectors per lap == number of lap points (SF + N sectors)
        self.pit_lines = [
            ln for ln in lines if ln.line_type in (LineType.PIT_ENTRY, LineType.PIT_EXIT)
        ]

    # -- geometry -----------------------------------------------------------
    def _crossing(self, p1: GPSPoint, p2: GPSPoint, line: TimingLine):
        """Return (crossing_time, direction_str, sign) if p1->p2 crosses line."""
        rlat, rlon = line.start.lat, line.start.lon
        ax1, ay1 = _to_meters(p1.lat, p1.lon, rlat, rlon)
        ax2, ay2 = _to_meters(p2.lat, p2.lon, rlat, rlon)
        bx1, by1 = _to_meters(line.start.lat, line.start.lon, rlat, rlon)
        bx2, by2 = _to_meters(line.end.lat, line.end.lon, rlat, rlon)
        hit = segment_intersection(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2)
        if hit is None:
            return None
        t, cross = hit
        ctime = p1.timestamp + t * (p2.timestamp - p1.timestamp)
        sign = 1 if cross >= 0 else -1
        direction = "clockwise" if sign > 0 else "counterclockwise"
        return ctime, direction, sign

    # -- main ---------------------------------------------------------------
    def process_point(self, gps: GPSPoint) -> list[TimingEvent]:
        events: list[TimingEvent] = []
        prev = self._prev
        self._prev = gps
        if prev is None:
            return events

        # Pit lines first (independent of the lap sequence).
        for line in self.pit_lines:
            hit = self._crossing(prev, gps, line)
            if hit and self._accept(line, hit):
                events += self._handle_pit(line, hit, gps)

        # Lap timing points (start/finish + sectors), in track order.
        for idx, line in enumerate(self.lap_points):
            hit = self._crossing(prev, gps, line)
            if hit and self._accept(line, hit):
                events += self._handle_lap_point(idx, line, hit, gps)
                break  # at most one lap point per GPS segment
        return events

    def _accept(self, line: TimingLine, hit) -> bool:
        """Debounce + direction gate. Records acceptance side effects."""
        ctime, _direction, sign = hit
        last = self.state.last_cross_time.get(line.name)
        if last is not None and (ctime - last) < self.min_line_reentry_s:
            return False
        if self.gate_direction:
            expected = self.state.line_direction.get(line.name)
            if expected is None:
                self.state.line_direction[line.name] = sign  # learn
            elif expected != sign:
                return False  # wrong-way crossing
        self.state.last_cross_time[line.name] = ctime
        self.revision += 1
        return True

    def _handle_pit(self, line, hit, gps) -> list[TimingEvent]:
        ctime, direction, _ = hit
        if line.line_type == LineType.PIT_ENTRY:
            self.state.pit_status = "pit"
            self.state.lap_valid = False  # this lap is an in-lap
            etype = EventType.PIT_ENTRY
        else:  # pit exit
            self.state.pit_status = "track"
            etype = EventType.PIT_EXIT
        return [
            TimingEvent(
                type=etype,
                time=ctime,
                line=line.name,
                lat=gps.lat,
                lon=gps.lon,
                lap_number=self.state.lap_number,
                direction=direction,
                pit_status=self.state.pit_status,
            )
        ]

    def _start_new_lap(self, ctime: float) -> None:
        self.state.lap_start_time = ctime
        self.state.segment_start_time = ctime
        self.state.expected_idx = 1 % self.n_points
        self.state.sectors_done = 0
        self.state.lap_valid = self.state.pit_status == "track"

    def _handle_lap_point(self, idx, line, hit, gps) -> list[TimingEvent]:
        ctime, direction, _ = hit
        st = self.state

        # First ever crossing: just start the clocks, no split/lap yet.
        if st.lap_start_time == 0.0:
            self._start_new_lap(ctime)
            st.lap_number = 1
            st.expected_idx = (idx + 1) % self.n_points
            if idx != 0:
                # Timing began mid-lap (e.g. out-lap joining the track): sector
                # splits from here are genuine, but the partial "lap" that ends
                # at the next start/finish crossing must not count as a lap.
                st.lap_valid = False
            return []

        in_order = idx == st.expected_idx

        # ---- Start/finish crossing -----------------------------------------
        if idx == 0:
            lap_time = ctime - st.lap_start_time
            # A re-cross with no sector progress, or an implausibly short lap,
            # is a glitch/spin — abandon the partial lap, resync, emit nothing.
            if st.sectors_done == 0 or lap_time < self.min_lap_time_s:
                self._start_new_lap(ctime)
                return []
            split = ctime - st.segment_start_time
            lap_valid = st.lap_valid and in_order and (st.pit_status == "track")
            events = [
                TimingEvent(
                    type=EventType.SECTOR_COMPLETED,
                    time=ctime,
                    line=line.name,
                    lat=gps.lat,
                    lon=gps.lon,
                    lap_number=st.lap_number,
                    sector=self.n_points,
                    split_time=split,
                    valid=lap_valid,
                    direction=direction,
                    pit_status=st.pit_status,
                )
            ]
            st.last_lap_time = lap_time
            if lap_valid and (st.best_lap_time == 0.0 or lap_time < st.best_lap_time):
                st.best_lap_time = lap_time
            events.append(
                TimingEvent(
                    type=EventType.LAP_COMPLETED,
                    time=ctime,
                    line=line.name,
                    lat=gps.lat,
                    lon=gps.lon,
                    lap_number=st.lap_number,
                    lap_time=lap_time,
                    valid=lap_valid,
                    direction=direction,
                    pit_status=st.pit_status,
                )
            )
            st.lap_number += 1
            self._start_new_lap(ctime)
            return events

        # ---- Sector crossing (idx > 0) -------------------------------------
        split = ctime - st.segment_start_time
        valid = in_order and (st.pit_status == "track")
        if not in_order:
            st.lap_valid = False  # skipped/out-of-order — invalidate lap
            valid = False
        st.sectors_done += 1
        st.segment_start_time = ctime
        st.expected_idx = (idx + 1) % self.n_points
        return [
            TimingEvent(
                type=EventType.SECTOR_COMPLETED,
                time=ctime,
                line=line.name,
                lat=gps.lat,
                lon=gps.lon,
                lap_number=st.lap_number,
                sector=idx,
                split_time=split,
                valid=valid,
                direction=direction,
                pit_status=st.pit_status,
            )
        ]

    # -- helpers for callers ------------------------------------------------
    def snapshot(self) -> dict:
        """Current running values (for enriching live GPS messages)."""
        st = self.state
        now = self._prev.timestamp if self._prev else 0.0
        if st.lap_start_time == 0.0:
            sector = 0  # timing not started
        else:
            # sector currently in progress = segment leading to the next point
            sector = st.expected_idx if st.expected_idx != 0 else self.n_points
        return {
            "lap_number": st.lap_number,
            "sector": sector,
            "pit_status": st.pit_status,
            "current_lap_time": max(0.0, now - st.lap_start_time) if st.lap_start_time else 0.0,
            "current_sector_time": max(0.0, now - st.segment_start_time)
            if st.segment_start_time
            else 0.0,
            "last_lap_time": st.last_lap_time,
            "best_lap_time": st.best_lap_time,
        }


def _sector_num(name: str) -> int:
    """Extract trailing integer from a sector name (Sector1 -> 1); 0 if none."""
    digits = ""
    for ch in reversed(name):
        if ch.isdigit():
            digits = ch + digits
        elif digits:
            break
    return int(digits) if digits else 0


def classify_line(name: str) -> LineType:
    """Classify a timing line by name (shared with KML loader)."""
    n = name.lower()
    if "start" in n or "finish" in n:
        return LineType.START_FINISH
    if "pit" in n and "entry" in n:
        return LineType.PIT_ENTRY
    if "pit" in n and "exit" in n:
        return LineType.PIT_EXIT
    if "sector" in n:
        return LineType.SECTOR
    return LineType.UNKNOWN
