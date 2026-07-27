"""
Deterministic tests for timing_core using a synthetic track.

The track is three short vertical timing lines (StartFinish, Sector1, Sector2)
at x = 0, 1000, 2000 m, each spanning y ∈ [-5, 5]. The "car" crosses them moving
+x at chosen times, then loops back north/west/south (above the lines, no
crossing) to repeat. Because we control crossing times exactly, the expected lap
and sector splits are known.
"""

import math

import pytest

from timing.timing_core import (
    EventType,
    GPSPoint,
    LineType,
    TimingEngine,
    TimingLine,
    TimingState,
    classify_line,
    segment_intersection,
)

REF_LAT, REF_LON = -31.66, 115.79


def m2ll(x, y):
    lat = REF_LAT + y / 111320.0
    lon = REF_LON + x / (111320.0 * math.cos(math.radians(REF_LAT)))
    return lat, lon


def pt(x, y, t):
    lat, lon = m2ll(x, y)
    return GPSPoint(lat, lon, t)


def vline(xc, name, ltype):
    return TimingLine(name, GPSPoint(*m2ll(xc, -5), 0.0), GPSPoint(*m2ll(xc, 5), 0.0), ltype)


SF = vline(0, "StartFinish", LineType.START_FINISH)
S1 = vline(1000, "Sector1", LineType.SECTOR)
S2 = vline(2000, "Sector2", LineType.SECTOR)
STD_LINES = [SF, S1, S2]


def cross_x(xc, tc, dt=0.1):
    """Two points straddling the line at x=xc, crossing at exactly t=tc (+x)."""
    return [pt(xc - 2, 0, tc - dt / 2), pt(xc + 2, 0, tc + dt / 2)]


def connector(t0, t1):
    """Return-to-start path (north/west/south), never crossing a line."""
    return [pt(2002, 100, t0), pt(-10, 100, (t0 + t1) / 2), pt(-10, 0, t1)]


def build_stream(laps):
    """laps: list of (sf_t, s1_t, s2_t). SF of lap n+1 closes lap n."""
    pts = [pt(-10, 0, laps[0][0] - 5)]
    for i, (sf, s1, s2) in enumerate(laps):
        pts += cross_x(0, sf) + cross_x(1000, s1) + cross_x(2000, s2)
        if i + 1 < len(laps):
            pts += connector(s2 + 1, laps[i + 1][0] - 1)
    return pts


def run(engine, pts):
    events = []
    for p in pts:
        events += engine.process_point(p)
    return events


# --------------------------------------------------------------------------


def test_geometry_intersection_basic():
    # segment (-1,0)->(1,0) crosses vertical line (0,-1)->(0,1) at t=0.5
    hit = segment_intersection(-1, 0, 1, 0, 0, -1, 0, 1)
    assert hit is not None
    t, _cross = hit
    assert t == pytest.approx(0.5)
    # parallel / non-crossing
    assert segment_intersection(0, 0, 1, 0, 0, 1, 1, 1) is None


def test_splits_explicit():
    eng = TimingEngine(STD_LINES)
    pts = build_stream([(100, 110, 125), (160, 168, 181), (220, 230, 245)])
    events = run(eng, pts)

    sectors = [e for e in events if e.type == EventType.SECTOR_COMPLETED]
    laps = [e for e in events if e.type == EventType.LAP_COMPLETED]

    # Lap 1 sector splits: 10, 15, 35
    lap1_sectors = [e for e in sectors if e.lap_number == 1]
    assert [(e.sector, round(e.split_time, 3)) for e in lap1_sectors] == [
        (1, 10.0),
        (2, 15.0),
        (3, 35.0),
    ]
    assert all(e.valid for e in lap1_sectors)

    # Lap 1 completes at t=160 with lap_time 60
    assert len(laps) >= 1
    assert laps[0].lap_number == 1
    assert laps[0].lap_time == pytest.approx(60.0)
    assert laps[0].valid

    # Lap 2 sectors: 8, 13, 39  (168-160, 181-168, 220-181)
    lap2_sectors = [e for e in sectors if e.lap_number == 2]
    assert [(e.sector, round(e.split_time, 3)) for e in lap2_sectors] == [
        (1, 8.0),
        (2, 13.0),
        (3, 39.0),
    ]
    assert laps[1].lap_time == pytest.approx(60.0)  # 220-160


def test_best_lap_tracks_minimum():
    eng = TimingEngine(STD_LINES)
    # lap1 = 60 (100->160), lap2 = 54 (160->214)
    pts = build_stream([(100, 110, 125), (160, 168, 181), (214, 224, 239)])
    run(eng, pts)
    assert eng.state.best_lap_time == pytest.approx(54.0)
    assert eng.state.last_lap_time == pytest.approx(54.0)


def test_debounce_ignores_immediate_recross():
    eng = TimingEngine(STD_LINES, min_line_reentry_s=10.0)
    pts = [pt(-10, 0, 99)]
    pts += cross_x(0, 100)  # SF crossing accepted
    pts += cross_x(0, 103)  # re-cross 3s later -> debounced (ignored)
    pts += cross_x(1000, 110)  # S1
    events = run(eng, pts)
    # only one sector event (S1); the 103 re-cross must not have started a lap
    sf_events = [e for e in events if e.line == "StartFinish"]
    assert sf_events == []  # first SF just starts timing, recross ignored
    assert eng.state.lap_number == 1


