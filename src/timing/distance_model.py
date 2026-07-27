"""
distance_model — position-integrated distance-along-lap.

Pure, side-effect free. Accumulates lap distance from RTK position deltas
(sum of point-to-point great-circle distances since the last lap start) and,
anchored by a manually-configured track length, exposes for each GPS fix:

- ``lap_distance``  metres travelled since lap start
- ``lap_fraction``  lap_distance / track_length, clamped to [0, 1]
- ``mini_sector``   1..N equal-distance bucket the car is currently in

No reference path / reference lap is needed. The track length sets the
mini-sector boundaries and normalises the fraction. State is serialisable so a
caller can persist it.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

EARTH_RADIUS_M = 6371000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in metres."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


@dataclass
class DistanceState:
    lap_distance: float = 0.0
    prev_lat: float | None = None
    prev_lon: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> DistanceState:
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


class DistanceModel:
    """
    Parameters
    ----------
    track_length_m : known lap length (metres). If <= 0, fraction / mini-sector
        are unavailable (lap_distance is still accumulated).
    n_mini_sectors : number of equal-distance mini-sectors per lap.
    """

    def __init__(
        self, track_length_m: float, n_mini_sectors: int = 20, state: DistanceState | None = None
    ):
        self.track_length_m = float(track_length_m or 0.0)
        self.n_mini_sectors = max(1, int(n_mini_sectors))
        self.state = state or DistanceState()

    def start_lap(self) -> None:
        """Reset accumulated distance at the start of a new lap."""
        self.state.lap_distance = 0.0
        self.state.prev_lat = None
        self.state.prev_lon = None

    def update(self, lat: float, lon: float) -> None:
        """Accumulate distance for a new GPS fix."""
        st = self.state
        if st.prev_lat is not None and st.prev_lon is not None:
            st.lap_distance += haversine_m(st.prev_lat, st.prev_lon, lat, lon)
        st.prev_lat = lat
        st.prev_lon = lon

    # -- derived values -----------------------------------------------------
    @property
    def lap_distance(self) -> float:
        return self.state.lap_distance

    @property
    def lap_fraction(self) -> float:
        if self.track_length_m <= 0:
            return 0.0
        return min(1.0, self.state.lap_distance / self.track_length_m)

    @property
    def mini_sector(self) -> int:
        if self.track_length_m <= 0:
            return 0
        idx = int(self.lap_fraction * self.n_mini_sectors)
        return min(self.n_mini_sectors, idx + 1)  # 1-based, clamped to N

    def snapshot(self) -> dict:
        return {
            "lap_distance": round(self.state.lap_distance, 2),
            "lap_fraction": round(self.lap_fraction, 5),
            "mini_sector": self.mini_sector,
        }
