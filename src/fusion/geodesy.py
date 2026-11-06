"""WGS-84 conversions between geodetic coordinates and a local NED frame.

A circuit spans a few kilometres at most, so the filter navigates in a flat
north-east-down tangent frame anchored at the first fix. Conversions go
through ECEF so they stay exact (millimetre-level) across that span rather
than relying on a small-angle approximation.
"""

from __future__ import annotations

import math

import numpy as np

_A = 6378137.0  # WGS-84 semi-major axis, m
_F = 1.0 / 298.257223563
_E2 = _F * (2.0 - _F)  # first eccentricity squared
_B = _A * (1.0 - _F)
_EP2 = (_A * _A - _B * _B) / (_B * _B)

# Somigliana normal-gravity constants (WGS-84).
_GAMMA_E = 9.7803253359
_K = 0.00193185265241


def geodetic_to_ecef(lat_deg: float, lon_deg: float, h_m: float) -> np.ndarray:
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sin_lat = math.sin(lat)
    n = _A / math.sqrt(1.0 - _E2 * sin_lat * sin_lat)
    cos_lat = math.cos(lat)
    return np.array(
        [
            (n + h_m) * cos_lat * math.cos(lon),
            (n + h_m) * cos_lat * math.sin(lon),
            (n * (1.0 - _E2) + h_m) * sin_lat,
        ]
    )


def ecef_to_geodetic(xyz: np.ndarray) -> tuple[float, float, float]:
    """Bowring's closed form: sub-millimetre for terrestrial heights."""
    x, y, z = float(xyz[0]), float(xyz[1]), float(xyz[2])
    p = math.hypot(x, y)
    theta = math.atan2(z * _A, p * _B)
    sin_t, cos_t = math.sin(theta), math.cos(theta)
    lat = math.atan2(z + _EP2 * _B * sin_t**3, p - _E2 * _A * cos_t**3)
    lon = math.atan2(y, x)
    sin_lat = math.sin(lat)
    n = _A / math.sqrt(1.0 - _E2 * sin_lat * sin_lat)
    cos_lat = math.cos(lat)
    h = p / cos_lat - n if abs(cos_lat) > 1e-9 else abs(z) - _B
    return math.degrees(lat), math.degrees(lon), h


def normal_gravity(lat_deg: float, h_m: float = 0.0) -> float:
    """WGS-84 normal gravity magnitude, m/s^2 (free-air corrected)."""
    sin2 = math.sin(math.radians(lat_deg)) ** 2
    gamma = _GAMMA_E * (1.0 + _K * sin2) / math.sqrt(1.0 - _E2 * sin2)
    return gamma - 3.086e-6 * h_m


class LocalFrame:
    """North-east-down tangent frame anchored at one geodetic origin."""

    def __init__(self, lat_deg: float, lon_deg: float, h_m: float = 0.0) -> None:
        self.lat0 = lat_deg
        self.lon0 = lon_deg
        self.h0 = h_m
        self._origin = geodetic_to_ecef(lat_deg, lon_deg, h_m)
        lat = math.radians(lat_deg)
        lon = math.radians(lon_deg)
        sl, cl = math.sin(lat), math.cos(lat)
        so, co = math.sin(lon), math.cos(lon)
        # Rows are the N, E, D unit vectors expressed in ECEF.
        self._r_ned_ecef = np.array(
            [
                [-sl * co, -sl * so, cl],
                [-so, co, 0.0],
                [-cl * co, -cl * so, -sl],
            ]
        )
        self.gravity = normal_gravity(lat_deg, h_m)

    def to_ned(self, lat_deg: float, lon_deg: float, h_m: float | None = None) -> np.ndarray:
        """NED position of a fix; ``h_m=None`` puts it on the origin height."""
        height = self.h0 if h_m is None else h_m
        delta = geodetic_to_ecef(lat_deg, lon_deg, height) - self._origin
        ned = self._r_ned_ecef @ delta
        if h_m is None:
            # Unknown height: report the horizontal part only (down = 0 would
            # be wrong by the frame's curvature, ~8 cm at 1 km; zero it).
            ned[2] = 0.0
        return ned

    def to_geodetic(self, ned: np.ndarray) -> tuple[float, float, float]:
        return ecef_to_geodetic(self._origin + self._r_ned_ecef.T @ ned)