def test_direction_gate_ignores_wrong_way():
    eng = TimingEngine(STD_LINES, gate_direction=True, min_line_reentry_s=0.0)
    # First cross +x (learns direction), then cross -x (opposite) -> ignored
    pts = [pt(-10, 0, 99)]
    pts += cross_x(0, 100)  # +x, learn
    pts += [pt(2, 0, 140), pt(-2, 0, 140.1)]  # -x crossing of SF, wrong way
    pts += cross_x(1000, 150)
    events = run(eng, pts)
    # the wrong-way SF crossing must not complete a lap
    assert all(e.type != EventType.LAP_COMPLETED for e in events)
    assert eng.state.lap_number == 1


def test_min_lap_time_rejects_phantom_lap():
    eng = TimingEngine(STD_LINES, min_lap_time_s=20.0, gate_direction=False, min_line_reentry_s=0.0)
    pts = [pt(-10, 0, 99)]
    pts += cross_x(0, 100)  # start lap 1
    pts += cross_x(0, 105)  # 5s later -> below min lap time, not a lap
    events = run(eng, pts)
    assert all(e.type != EventType.LAP_COMPLETED for e in events)
    assert eng.state.lap_number == 1


def test_skipped_sector_marks_invalid():
    eng = TimingEngine(STD_LINES)
    pts = [pt(-10, 0, 95)]
    pts += cross_x(0, 100)  # SF start
    pts += cross_x(1000, 110)  # S1 ok
    # skip S2, go straight to SF (simulate GPS gap) via connector then SF
    pts += connector(120, 158)
    pts += cross_x(0, 160)  # SF again but S2 was skipped
    events = run(eng, pts)
    lap = [e for e in events if e.type == EventType.LAP_COMPLETED]
    assert lap and lap[0].valid is False  # lap invalid due to skipped sector


def test_pit_in_out_marks_lap_invalid():
    lines = STD_LINES + [
        vline(500, "PitEntry", LineType.PIT_ENTRY),
        vline(1500, "PitExit", LineType.PIT_EXIT),
    ]
    eng = TimingEngine(lines)
    pts = [pt(-10, 0, 95)]
    pts += cross_x(0, 100)  # SF start
    pts += cross_x(500, 105)  # pit entry -> lap invalid, pit status
    pts += cross_x(1000, 110)
    pts += cross_x(1500, 112)  # pit exit -> back to track
    pts += cross_x(2000, 125)
    pts += connector(126, 158)
    pts += cross_x(0, 160)  # complete lap
    events = run(eng, pts)
    assert any(e.type == EventType.PIT_ENTRY for e in events)
    assert any(e.type == EventType.PIT_EXIT for e in events)
    lap = [e for e in events if e.type == EventType.LAP_COMPLETED]
    assert lap and lap[0].valid is False  # in-lap (touched pit) not valid


def test_state_roundtrip_persists():
    eng = TimingEngine(STD_LINES)
    run(eng, build_stream([(100, 110, 125), (160, 168, 181)]))
    d = eng.state.to_dict()
    restored = TimingState.from_dict(d)
    assert restored.lap_number == eng.state.lap_number
    assert restored.best_lap_time == eng.state.best_lap_time
    assert restored.line_direction == eng.state.line_direction
    # a fresh engine seeded with restored state keeps counting
    eng2 = TimingEngine(STD_LINES, state=restored)
    assert eng2.state.lap_number == eng.state.lap_number


def test_snapshot_running_values():
    eng = TimingEngine(STD_LINES)
    run(eng, [pt(-10, 0, 95)] + cross_x(0, 100) + cross_x(1000, 110))
    # feed a plain point at t=115 to advance the running clock
    eng.process_point(pt(1100, 0, 115))
    snap = eng.snapshot()
    assert snap["lap_number"] == 1
    assert snap["current_lap_time"] == pytest.approx(15.0, abs=0.2)  # 115-100
    assert snap["current_sector_time"] == pytest.approx(5.0, abs=0.2)  # 115-110


def test_partial_first_lap_invalid_but_sectors_count():
    """Timing that starts mid-lap (first crossing != SF) gives valid sector
    splits but the partial lap itself must be invalid."""
    eng = TimingEngine(STD_LINES)
    pts = [pt(990, 0, 95)]
    pts += cross_x(1000, 100)  # first crossing is Sector1 (mid-lap join)
    pts += cross_x(2000, 120)  # Sector2: genuine 20s split
    pts += connector(121, 158)
    pts += cross_x(0, 160)  # SF closes the partial lap
    pts += cross_x(1000, 170)  # next lap sector 1: 10s
    events = run(eng, pts)

    sectors = [e for e in events if e.type == EventType.SECTOR_COMPLETED]
    laps = [e for e in events if e.type == EventType.LAP_COMPLETED]
    # sector 2 split on the partial lap is genuine and in-order -> valid
    s2 = next(e for e in sectors if e.sector == 2 and e.lap_number == 1)
    assert s2.valid and s2.split_time == pytest.approx(20.0)
    # but the partial lap must not be a valid lap (didn't start at SF)
    assert laps and laps[0].valid is False
    assert eng.state.best_lap_time == 0.0  # not adopted as best
    # and the following full lap continues normally
    s1_lap2 = next(e for e in sectors if e.lap_number == 2 and e.sector == 1)
    assert s1_lap2.valid and s1_lap2.split_time == pytest.approx(10.0)


def test_classify_line():
    assert classify_line("StartFinish") == LineType.START_FINISH
    assert classify_line("Sector2") == LineType.SECTOR
    assert classify_line("PitEntry") == LineType.PIT_ENTRY
    assert classify_line("PitExit") == LineType.PIT_EXIT
    assert classify_line("Weird") == LineType.UNKNOWN
