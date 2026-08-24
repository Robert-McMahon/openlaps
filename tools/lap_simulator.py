#!/usr/bin/env python3
"""Synthetic lap simulator: a car that doesn't exist, lapping a track that does.

A smooth closed loop through the track's timing-line midpoints (sized to
the track's configured length), a curvature-derived speed profile with
lateral-g and accel/braking limits, per-lap pace variation, and 20 Hz GPS
with RTK-like noise -- fed as `$GPRMC` sentences through the **real** serial
collector's NMEA decoder into the **real** `Pipeline`/`LapTimingApp`, so the
real timing engine does the timing. Built so bench work does not need a car
(`docs/plan/PHASE3.md` P3.7).

Ported from `/mnt/data/logger/scripts/lap_simulator.py`: keeps the path and
speed math -- the actual value -- and drops the MQTT publishing,
`TrackManager` and `SessionCache` coupling, none of which exist here.
Simulated laps are stamped with a distinct `track_name` (default
`<track>_sim`, mirroring the predecessor's convention) so they are
trivially filterable and deletable from the pit database.

Examples:

    uv run tools/lap_simulator.py --dry-run
    uv run tools/lap_simulator.py --laps 10
    uv run tools/lap_simulator.py --laps 1 --pit-stop refuel
    uv run tools/lap_simulator.py --laps 1 --pit-stop service
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time

from _bench import (
    DEFAULT_PROFILE,
    DEFAULT_SERVER,
    load_catalog,
    make_publisher,
    publish_paced,
    rmc_sentence,
    wait_connected,
)

from agent.pipeline import Pipeline, TickBatch
from agent.timing_app import LapTimingApp
from collectors.clock import MonotonicWallClock, WallClock
from collectors.serial.transport import SerialCollector
from core.catalog import RuntimeCatalog
from core.config import ProfileConfig, load_profile
from timing.timing_core import LineType, TimingLine
from timing.tracks import TrackDefinition, load_tracks

M_PER_DEG_LAT = 111_320.0
DEFAULT_GPS_HZ = 20.0
DEFAULT_NOISE_M = 0.015
PIT_STOP_TYPES = ("refuel", "service")

# How far before the start/finish line each loop begins, in metres (the path is
# resampled at 1 m, so this is also a point count). See
# `_started_before_the_line`: enough that no fix rate can land the opening
# crossing on a segment endpoint.
START_LEAD_IN_M = 5

Point = tuple[float, float]


class Frame:
    """Local metric (x, y) frame around a reference lat/lon."""

    __slots__ = ("ref_lat", "ref_lon", "m_per_deg_lon")

    def __init__(self, ref_lat: float, ref_lon: float) -> None:
        self.ref_lat = ref_lat
        self.ref_lon = ref_lon
        self.m_per_deg_lon = M_PER_DEG_LAT * math.cos(math.radians(ref_lat))

    def to_xy(self, lat: float, lon: float) -> Point:
        return ((lon - self.ref_lon) * self.m_per_deg_lon, (lat - self.ref_lat) * M_PER_DEG_LAT)

    def to_ll(self, x: float, y: float) -> tuple[float, float]:
        return (self.ref_lat + y / M_PER_DEG_LAT, self.ref_lon + x / self.m_per_deg_lon)


# --- path geometry ------------------------------------------------------------


def catmull_rom_closed(points: list[Point], samples_per_seg: int = 200) -> list[Point]:
    """Dense closed Catmull-Rom spline through control points."""
    n = len(points)
    out: list[Point] = []
    for i in range(n):
        p0, p1, p2, p3 = (points[(i + k) % n] for k in (-1, 0, 1, 2))
        for j in range(samples_per_seg):
            t = j / samples_per_seg
            t2, t3 = t * t, t * t * t
            out.append(
                tuple(
                    0.5
                    * (
                        (2 * p1[k])
                        + (-p0[k] + p2[k]) * t
                        + (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * t2
                        + (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * t3
                    )
                    for k in (0, 1)
                )
            )
    return out


def path_length(path: list[Point]) -> float:
    return sum(math.dist(path[i], path[i + 1]) for i in range(len(path) - 1))


def resample_by_arc(path: list[Point], step: float = 1.0) -> list[Point]:
    """Resample a polyline to ~equal arc-length steps."""
    out = [path[0]]
    acc = 0.0
    for i in range(1, len(path)):
        acc += math.dist(path[i - 1], path[i])
        if acc >= step:
            out.append(path[i])
            acc = 0.0
    return out


def build_path(
    lines: list[TimingLine],
    frame: Frame,
    target_len: float,
    *,
    pit_stop: str | None = None,
    out_lap: bool = False,
) -> tuple[list[Point], float]:
    """Build a closed lap, optionally routing through one Wanneroo pit.

    A pit lap leaves through its exit just after start/finish and reaches its
    entry just before the next start/finish crossing.  Selecting the line pair
    by its exact KML suffix keeps refuel and service events distinguishable.

    With ``out_lap`` the loop still leaves through the pit exit -- closing the
    stop that the previous loop's entry crossing opened -- but never reaches
    the entry again: the lap that follows the run's last stop.
    """

    def mid(line: TimingLine) -> Point:
        return frame.to_xy((line.start.lat + line.end.lat) / 2, (line.start.lon + line.end.lon) / 2)

    start_finish = next(line for line in lines if line.line_type == LineType.START_FINISH)
    sectors = sorted(
        (line for line in lines if line.line_type == LineType.SECTOR), key=lambda line: line.name
    )
    if not sectors:
        raise ValueError("track has no sector lines to route the closed loop through")
    anchors = [mid(start_finish)]
    pit_entry = None
    if pit_stop is not None:
        if pit_stop not in PIT_STOP_TYPES:
            raise ValueError(f"unknown pit stop type {pit_stop!r}")
        suffix = pit_stop.title()
        pit_exit = next(
            line
            for line in lines
            if line.line_type == LineType.PIT_EXIT and line.name == f"PitExit{suffix}"
        )
        pit_entry = next(
            line
            for line in lines
            if line.line_type == LineType.PIT_ENTRY and line.name == f"PitEntry{suffix}"
        )
        anchors.append(mid(pit_exit))
    anchors.extend(mid(sector) for sector in sectors)
    if pit_entry is not None and not out_lap:
        anchors.append(mid(pit_entry))

    def left_offset(a: Point, b: Point, dist: float) -> Point:
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy) or 1.0
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        return (mx - dy / length * dist, my + dx / length * dist)

    def make(bulge: float) -> list[Point]:
        control: list[Point] = []
        n = len(anchors)
        for i in range(n):
            a, b = anchors[i], anchors[(i + 1) % n]
            control.append(a)
            # A small fixed bow on the first (pit-adjacent) leg, variable bulge
            # on the rest -- kept from the predecessor's shaping, which pulled
            # the loop away from a straight-line polygon through the anchors.
            control.append(left_offset(a, b, 40.0 if i == 0 else bulge))
        return resample_by_arc(catmull_rom_closed(control), 1.0)

    lo, hi = 10.0, 600.0
    path = make(hi)
    for _ in range(40):  # binary search the bulge to hit the target length
        bulge = (lo + hi) / 2
        path = make(bulge)
        length = path_length(path)
        if abs(length - target_len) < 2.0:
            break
        if length < target_len:
            lo = bulge
        else:
            hi = bulge
    return _started_before_the_line(path), path_length(path)


def build_paths(
    lines: list[TimingLine],
    frame: Frame,
    target_len: float,
    pit_stop: str | None,
) -> tuple[list[Point], float, list[Point] | None]:
    """The run's lap path, its length, and the closing loop's path.

    A pit run's timed laps drive the pit path, but the closing loop -- fed
    only to supply the final crossings, see `simulate` -- drives an out-lap
    instead: through the pit exit, which closes the last timed lap's stop,
    and never back through the entry, which would open a stop nothing will
    ever close. Without a pit stop the closing loop is just the lap path.
    """
    path, length = build_path(lines, frame, target_len, pit_stop=pit_stop)
    closing_path = None
    if pit_stop is not None:
        closing_path, _ = build_path(lines, frame, target_len, pit_stop=pit_stop, out_lap=True)
    return path, length, closing_path


def _started_before_the_line(path: list[Point]) -> list[Point]:
    """Roll the closed loop back so it begins *approaching* start/finish.

    The first anchor is the start/finish midpoint, so the loop used to begin
    exactly on the line -- and a segment whose first point lies on the line is
    a degenerate intersection: `segment_intersection` needs ``0 <= t <= 1``,
    and float64 can put it a whisker either side. Detecting the opening
    crossing was therefore a coin toss, and for a long time the toss was
    rigged: `rmc_sentence` rounded coordinates to 0.185 m, which reliably
    nudged that first point clear of the line. Widening the encoder for P4.6
    removed the nudge, the opening crossing started being missed, and lap 1
    came out invalid because timing then began mid-lap at Sector1.

    Starting a few metres short of the line makes the first segment span it
    unambiguously at any fix rate, with nothing resting on rounding. The loop
    is closed and resampled at 1 m, so this is a rotation: same path, same
    length, different entry point.
    """
    if len(path) <= START_LEAD_IN_M:
        return path
    return path[-START_LEAD_IN_M:] + path[:-START_LEAD_IN_M]


def speed_profile(
    path: list[Point],
    *,
    a_lat: float = 11.0,
    a_acc: float = 5.5,
    a_brk: float = 9.5,
    v_max: float = 52.0,
    v_min: float = 13.0,
    pace: float = 1.0,
) -> list[float]:
    """Curvature-limited speed per point (m/s), smoothed by accel/brake limits."""
    n = len(path)
    v = [v_max * pace] * n

    def curvature(i: int) -> float:
        p0, p1, p2 = path[(i - 1) % n], path[i], path[(i + 1) % n]
        a = math.dist(p0, p1)
        b = math.dist(p1, p2)
        c = math.dist(p0, p2)
        area2 = abs((p1[0] - p0[0]) * (p2[1] - p0[1]) - (p2[0] - p0[0]) * (p1[1] - p0[1]))
        if a * b * c == 0:
            return 0.0
        return 2 * area2 / (a * b * c)

    for i in range(n):
        k = curvature(i)
        if k > 1e-6:
            v[i] = min(v[i], max(v_min, math.sqrt(a_lat * pace / k)))

    for _ in range(2):  # two passes so accel/brake limits propagate across the seam
        for i in range(1, n):
            ds = math.dist(path[i - 1], path[i])
            v[i] = min(v[i], math.sqrt(v[i - 1] ** 2 + 2 * a_acc * ds))
        for i in range(n - 2, -1, -1):
            ds = math.dist(path[i], path[i + 1])
            v[i] = min(v[i], math.sqrt(v[i + 1] ** 2 + 2 * a_brk * ds))
    return v


def _bearing(a: Point, b: Point) -> float:
    """Compass bearing (0 = north, clockwise) from ``a`` to ``b``, in [0, 360)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == 0 and dy == 0:
        return 0.0
    return math.degrees(math.atan2(dx, dy)) % 360.0


