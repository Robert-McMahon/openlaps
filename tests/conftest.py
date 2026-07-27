"""Shared fixtures: synthetic Wanneroo lap paths, NMEA encoding, docker NATS."""

from __future__ import annotations

import math
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from timing.tracks import TrackDefinition, load_track

EXAMPLE_PROFILE = Path(__file__).parents[1] / "profiles" / "example-club-racer"
WANNEROO_KML = EXAMPLE_PROFILE / "tracks" / "Wanneroo.kml"

_M_PER_DEG_LAT = 111_320.0
_NATS_IMAGE = "nats:2.12-alpine"
_LAP_POINT_ORDER = ("StartFinish", "Sector1", "Sector2")


def crossing_pair(line, offset_m: float = 20.0) -> tuple[tuple[float, float], tuple[float, float]]:
    """Two points straddling a timing line, ``offset_m`` either side of its middle."""
    mid_lat = (line.start.lat + line.end.lat) / 2
    mid_lon = (line.start.lon + line.end.lon) / 2
    cos_lat = math.cos(math.radians(mid_lat))
    along_x = (line.end.lon - line.start.lon) * _M_PER_DEG_LAT * cos_lat
    along_y = (line.end.lat - line.start.lat) * _M_PER_DEG_LAT
    norm = math.hypot(along_x, along_y)
    perp_x, perp_y = -along_y / norm, along_x / norm
    dlat = perp_y * offset_m / _M_PER_DEG_LAT
    dlon = perp_x * offset_m / (_M_PER_DEG_LAT * cos_lat)
    return (mid_lat - dlat, mid_lon - dlon), (mid_lat + dlat, mid_lon + dlon)


def wanneroo_track() -> TrackDefinition:
    """The example profile's track fixture."""
    return load_track(WANNEROO_KML)


def lap_path(
    track: TrackDefinition,
    laps: int = 1,
    *,
    t0_s: float = 0.0,
    leg_s: float = 30.0,
    points_per_leg: int = 8,
) -> list[tuple[float, float, float]]:
    """Synthesize ``(t_s, lat, lon)`` fixes that lap the track's timing lines.

    Crossing order is StartFinish -> Sector1 -> Sector2 -> StartFinish with
    ``leg_s`` seconds per leg, always crossing each line in the same
    direction so the engine's direction gate stays satisfied. Verified to
    produce only clean, valid laps against the Wanneroo fixture.
    """
    lines = {line.name: line for line in track.lines}
    pairs = {name: crossing_pair(lines[name]) for name in _LAP_POINT_ORDER}
    points: list[tuple[float, float, float]] = []
    t = t0_s
    for lap in range(laps + 1):
        for index, name in enumerate(_LAP_POINT_ORDER):
            if lap == laps and index > 0:
                break
            before, after = pairs[name]
            if points:
                previous = points[-1]
                arrive = t + leg_s - 1.0
                for step in range(1, points_per_leg):
                    fraction = step / points_per_leg
                    points.append(
                        (
                            previous[0] + fraction * (arrive - previous[0]),
                            previous[1] + fraction * (before[0] - previous[1]),
                            previous[2] + fraction * (before[1] - previous[2]),
                        )
                    )
                t += leg_s
            points.append((t, before[0], before[1]))
            points.append((t + 1.0, after[0], after[1]))
            t += 1.0
        if lap == laps:
            break
    return points


def rmc_sentence(lat: float, lon: float, *, speed_kn: float = 60.0, heading: float = 90.0) -> bytes:
    """Encode one valid ``$GPRMC`` sentence for a fix."""
    ns, alat = ("N", lat) if lat >= 0 else ("S", -lat)
    ew, alon = ("E", lon) if lon >= 0 else ("W", -lon)
    lat_field = f"{int(alat):02d}{(alat - int(alat)) * 60:07.4f}"
    lon_field = f"{int(alon):03d}{(alon - int(alon)) * 60:07.4f}"
    body = (
        f"GPRMC,000000.00,A,{lat_field},{ns},{lon_field},{ew},"
        f"{speed_kn:.2f},{heading:.1f},010126,,,A"
    )
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f"${body}*{checksum:02X}\r\n".encode("ascii")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def nats_url():
    """A fresh throwaway JetStream-enabled nats-server in docker (skip-less-able)."""
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    port = _free_port()
    name = f"openlaps-test-nats-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:4222",
            _NATS_IMAGE,
            "-js",
        ],
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        pytest.skip(f"cannot start nats container: {run.stderr.strip()[:200]}")
    try:
        deadline = time.monotonic() + 20.0
        ready = False
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1.0) as sock:
                    sock.settimeout(1.0)
                    if sock.recv(64).startswith(b"INFO"):
                        ready = True
                        break
            except OSError:
                pass
            time.sleep(0.1)
        if not ready:
            pytest.skip("nats container did not become ready")
        yield f"nats://127.0.0.1:{port}"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
