"""Known-answer tests for distance_model."""

import math

import pytest

from timing.distance_model import (
    EARTH_RADIUS_M,
    DistanceModel,
    DistanceState,
    haversine_m,
)

REF_LAT, REF_LON = -31.66, 115.79
M_PER_DEG = math.pi * EARTH_RADIUS_M / 180.0  # haversine-consistent


def east(meters):
    """Longitude offset (degrees) for `meters` east at REF_LAT."""
    return meters / (M_PER_DEG * math.cos(math.radians(REF_LAT)))


def test_haversine_known_distance():
    # one degree of longitude at the equator == one degree of arc
    d = haversine_m(0.0, 0.0, 0.0, 1.0)
    assert d == pytest.approx(M_PER_DEG, rel=1e-6)
    # a small eastward hop of 100 m
    d2 = haversine_m(REF_LAT, REF_LON, REF_LAT, REF_LON + east(100))
    assert d2 == pytest.approx(100, rel=1e-3)


def test_distance_accumulates_along_straight_line():
    dm = DistanceModel(track_length_m=1000, n_mini_sectors=10)
    dm.start_lap()
    # step east in 100 m increments
    for i in range(0, 11):
        dm.update(REF_LAT, REF_LON + east(100 * i))
    assert dm.lap_distance == pytest.approx(1000, rel=1e-3)
    assert dm.lap_fraction == pytest.approx(1.0)


def test_mini_sector_bucketing():
    dm = DistanceModel(track_length_m=1000, n_mini_sectors=10)  # 100 m each
    dm.start_lap()
    dm.update(REF_LAT, REF_LON)  # 0 m -> sector 1
    assert dm.mini_sector == 1
    dm.update(REF_LAT, REF_LON + east(150))  # 150 m -> sector 2
    assert dm.mini_sector == 2
    dm.update(REF_LAT, REF_LON + east(950))  # 950 m -> sector 10
    assert dm.mini_sector == 10
    dm.update(REF_LAT, REF_LON + east(1200))  # past length -> clamp to 10
    assert dm.mini_sector == 10
    assert dm.lap_fraction == 1.0


def test_start_lap_resets():
    dm = DistanceModel(track_length_m=1000)
    dm.start_lap()
    dm.update(REF_LAT, REF_LON)
    dm.update(REF_LAT, REF_LON + east(500))
    assert dm.lap_distance == pytest.approx(500, rel=1e-3)
    dm.start_lap()
    assert dm.lap_distance == 0.0
    assert dm.snapshot()["mini_sector"] == 1


def test_no_length_disables_fraction():
    dm = DistanceModel(track_length_m=0)
    dm.start_lap()
    dm.update(REF_LAT, REF_LON)
    dm.update(REF_LAT, REF_LON + east(100))
    assert dm.lap_distance == pytest.approx(100, rel=1e-3)
    assert dm.lap_fraction == 0.0
    assert dm.mini_sector == 0


def test_state_roundtrip():
    dm = DistanceModel(track_length_m=1000)
    dm.start_lap()
    dm.update(REF_LAT, REF_LON)
    dm.update(REF_LAT, REF_LON + east(300))
    restored = DistanceState.from_dict(dm.state.to_dict())
    dm2 = DistanceModel(track_length_m=1000, state=restored)
    assert dm2.lap_distance == pytest.approx(300, rel=1e-3)
    # continues accumulating from restored position
    dm2.update(REF_LAT, REF_LON + east(400))
    assert dm2.lap_distance == pytest.approx(400, rel=1e-3)
