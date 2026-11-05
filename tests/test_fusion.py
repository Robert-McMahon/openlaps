"""IMU/GNSS fusion against simulated drives with known truth.

The simulator (``fusion.simulate``) produces a car with real sideslip, body
roll, squat/dive and grade, an IMU with bias and noise, and a GNSS receiver
with noise and output latency. Thresholds are a few times the errors the
filter achieves, so they catch regressions without being brittle to seeds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np
import pytest

from fusion.engine import EngineSettings, FusionEngine
from fusion.geodesy import LocalFrame
from fusion.ins import dcm_to_euler
from fusion.simulate import SensorModel, SimEvent, feed, lap_profile, simulate

SENSORS = SensorModel()


@dataclass
class Run:
    t: np.ndarray
    pos_err: np.ndarray
    alt_err: np.ndarray
    roll_err: np.ndarray
    pitch_err: np.ndarray
    yaw_err: np.ndarray
    slip_err: np.ndarray
    engine: FusionEngine

    def window(self, lo: float, hi: float) -> np.ndarray:
        return (self.t >= lo) & (self.t < hi)


def _angle_err(a: float, b: float) -> float:
    return math.degrees((a - b + math.pi) % (2 * math.pi) - math.pi)


def _run(
    sensors: SensorModel = SENSORS,
    duration: float = 75.0,
    settings: EngineSettings | None = None,
    events_hook=None,
) -> Run:
    events, truth, frame = simulate(lap_profile(), duration, sensors)
    if events_hook is not None:
        events = events_hook(events, frame)
    engine = FusionEngine(
        settings
        or EngineSettings(antenna_lever_arm=sensors.lever_arm, latency_s=sensors.gnss_latency_s)
    )
    rows: list[tuple[float, ...]] = []

    def step(t: float) -> None:
        out = engine.output()
        if out is None or engine.ins is None:
            return
        i = truth.at(t)
        est = frame.to_ned(*engine._frame.to_geodetic(engine.ins.p))
        roll, pitch, yaw = dcm_to_euler(engine.ins.dcm)
        slip = (
            out.slip_angle_deg - math.degrees(truth.slip[i])
            if out.slip_angle_deg is not None
            else math.nan
        )
        rows.append(
            (
                t,
                float(np.linalg.norm(est[:2] - truth.p[i, :2])),
                abs(est[2] - truth.p[i, 2]) if out.alt is not None else math.nan,
                _angle_err(roll, truth.euler[i, 0]),
                _angle_err(pitch, truth.euler[i, 1]),
                _angle_err(yaw, truth.euler[i, 2]),
                slip,
            )
        )

    feed(engine, events, step)
    a = np.array(rows)
    return Run(*(a[:, k] for k in range(7)), engine=engine)


@pytest.fixture(scope="module")
def baseline() -> Run:
    return _run()


def test_local_frame_round_trips_to_the_millimetre():
    frame = LocalFrame(-31.6745, 115.782, 40.0)
    for ned in ([1500.0, -2200.0, -35.0], [-3.0, 0.5, 12.0]):
        lat, lon, h = frame.to_geodetic(np.array(ned))
        back = LocalFrame(-31.6745, 115.782, 40.0).to_ned(lat, lon, h)
        assert np.allclose(back, ned, atol=1e-3)


def test_aligns_on_the_launch_and_not_while_parked(baseline: Run):
    # Parked for 6 s, then launches at 4 m/s^2: alignment needs 5 m/s.
    assert baseline.engine.stats.alignments == 1
    assert 7.0 < baseline.t[0] < 8.0


def test_rtk_position_is_centimetre_level(baseline: Run):
    w = baseline.window(15.0, 75.0)
    assert np.sqrt(np.mean(baseline.pos_err[w] ** 2)) < 0.03
    assert baseline.pos_err[w].max() < 0.1
    assert np.nanmax(baseline.alt_err[w]) < 0.15


def test_attitude_holds_through_sustained_corners(baseline: Run):
    # After the first corners make heading and tilt observable.
    w = baseline.window(20.0, 75.0)
    assert np.abs(baseline.roll_err[w]).max() < 0.8
    assert np.abs(baseline.pitch_err[w]).max() < 0.6
    assert np.abs(baseline.yaw_err[w]).max() < 0.6


def test_sideslip_is_resolved_to_a_fraction_of_a_degree(baseline: Run):
    w = baseline.window(20.0, 75.0)
    assert np.nanmax(np.abs(baseline.slip_err[w])) < 0.6


def test_gyro_bias_converges(baseline: Run):
    bias = np.radians(baseline.engine.output().gyro_bias_dps)
    assert abs(bias[2] - SENSORS.gyro_bias[2]) < math.radians(0.03)


def test_five_second_outage_drifts_under_a_metre_and_recovers():
    run = _run(replace(SENSORS, gnss_outages=((40.0, 45.0),)))
    during = run.window(40.0, 45.2)
    after = run.window(47.0, 75.0)
    assert run.pos_err[during].max() < 1.0
    assert run.pos_err[after].max() < 0.1
    assert run.engine.stats.outages_lost == 0


def test_status_reports_dead_reckoning_in_an_outage():
    events, _, _ = simulate(lap_profile(), 45.0, replace(SENSORS, gnss_outages=((40.0, 46.0),)))
    engine = FusionEngine(EngineSettings(antenna_lever_arm=SENSORS.lever_arm))
    feed(engine, events)
    out = engine.output()
    assert out.status == "dead_reckoning"
    assert out.gnss_age_s > 4.0


def test_too_long_an_outage_drops_position_and_realigns():
    sensors = replace(SENSORS, gnss_outages=((30.0, 45.0),))
    events, _, _ = simulate(lap_profile(), 55.0, sensors)
    engine = FusionEngine(EngineSettings(antenna_lever_arm=SENSORS.lever_arm, max_outage_s=10.0))
    feed(engine, events)
    assert engine.stats.outages_lost == 1
    assert engine.stats.alignments == 2
    assert engine.output() is not None


def test_single_wild_fix_is_rejected():
    def jump(events: list[SimEvent], frame: LocalFrame) -> list[SimEvent]:
        out = []
        for e in events:
            if e.kind == "fix" and 50.0 <= e.t < 50.02:
                lat, lon, sp, cog, mode = e.data
                e = SimEvent(e.t, e.kind, (lat + 15.0 / 111_000.0, lon, sp, cog, mode))
            out.append(e)
        return out

    run = _run(events_hook=jump)
    assert run.engine.stats.fixes_rejected >= 1
    assert run.pos_err[run.window(49.0, 52.0)].max() < 0.1


def test_latency_compensation_matters_at_speed():
    sensors = replace(SENSORS, gnss_latency_s=0.06)
    compensated = _run(sensors, duration=50.0)
    naive = _run(
        sensors,
        duration=50.0,
        settings=EngineSettings(antenna_lever_arm=SENSORS.lever_arm, latency_s=0.0),
    )
    w = compensated.window(20.0, 50.0)
    rms = lambda r: float(np.sqrt(np.mean(r.pos_err[w] ** 2)))  # noqa: E731
    assert rms(compensated) < 0.05
    assert rms(naive) > 3 * rms(compensated)


# ---- IMU failover -----------------------------------------------------------


def _track(sensors: SensorModel, duration: float, mutate=None):
    """Run the engine, recording (t, status, IMU-position error) per output."""
    events, truth, frame = simulate(lap_profile(), duration, sensors)
    if mutate is not None:
        events = mutate(events)
    engine = FusionEngine(
        EngineSettings(antenna_lever_arm=sensors.lever_arm, latency_s=sensors.gnss_latency_s)
    )
    rows: list[tuple[float, str, float]] = []

    def record(t: float) -> None:
        out = engine.output()
        if out is None or out.lat is None:
            return
        i = truth.at(t)
        ned = frame.to_ned(out.lat, out.lon)
        rows.append((t, out.status, float(np.linalg.norm(ned[:2] - truth.p[i, :2]))))

    for event in events:
        if event.kind == "imu":
            engine.imu(event.t, *event.data)
        elif event.kind == "fix":
            engine.gnss_fix(event.t, *event.data)
            record(event.t)  # outputs sampled at the fix rate in every mode
        elif event.kind == "alt":
            engine.gnss_alt(event.t, *event.data)
        else:
            engine.gnss_std(event.t, *event.data)
    return engine, rows


def _in(rows, lo, hi):
    return [r for r in rows if lo <= r[0] < hi]


def test_silent_imu_fails_over_to_gnss_and_realigns_when_it_returns():
    engine, rows = _track(replace(SENSORS, imu_outages=((40.0, 50.0),)), 65.0)

    during = _in(rows, 40.5, 50.0)
    assert {r[1] for r in during} == {"gnss_only"}
    # Passthrough has no attitude: moving a 1.2 m lever arm by course alone
    # ignores ~3.5 deg of sideslip and ~2.7 deg of roll at 1.5 g, worth
    # 10-15 cm on top of receiver noise. Still far better than the metre-
    # plus a raw fix would be off at speed without latency projection.
    assert max(r[2] for r in during) < 0.25
    assert engine.stats.imu_failovers == 1
    assert engine.stats.alignments == 2
    after = _in(rows, 55.0, 65.0)
    assert {r[1] for r in after} == {"rtk_fixed"}
    assert max(r[2] for r in after) < 0.1


def test_frozen_imu_is_detected_while_moving():
    engine, rows = _track(replace(SENSORS, imu_frozen=((40.0, 46.0),)), 46.0)
    assert engine.stats.imu_failovers == 1
    assert {r[1] for r in _in(rows, 41.0, 46.0)} == {"gnss_only"}


def test_impossible_imu_values_trigger_failover():
    def corrupt(events):
        out = []
        for e in events:
            if e.kind == "imu" and 40.0 <= e.t < 42.0:
                f, w = e.data
                e = SimEvent(e.t, "imu", (np.array([np.nan, f[1], f[2]]), w))
            out.append(e)
        return out

    engine, rows = _track(SENSORS, 45.0, mutate=corrupt)
    assert engine.stats.imu_failovers == 1
    assert engine.stats.imu_bad_samples > 100
    assert {r[1] for r in _in(rows, 40.5, 42.0)} == {"gnss_only"}


def test_no_imu_at_all_still_reports_gnss_position():
    engine, rows = _track(SENSORS, 30.0, mutate=lambda ev: [e for e in ev if e.kind != "imu"])
    assert engine.stats.alignments == 0
    assert rows and {r[1] for r in rows} == {"gnss_only"}
    # From the launch on (heading known), until the run's last fix.
    assert max(r[2] for r in rows if 8.0 < r[0] < 29.9) < 0.25


def test_parked_quantised_imu_is_not_mistaken_for_frozen():
    # A quiet sensor parked can repeat one CAN value indefinitely.
    engine = FusionEngine()
    f, w = np.array([0.0, 0.0, -9.79]), np.zeros(3)
    engine.gnss_fix(0.0, -31.67, 115.78, 0.0, 0.0, "R")
    for k in range(1, 1000):
        engine.imu(k * 0.01, f, w)
    assert engine.imu_healthy(10.0)
    assert engine.stats.imu_failovers == 0 and engine.stats.imu_bad_samples == 0