def lap_points(
    path: list[Point],
    v: list[float],
    rate_hz: float,
    rng: random.Random,
    *,
    noise_m: float = DEFAULT_NOISE_M,
) -> tuple[list[tuple[float, float, float, float, float]], float]:
    """``rate_hz`` fixes along one lap. Returns (fixes, lap_time_s).

    Each fix is ``(t_s, x, y, speed_ms, heading_deg)`` relative to lap
    start; ``x``/``y`` carry Gaussian RTK-like noise.
    """
    n = len(path)
    times = [0.0]
    for i in range(1, n):
        ds = math.dist(path[i - 1], path[i])
        vv = max(1.0, (v[i - 1] + v[i]) / 2)
        times.append(times[-1] + ds / vv)
    lap_time = times[-1]

    fixes: list[tuple[float, float, float, float, float]] = []
    dt = 1.0 / rate_hz
    t = 0.0
    j = 0
    while t < lap_time:
        while j < n - 2 and times[j + 1] < t:
            j += 1
        span = times[j + 1] - times[j]
        frac = (t - times[j]) / span if span > 0 else 0.0
        x = path[j][0] + frac * (path[j + 1][0] - path[j][0])
        y = path[j][1] + frac * (path[j + 1][1] - path[j][1])
        heading = _bearing(path[j], path[j + 1])
        x += rng.gauss(0, noise_m)
        y += rng.gauss(0, noise_m)
        speed_ms = v[j] + frac * (v[j + 1] - v[j])
        fixes.append((t, x, y, speed_ms, heading))
        t += dt
    return fixes, lap_time


