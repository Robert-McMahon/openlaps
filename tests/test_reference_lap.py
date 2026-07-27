"""Known-answer tests for reference_lap."""

import pytest

from timing.reference_lap import DeltaTracker, LapRecorder, ReferenceLap


def constant_speed_ref(length=1000.0, speed=10.0, step=10.0):
    """Reference lap at constant speed (10 m/s => 100 s for 1 km)."""
    dists = [i * step for i in range(int(length / step) + 1)]
    times = [d / speed for d in dists]
    return ReferenceLap(distances=dists, times=times, lap_time=length / speed)


def test_time_at_interpolates():
    ref = constant_speed_ref()
    assert ref.time_at(0) == 0.0
    assert ref.time_at(555) == pytest.approx(55.5)
    assert ref.time_at(99999) == pytest.approx(100.0)  # clamps to end
    assert ref.time_at(-5) == 0.0  # clamps to start


def test_recorder_decimates_and_stays_monotonic():
    rec = LapRecorder(grid_m=5.0)
    rec.reset()
    # 1 m samples: keeps roughly one per 5 m; ignores backwards jumps
    for i in range(101):
        rec.add(float(i), i / 10.0)
    rec.add(50.0, 99.0)  # backwards — ignored
    lap = rec.complete(lap_time=10.0)
    assert lap is not None
    assert all(b > a for a, b in zip(lap.distances, lap.distances[1:]))  # noqa: B905
    assert 15 <= len(lap.distances) <= 25  # ~100 m / 5 m grid
    assert lap.lap_time == 10.0


def test_recorder_too_sparse_returns_none():
    rec = LapRecorder()
    rec.reset()
    rec.add(0.0, 0.0)
    rec.add(50.0, 5.0)
    assert rec.complete(lap_time=10.0) is None


def test_delta_negative_when_faster():
    tracker = DeltaTracker(constant_speed_ref())  # 10 m/s baseline
    # at 500 m the reference took 50 s; we got there in 45 s => -5 s
    assert tracker.delta(500.0, 45.0) == pytest.approx(-5.0)
    assert tracker.predicted(500.0, 45.0) == pytest.approx(95.0)
    # slower: +2 s
    assert tracker.delta(500.0, 52.0) == pytest.approx(2.0)


def test_delta_none_without_reference():
    tracker = DeltaTracker()
    assert tracker.delta(100.0, 10.0) is None
    assert tracker.predicted(100.0, 10.0) is None


def test_offer_adopts_only_faster_valid_laps():
    tracker = DeltaTracker()
    slow = constant_speed_ref(speed=9.0)  # 111.1 s
    fast = constant_speed_ref(speed=11.0)  # 90.9 s
    assert tracker.offer(slow, valid=True)
    assert tracker.reference.lap_time == pytest.approx(1000 / 9.0)
    assert not tracker.offer(slow, valid=True)  # not faster
    assert not tracker.offer(fast, valid=False)  # invalid lap
    assert tracker.offer(fast, valid=True)  # faster + valid
    assert tracker.reference.lap_time == pytest.approx(1000 / 11.0)
    assert not tracker.offer(None, valid=True)


def test_roundtrip_serialisation():
    ref = constant_speed_ref()
    restored = ReferenceLap.from_dict(ref.to_dict())
    assert restored.lap_time == ref.lap_time
    assert restored.time_at(345) == pytest.approx(ref.time_at(345), abs=0.01)
