"""Synthetic drive generator for exercising the fusion engine against truth.

Builds a kinematically consistent car trajectory at 1 kHz from simple
commands (longitudinal acceleration, path curvature, road grade), with the
attitude a real car shows: sideslip proportional to lateral acceleration,
body roll out of corners, pitch from grade plus squat/dive. From it, it
synthesises what an IMU (with bias and noise) and a GNSS receiver (with
noise and output latency) would report.

Used by the tests and handy on the bench for tuning: run a profile, feed
``events`` to a ``FusionEngine``, and compare its output with ``truth_at``.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from fusion.geodesy import LocalFrame
from fusion.ins import dcm_to_euler, quat_from_euler, quat_to_dcm

G0 = 9.80665


@dataclass(frozen=True, slots=True)
class DriveCommand:
    """Commanded motion as functions of time (s)."""

    accel_long: Callable[[float], float]  # m/s^2 along the path
    curvature: Callable[[float], float]  # 1/m, positive = turning right
    grade: Callable[[float], float]  # rad, positive = climbing


@dataclass(frozen=True, slots=True)
class SensorModel:
    imu_rate_hz: float = 100.0
    gnss_rate_hz: float = 50.0
    aux_rate_hz: float = 10.0  # GGA/GST
    gnss_latency_s: float = 0.03
    gnss_sigma_h: float = 0.015
    gnss_sigma_v: float = 0.025
    gnss_vel_sigma: float = 0.03
    accel_noise: float = 0.04  # m/s^2 per sample
    gyro_noise: float = math.radians(0.05)  # rad/s per sample
    accel_bias: tuple[float, float, float] = (0.06, -0.04, 0.05)
    gyro_bias: tuple[float, float, float] = (
        math.radians(0.25),
        math.radians(-0.15),
        math.radians(0.3),
    )
    lever_arm: tuple[float, float, float] = (-0.8, 0.0, -0.9)
    gnss_outages: tuple[tuple[float, float], ...] = ()
    # IMU fault windows: silent (no frames), frozen (last frame repeated).
    imu_outages: tuple[tuple[float, float], ...] = ()
    imu_frozen: tuple[tuple[float, float], ...] = ()
    seed: int = 7


@dataclass(slots=True)
class Truth:
    t: np.ndarray
    p: np.ndarray  # IMU NED position
    v: np.ndarray
    euler: np.ndarray  # roll, pitch, yaw (rad)
    slip: np.ndarray  # rad
    accel_body: np.ndarray  # kinematic acceleration, body frame
    _index: list[float] = field(default_factory=list)

    def at(self, t: float) -> int:
        if not self._index:
            self._index = self.t.tolist()
        i = bisect.bisect_left(self._index, t)
        return min(i, len(self._index) - 1)


@dataclass(slots=True)
class SimEvent:
    t: float
    kind: str  # "imu" | "fix" | "alt" | "gst"
    data: tuple


def lap_profile(duration_s: float = 150.0, still_s: float = 6.0) -> DriveCommand:
    """A loop of straights, braking, fast and slow corners and hills."""

    def phase(t: float) -> float:
        return (t - still_s) % 30.0

    def accel_long(t: float) -> float:
        if t < still_s:
            return 0.0
        if t < still_s + 7.0:
            return 4.0  # launch to ~28 m/s
        ph = phase(t)
        if ph < 4.0:
            return 2.5  # straight, building speed
        if ph < 6.0:
            return -9.0  # brake zone into the slow corner
        if ph < 12.0:
            return 0.0
        if ph < 18.0:
            return 2.0  # exit and fast sweeper
        if ph < 21.0:
            return -4.0
        return 0.9  # net zero over the loop: lap speed stays constant

    def curvature(t: float) -> float:
        if t < still_s + 7.0:
            return 0.0
        ph = phase(t)
        if 6.0 <= ph < 12.0:
            return _bump(ph, 6.0, 12.0) / 55.0  # tight right-hander
        if 14.0 <= ph < 21.0:
            return -_bump(ph, 14.0, 21.0) / 115.0  # fast left sweeper
        if 23.0 <= ph < 28.0:
            return _bump(ph, 23.0, 28.0) / 80.0
        return 0.0

    def grade(t: float) -> float:
        if t < still_s:
            return 0.0
        return math.radians(4.0) * math.sin(2 * math.pi * (t - still_s) / 37.0)

    _ = duration_s
    return DriveCommand(accel_long, curvature, grade)


def _bump(x: float, a: float, b: float) -> float:
    """Smooth 0 -> 1 -> 0 over [a, b] (raised cosine)."""
    return 0.5 - 0.5 * math.cos(2 * math.pi * (x - a) / (b - a))


def simulate(
    command: DriveCommand,
    duration_s: float,
    sensors: SensorModel | None = None,
    origin: tuple[float, float, float] = (-31.6745, 115.7820, 40.0),
) -> tuple[list[SimEvent], Truth, LocalFrame]:
    s = sensors or SensorModel()
    rng = np.random.default_rng(s.seed)
    frame = LocalFrame(*origin)
    g_n = np.array([0.0, 0.0, frame.gravity])

    dt = 0.001
    n = int(duration_s / dt) + 1
    t = np.arange(n) * dt
    p = np.zeros((n, 3))
    v = np.zeros((n, 3))
    euler = np.zeros((n, 3))
    slip = np.zeros(n)
    speed, course = 0.0, math.radians(30.0)
    beta = 0.0
    a_long_f = 0.0  # filtered, for squat/dive pitch
    for i in range(n):
        ti = t[i]
        a_long = command.accel_long(ti)
        speed = max(0.0, speed + a_long * dt) if i else 0.0
        kappa = command.curvature(ti)
        a_lat = speed * speed * kappa
        # Sideslip (nose pointing inside the path) and body roll both
        # follow lateral acceleration with suspension-like lag.
        beta += (math.radians(2.5) * a_lat / G0 - beta) * dt / 0.15
        a_long_f += (a_long - a_long_f) * dt / 0.2
        gamma = command.grade(ti)
        course += speed * kappa * dt
        roll = -math.radians(1.8) * a_lat / G0
        pitch = gamma + math.radians(1.2) * a_long_f / G0  # dive under braking
        yaw = course + beta
        euler[i] = (roll, pitch, yaw)
        slip[i] = -beta
        v[i] = speed * np.array(
            [
                math.cos(gamma) * math.cos(course),
                math.cos(gamma) * math.sin(course),
                -math.sin(gamma),
            ]
        )
        if i:
            p[i] = p[i - 1] + 0.5 * (v[i] + v[i - 1]) * dt

    dcm = [quat_to_dcm(quat_from_euler(*e)) for e in euler]
    # Kinematic acceleration, central differences.
    a_n = np.gradient(v, dt, axis=0)
    accel_body = np.array([dcm[i].T @ a_n[i] for i in range(n)])

    events: list[SimEvent] = []
    arm = np.asarray(s.lever_arm)
    ba = np.asarray(s.accel_bias)
    bg = np.asarray(s.gyro_bias)

    step = round(1.0 / (s.imu_rate_hz * dt))
    frozen: tuple | None = None
    for k in range(0, n - step, step):
        r0, r1 = dcm[k], dcm[k + step]
        h = step * dt
        f_b = r0.T @ ((v[k + step] - v[k]) / h - g_n)
        w_b = _rotvec(r0.T @ r1) / h
        f_m = f_b + ba + rng.normal(0.0, s.accel_noise, 3)
        w_m = w_b + bg + rng.normal(0.0, s.gyro_noise, 3)
        t_k = t[k + step]
        if any(a <= t_k < b for a, b in s.imu_outages):
            continue
        if any(a <= t_k < b for a, b in s.imu_frozen):
            frozen = frozen or (f_m, w_m)
            f_m, w_m = frozen
        else:
            frozen = None
        events.append(SimEvent(t_k, "imu", (f_m, w_m)))

    def in_outage(time: float) -> bool:
        return any(a <= time < b for a, b in s.gnss_outages)

    gstep = round(1.0 / (s.gnss_rate_hz * dt))
    astep = round(1.0 / (s.aux_rate_hz * dt))
    for k in range(0, n, gstep):
        if in_outage(t[k]):
            continue
        r = dcm[k]
        w_b = _rotvec(r.T @ dcm[min(k + 1, n - 1)]) / dt
        ant = p[k] + r @ arm + rng.normal(0.0, s.gnss_sigma_h, 3) * np.array([1, 1, 0])
        v_ant = v[k] + r @ np.cross(w_b, arm)
        lat, lon, _ = frame.to_geodetic(ant)
        sp = math.hypot(v_ant[0], v_ant[1]) + rng.normal(0.0, s.gnss_vel_sigma)
        cog = math.degrees(math.atan2(v_ant[1], v_ant[0])) % 360.0
        arrive = t[k] + s.gnss_latency_s
        events.append(SimEvent(arrive, "fix", (lat, lon, max(sp, 0.0), cog, "R")))
        if k % astep == 0:
            alt = frame.h0 - (p[k] + r @ arm)[2] + rng.normal(0.0, s.gnss_sigma_v)
            events.append(SimEvent(arrive + 1e-6, "alt", (alt,)))
            sig = s.gnss_sigma_h
            events.append(SimEvent(arrive - 1e-6, "gst", (sig, sig, s.gnss_sigma_v)))

    events.sort(key=lambda e: e.t)
    truth = Truth(t=t, p=p, v=v, euler=euler, slip=slip, accel_body=accel_body)
    return events, truth, frame


def _rotvec(r: np.ndarray) -> np.ndarray:
    cos_angle = max(-1.0, min(1.0, (np.trace(r) - 1.0) / 2.0))
    angle = math.acos(cos_angle)
    axis = np.array([r[2, 1] - r[1, 2], r[0, 2] - r[2, 0], r[1, 0] - r[0, 1]])
    if angle < 1e-9:
        return 0.5 * axis
    return axis * angle / (2.0 * math.sin(angle))


def feed(engine, events: list[SimEvent], on_step: Callable[[float], None] | None = None) -> None:
    """Drive a ``FusionEngine`` with simulator events, in time order."""
    for event in events:
        if event.kind == "imu":
            engine.imu(event.t, *event.data)
            if on_step is not None:
                on_step(event.t)
        elif event.kind == "fix":
            engine.gnss_fix(event.t, *event.data)
        elif event.kind == "alt":
            engine.gnss_alt(event.t, *event.data)
        elif event.kind == "gst":
            engine.gnss_std(event.t, *event.data)


__all__ = [
    "DriveCommand",
    "SensorModel",
    "Truth",
    "dcm_to_euler",
    "feed",
    "lap_profile",
    "simulate",
]