def lap_paces(laps: int, rng: random.Random) -> list[float]:
    """Warmup lap slower, then variation, with one clearly-fastest lap."""
    paces = []
    for i in range(laps):
        if i == 0:
            paces.append(0.90)
        elif laps >= 3 and i == laps - 2:
            paces.append(1.00)  # the best lap
        else:
            paces.append(rng.uniform(0.94, 0.99))
    return paces


# --- track / pipeline wiring ---------------------------------------------------


def resolve_track(profile: ProfileConfig, name: str | None) -> TrackDefinition:
    tracks = load_tracks(str(profile.path / "tracks"))
    lap_timing = profile.catalog.apps.lap_timing
    track_name = name or (lap_timing.track if lap_timing is not None else None)
    if not track_name:
        raise ValueError("no --track given and the profile has no apps.lap_timing.track")
    track = tracks.get(track_name)
    if track is None:
        available = ", ".join(sorted(tracks)) or "none"
        raise ValueError(f"track {track_name!r} not found (available: {available})")
    return track


def _emit(pipeline: Pipeline, source_class: str):
    def emit(sample) -> None:
        pipeline.ingest(source_class, sample)

    return emit


def simulate(
    profile: ProfileConfig,
    catalog: RuntimeCatalog,
    clock: WallClock,
    *,
    track: TrackDefinition,
    frame: Frame,
    path: list[Point],
    label: str,
    laps: int,
    rate_hz: float,
    seed: int,
    closing_path: list[Point] | None = None,
) -> tuple[list[TickBatch], list[float]]:
    """Feed ``laps`` synthetic laps through the real pipeline.

    Returns the resulting batches plus each lap's target duration (seconds)
    so the caller can report/verify pace variation.
    """
    lap_timing = profile.catalog.apps.lap_timing
    if lap_timing is None:
        raise ValueError("profile has no apps.lap_timing configured")
    tracks_by_name = load_tracks(str(profile.path / "tracks"))
    timing_app = LapTimingApp(catalog, lap_timing.position, track, tracks_by_name=tracks_by_name)
    # Labelling the session names the *sim* track, not the real one, so this
    # also (harmlessly) trips the app's track-switch lookup, which logs one
    # warning and stays put -- the label still lands on every event either
    # way, which is the point.
    timing_app.apply_session({"track_name": label})

    pipeline = Pipeline(catalog, tick_ms=20, timing_app=timing_app)
    base_mono_ns = time.monotonic_ns()
    rng = random.Random(seed)

    serial = profile.vehicle.serial[0]
    collector = SerialCollector(serial, _emit(pipeline, serial.name), wall_clock=clock)

    # Each physical loop's fixes start exactly at the line (`lap_points`
    # begins every loop at `path[0]`, the StartFinish midpoint), so the
    # engine's first crossing -- at the very start of loop 0 -- immediately
    # starts timing rather than needing a separate warmup loop; loop 0's own
    # lap then completes when loop 1's first fix reaches the line again, and
    # so on. The *last* loop fed therefore never completes -- there is no
    # following loop to supply its closing crossing -- so one extra loop is
    # driven purely to supply it, and its geometric estimate is dropped
    # below: `laps` requested laps takes `laps + 1` loops around the path.
    #
    # That extra loop is not a timed lap, so when the lap path routes through
    # a pit it drives ``closing_path`` -- an out-lap through the pit exit but
    # not the entry (`build_paths`) -- rather than opening one more stop the
    # run then ends inside of.
    lap_times: list[float] = []
    t0 = 0.0
    for index, pace in enumerate(lap_paces(laps + 1, rng)):
        loop_path = path if closing_path is None or index < laps else closing_path
        v = speed_profile(loop_path, pace=pace)
        fixes, lap_time = lap_points(loop_path, v, rate_hz, rng)
        lap_times.append(lap_time)
        for t_s, x, y, speed_ms, heading in fixes:
            lat, lon = frame.to_ll(x, y)
            sentence = rmc_sentence(lat, lon, speed_ms * 3.6, heading)
            collector.handle_line(sentence, t_mono_ns=base_mono_ns + int((t0 + t_s) * 1e9))
        t0 += lap_time

    return pipeline.flush(clock), lap_times[:-1]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--server", default=DEFAULT_SERVER, help="NATS URL (default: %(default)s)")
    parser.add_argument(
        "--vehicle", default=None, help="vehicle id override (default: the profile's)"
    )
    parser.add_argument(
        "--profile", default=str(DEFAULT_PROFILE), help="profile directory (default: %(default)s)"
    )
    parser.add_argument(
        "--track", default=None, help="track name (default: the profile's apps.lap_timing.track)"
    )
    parser.add_argument(
        "--label",
        default=None,
        help="track_name stamped on simulated laps (default: '<track>_sim')",
    )
    parser.add_argument(
        "--laps", type=int, default=10, help="laps to simulate (default: %(default)s)"
    )
    parser.add_argument(
        "--pit-stop",
        choices=PIT_STOP_TYPES,
        default=None,
        help="route each lap through the refuel or service pit (default: no pit stop)",
    )
    parser.add_argument(
        "--gps-hz", type=float, default=DEFAULT_GPS_HZ, help="GPS fix rate (default: %(default)s)"
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="RNG seed for pace/noise (default: %(default)s)"
    )
    parser.add_argument(
        "--state-dir", default=None, help="where the registry-generation counter persists"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate the closed-loop path only; no pipeline, no NATS",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    profile = load_profile(args.profile)
    track = resolve_track(profile, args.track)
    label = args.label or f"{track.name}_sim"

    start_finish = next(line for line in track.lines if line.line_type == LineType.START_FINISH)
    frame = Frame(
        (start_finish.start.lat + start_finish.end.lat) / 2,
        (start_finish.start.lon + start_finish.end.lon) / 2,
    )
    target_len = track.length_m or 2500.0
    path, length, closing_path = build_paths(track.lines, frame, target_len, args.pit_stop)
    print(
        f"lap-simulator: {track.name}: path {len(path)} pts, length {length:.0f} m "
        f"(target {target_len:.0f} m), label {label!r}",
        file=sys.stderr,
    )

    if args.dry_run:
        return 0

    if not profile.vehicle.serial:
        print("lap-simulator: profile has no serial source to carry position.*", file=sys.stderr)
        return 2

    _, catalog, vehicle_id = load_catalog(
        args.profile, vehicle=args.vehicle, state_dir=args.state_dir
    )
    publisher = make_publisher(args.server, vehicle_id, catalog)
    publisher.start()
    try:
        wait_connected(publisher)
    except TimeoutError as exc:
        print(f"lap-simulator: {exc}", file=sys.stderr)
        publisher.stop()
        return 1

    clock = MonotonicWallClock()
    try:
        batches, lap_times = simulate(
            profile,
            catalog,
            clock,
            track=track,
            frame=frame,
            path=path,
            label=label,
            laps=args.laps,
            rate_hz=args.gps_hz,
            seed=args.seed,
            closing_path=closing_path,
        )
        print(
            f"lap-simulator: {args.laps} lap(s), target times: "
            + ", ".join(f"{t:.1f}s" for t in lap_times),
            file=sys.stderr,
        )
        publish_paced(publisher, batches, rate=1.0)
    finally:
        publisher.drain(timeout_s=20.0)
        publisher.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
