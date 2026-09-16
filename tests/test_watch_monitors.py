"""P7.6: drift, ratio, counter and the whole-car monitor.

Each kind against a synthetic clean run and the same run with one injected
fault: the fault opens its finding, the clean run opens none, and every
summary carries expected, observed and baseline. The wheel-speed monitor
stays quiet through a braking zone and a pit stop; the ratio monitor
learns per-gear baselines and names the gear a slip happened in; the
whole-car monitor, given an oil-pressure fault it was never configured
for, ranks car.oil_pressure first with expected and observed stated.
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

import pytest
import yaml

from pit.watch.config import WatchConfig, load_config
from pit.watch.engine import WatchEngine
from pit.watch.monitors import Counter, Drift, Ratio, WholeCar
from pit.watch.replay import ScaleFault
from pit.watch.service import Settings, WatchService

ROOT = Path(__file__).parents[1]
PROFILE = ROOT / "profiles/example-club-racer"
LAP_S = 60
UNITS = {
    "car.rpm": "RPM",
    "car.oil_pressure": "kPa",
    "car.oil_temp": "K",
    "car.coolant_temp": "K",
    "car.map": "kPa",
    "car.throttle_pos": "%",
    "car.fuel_pressure": "kPa",
    "car.battery_v": "Volts",
    "car.lambda1": "λ",
    "car.driveshaft_rpm": "RPM",
    "car.gear": "",
    "car.clutch_switch": "",
    "car.wheel_speed_fl": "km/h",
    "car.wheel_speed_fr": "km/h",
    "car.wheel_speed_rl": "km/h",
    "car.wheel_speed_rr": "km/h",
    "car.accel_x": "g",
    "car.vehicle_speed": "km/h",
    "car.brake_pedal_switch": "",
    "car.trigger_error_count": "",
}
GEAR_RATIOS = {1: 3.5, 2: 2.2, 3: 1.5, 4: 1.0}


def monitors(**specs) -> WatchConfig:
    return WatchConfig.model_validate(dict(baseline_rpm_min=1200, monitors=specs))


class Car:
    """A deterministic synthetic car: one reading set per second, laps of LAP_S."""

    def __init__(self, seed: int = 1) -> None:
        self.random = random.Random(seed)
        self.faults: list = []
        self.wheel_factor = {"fl": 1.0, "fr": 1.003, "rl": 0.997, "rr": 1.0}
        self.trigger_errors = 4

    def readings(self, t: int) -> dict[str, float]:
        r = self.random
        phase = (t % LAP_S) / LAP_S
        # Around the lap: a straight (steady, fast) then a braking zone.
        braking = 0.55 < phase < 0.65
        gear = 4 if phase < 0.5 else (2 if braking else 3)
        rpm = 4500 + 400 * math.sin(2 * math.pi * phase) + r.gauss(0, 40)
        speed = 150 if gear == 4 else (70 if braking else 110)
        speed += r.gauss(0, 1.5)
        oil_temp = 372 + 3 * math.sin(2 * math.pi * phase) + r.gauss(0, 0.3)
        oil_pressure = 0.018 * rpm - 0.4 * (oil_temp - 372) + 20 + r.gauss(0, 1.0)
        throttle = 85 if not braking else 5
        values = {
            "car.rpm": rpm,
            "car.oil_pressure": oil_pressure,
            "car.oil_temp": oil_temp,
            "car.coolant_temp": 362 + 0.5 * math.sin(2 * math.pi * phase) + r.gauss(0, 0.2),
            "car.map": 95 + 0.3 * throttle + r.gauss(0, 0.5),
            "car.throttle_pos": throttle + r.gauss(0, 1.0),
            "car.fuel_pressure": 300 + 0.002 * rpm + r.gauss(0, 1.0),
            "car.battery_v": 14.1 + r.gauss(0, 0.02),
            "car.lambda1": 0.9 + 0.0005 * (throttle - 85) + r.gauss(0, 0.005),
            "car.driveshaft_rpm": rpm / GEAR_RATIOS[gear] + r.gauss(0, 5),
            "car.gear": gear,
            "car.clutch_switch": 0,
            "car.accel_x": (-0.8 if braking else r.gauss(0, 0.03)),
            "car.vehicle_speed": speed,
            "car.brake_pedal_switch": 1 if braking else 0,
            "car.trigger_error_count": self.trigger_errors,
        }
        for corner, factor in self.wheel_factor.items():
            values[f"car.wheel_speed_{corner}"] = speed * factor + r.gauss(0, 0.3)
        for fault in self.faults:
            fault(t, values)
        return values


def drive(engine: WatchEngine, car: Car, seconds: int, *, pit_laps=(), start: int = 0):
    """Run the car through the engine; return the rows per tick and every finding seen."""
    findings: dict[str, list[dict]] = {}
    rows_by_tick = []
    for t in range(start, start + seconds):
        lap_number = t // LAP_S + 1
        in_pit = lap_number in pit_laps and 0.3 < (t % LAP_S) / LAP_S < 0.5
        status = "pit" if in_pit else "track"
        if t % LAP_S == 0 and t > 0:
            engine.observe(
                "lap.event",
                json.dumps(
                    dict(
                        type="lap_completed",
                        lap_number=lap_number - 1,
                        lap_time=float(LAP_S),
                        valid=(lap_number - 1) not in pit_laps,
                        pit_status=status,
                    )
                ),
                t - 0.02,
            )
        engine.observe("lap.event", json.dumps(dict(pit_status=status)), t - 0.01)
        for channel, value in car.readings(t).items():
            engine.observe(channel, value, t - 0.01, UNITS[channel])
        rows = engine.tick(t)
        rows_by_tick.append(rows)
        for name, row in rows.items():
            if row["finding"]:
                findings.setdefault(name, []).append(row["finding"])
    return rows_by_tick, findings


# --- drift ------------------------------------------------------------------------------


def drift_config(**overrides):
    spec = dict(
        kind="drift",
        target="car.oil_pressure",
        when=[dict(channel="car.rpm", between=[4000, 5000])],
        baseline_laps=4,
        min_lap_samples=10,
        min_scale=0.5,
        direction="down",
        severity="warning",
    )
    spec.update(overrides)
    return monitors(oil_drift=spec)


def test_drift_learns_from_clean_laps_and_a_slow_decline_accumulates_into_a_finding():
    clean = WatchEngine(drift_config())
    rows, findings = drive(clean, Car(), 20 * LAP_S)
    monitor = clean.monitors["oil_drift"]
    assert isinstance(monitor, Drift) and monitor.frozen and monitor.learned == 4
    assert not findings
    assert rows[-1]["oil_drift"]["baseline_status"] == "ready"
    assert rows[-1]["oil_drift"]["score"] < 1.0

    car = Car()
    # Half a kPa per lap: never a single sample the envelope would notice.
    car.faults.append(
        lambda t, v: v.__setitem__(
            "car.oil_pressure", v["car.oil_pressure"] - 0.5 * max(0, t // LAP_S - 5)
        )
    )
    faulty = WatchEngine(drift_config())
    _, findings = drive(faulty, car, 20 * LAP_S)
    assert set(findings) == {"oil_drift"}
    summary = findings["oil_drift"][-1]["summary"]
    assert summary["kind"] == "drift" and summary["direction"] == "down"
    assert summary["baseline"] == "session_start"
    assert summary["observed"] < summary["expected"]
    assert summary["unit"] == "kPa" and summary["cusum"] >= summary["cusum_h"]
    assert [lap["lap_number"] for lap in summary["series"]][-1] >= 10
    assert "per lap down" in summary["message"]


def test_drift_ignores_in_and_out_laps_and_a_lap_without_enough_condition_samples():
    engine = WatchEngine(drift_config())
    _, findings = drive(engine, Car(), 12 * LAP_S, pit_laps=(2, 3))
    monitor = engine.monitors["oil_drift"]
    banked = [lap["lap_number"] for lap in monitor.baseline_laps]
    assert 2 not in banked and 3 not in banked and monitor.frozen
    assert not findings
    # A lap driven entirely outside the cruise band contributes nothing.
    quiet = WatchEngine(drift_config(when=[dict(channel="car.rpm", between=[9000, 9500])]))
    drive(quiet, Car(), 8 * LAP_S)
    assert quiet.monitors["oil_drift"].learned == 0


def test_drift_finding_closes_after_three_calm_laps_and_survives_a_restart():
    car = Car()
    car.faults.append(
        lambda t, v: v.__setitem__(
            "car.oil_pressure", v["car.oil_pressure"] - (6.0 if 5 <= t // LAP_S < 12 else 0.0)
        )
    )
    engine = WatchEngine(drift_config())
    _, findings = drive(engine, car, 12 * LAP_S)
    opened = findings["oil_drift"][0]
    assert opened["closed_at"] is None
    model = json.loads(json.dumps(engine.monitors["oil_drift"].snapshot()))
    restarted = WatchEngine(drift_config())
    restarted.monitors["oil_drift"].restore(model)
    assert restarted.monitors["oil_drift"].finding["finding_id"] == opened["finding_id"]
    _, later = drive(restarted, car, 8 * LAP_S, start=12 * LAP_S)
    closed = later["oil_drift"][-1]
    assert closed["finding_id"] == opened["finding_id"] and closed["closed_at"] is not None
    assert restarted.monitors["oil_drift"].finding is None


# --- ratio --------------------------------------------------------------------------------


def driveline_config():
    return monitors(
        driveline=dict(
            kind="ratio",
            numerator="car.rpm",
            denominator="car.driveshaft_rpm",
            per="car.gear",
            when=[dict(channel="car.gear", above=0), dict(channel="car.clutch_switch", below=0.5)],
            baseline_seconds=120,
            min_group_samples=10,
            min_scale=0.02,
            score_window=10,
            high_means="clutch slip",
        )
    )


def test_ratio_learns_per_gear_and_reports_a_slip_in_the_gear_it_happened_in():
    clean = WatchEngine(driveline_config())
    _, findings = drive(clean, Car(), 8 * LAP_S)
    monitor = clean.monitors["driveline"]
    assert isinstance(monitor, Ratio) and monitor.frozen
    assert {int(k) for k in monitor.table} == {2, 3, 4}
    for gear, stats in monitor.table.items():
        assert stats["median"] == pytest.approx(GEAR_RATIOS[int(gear)], rel=0.02)
    assert not findings

    car = Car()
    # Third gear only: the clutch slips 8 % under load from lap 4.
    car.faults.append(
        lambda t, v: v.__setitem__(
            "car.driveshaft_rpm",
            v["car.driveshaft_rpm"] / (1.08 if v["car.gear"] == 3 and t >= 4 * LAP_S else 1),
        )
    )
    faulty = WatchEngine(driveline_config())
    _, findings = drive(faulty, car, 8 * LAP_S)
    assert set(findings) == {"driveline"}
    summary = findings["driveline"][-1]["summary"]
    assert summary["group"] == {"car.gear": 3}
    assert summary["direction"] == "high" and summary["meaning"] == "clutch slip"
    assert summary["expected"] == pytest.approx(1.5, rel=0.02)
    assert summary["observed"] == pytest.approx(1.5 * 1.08, rel=0.02)
    assert summary["baseline"] == "session_start"
    assert "in car.gear 3" in summary["message"]


def wheel_config(corner="rl"):
    others = [c for c in ("fl", "fr", "rl", "rr") if c != corner]
    return monitors(
        wheel=dict(
            kind="ratio",
            numerator=f"car.wheel_speed_{corner}",
            denominator=[f"car.wheel_speed_{c}" for c in others],
            when=[
                dict(channel="car.accel_x", between=[-0.15, 0.15]),
                dict(channel="car.vehicle_speed", above=60),
                dict(channel="car.brake_pedal_switch", below=0.5),
            ],
            baseline_seconds=90,
            min_group_samples=10,
            min_scale=0.004,
            score_window=10,
            low_means="puncture or pressure loss",
            high_means="dragging brake or bearing",
        )
    )


def test_wheel_speed_learns_the_tyre_difference_and_stays_quiet_in_braking_and_the_pits():
    engine = WatchEngine(wheel_config())
    rows, findings = drive(engine, Car(), 10 * LAP_S, pit_laps=(6,))
    monitor = engine.monitors["wheel"]
    assert monitor.frozen and not findings
    # The rear-left runs 0.3 % under the others by circumference: learned, not zero.
    assert monitor.table["all"]["median"] == pytest.approx(
        0.997 / ((1.0 + 1.003 + 1.0) / 3), rel=1e-3
    )
    braking = [r["wheel"] for t, r in enumerate(rows) if 0.55 < (t % LAP_S) / LAP_S < 0.65]
    assert all(r["baseline_status"] == "gated" for r in braking)
    pit = [
        r["wheel"]
        for t, r in enumerate(rows)
        if t // LAP_S + 1 == 6 and 0.3 < (t % LAP_S) / LAP_S < 0.5
    ]
    assert all(r["score"] is None for r in pit)


def test_a_slow_puncture_is_a_low_reading_and_a_dragging_brake_a_high_one():
    for factor, direction, meaning in (
        (0.98, "low", "puncture or pressure loss"),
        (1.02, "high", "dragging brake or bearing"),
    ):
        car = Car()
        car.faults.append(
            lambda t, v, f=factor: v.__setitem__(
                "car.wheel_speed_rl", v["car.wheel_speed_rl"] * (f if t >= 4 * LAP_S else 1.0)
            )
        )
        engine = WatchEngine(wheel_config())
        _, findings = drive(engine, car, 8 * LAP_S)
        assert set(findings) == {"wheel"}
        summary = findings["wheel"][-1]["summary"]
        assert summary["direction"] == direction and summary["meaning"] == meaning
        assert summary["target"] == "car.wheel_speed_rl" and summary["value_unit"] == "km/h"
        assert set(summary["denominator_values"]) == {
            "car.wheel_speed_fl",
            "car.wheel_speed_fr",
            "car.wheel_speed_rr",
        }


# --- counter -----------------------------------------------------------------------------


def counter_config():
    return monitors(
        trigger=dict(
            kind="counter",
            channel="car.trigger_error_count",
            score_window=60,
            open_finding_above=1.0,
            severity="critical",
        )
    )


def test_a_counter_that_holds_still_is_healthy_and_one_that_climbs_reports_its_rate():
    engine = WatchEngine(counter_config())
    rows, findings = drive(engine, Car(), 3 * LAP_S)
    monitor = engine.monitors["trigger"]
    assert isinstance(monitor, Counter) and not findings
    assert rows[-1]["trigger"]["score"] == 0.0 and rows[-1]["trigger"]["baseline_status"] == "ready"

    car = Car()

    def climb(t, values):
        # One more error every ten seconds from lap 3: 6 per minute.
        if t >= 2 * LAP_S:
            values["car.trigger_error_count"] = 4 + (t - 2 * LAP_S) // 10

    car.faults.append(climb)
    engine = WatchEngine(counter_config())
    _, findings = drive(engine, car, 5 * LAP_S)
    assert set(findings) == {"trigger"}
    summary = findings["trigger"][-1]["summary"]
    assert summary["rate_per_min"] == pytest.approx(6.0, rel=0.1)
    assert summary["observed"] > summary["expected"] and summary["baseline"] == "unchanged"
    assert findings["trigger"][-1]["severity"] == "critical"
    model = json.loads(json.dumps(engine.monitors["trigger"].snapshot()))
    restarted = WatchEngine(counter_config())
    restarted.monitors["trigger"].restore(model)
    assert (
        restarted.monitors["trigger"].finding["finding_id"] == findings["trigger"][-1]["finding_id"]
    )


# --- whole car -------------------------------------------------------------------------


def whole_car_config(**overrides):
    spec = dict(
        kind="whole_car",
        channels=[
            "car.rpm",
            "car.map",
            "car.throttle_pos",
            "car.oil_pressure",
            "car.oil_temp",
            "car.coolant_temp",
            "car.fuel_pressure",
            "car.battery_v",
            "car.lambda1",
        ],
        baseline="session_start",
        baseline_minutes=3,
        score_window=20,
        open_finding_above=4.0,
    )
    spec.update(overrides)
    return monitors(whole_car=spec)


def test_whole_car_learns_from_statistics_alone_and_the_clean_run_stays_under_threshold():
    engine = WatchEngine(whole_car_config())
    rows, findings = drive(engine, Car(), 10 * LAP_S)
    monitor = engine.monitors["whole_car"]
    assert isinstance(monitor, WholeCar) and monitor.frozen and monitor.learned == 180
    assert not findings
    ready = [r["whole_car"] for r in rows if r["whole_car"]["baseline_status"] == "ready"]
    assert ready and max(r["score"] for r in ready) < 1.0
    # The checkpoint is a p x p table, whatever the baseline window length.
    snapshot = json.loads(json.dumps(monitor.snapshot()))
    assert len(snapshot["sum_sq"]) == 9 and len(snapshot["coef"]) == 9
    # Oil pressure is predicted from rpm and oil temperature, as the physics says.
    index = monitor.channels.index("car.oil_pressure")
    strongest = sorted(range(9), key=lambda k: -abs(monitor.coef[index, k]))[:2]
    assert {monitor.channels[k] for k in strongest} == {"car.rpm", "car.oil_temp"}


def test_whole_car_ranks_an_unconfigured_oil_pressure_fault_first_with_expected_and_observed():
    car = Car()
    fault = ScaleFault(after_s=5 * LAP_S)
    car.faults.append(
        lambda t, v: v.__setitem__(
            "car.oil_pressure", fault.apply("car.oil_pressure", v["car.oil_pressure"], t)
        )
    )
    engine = WatchEngine(whole_car_config())
    _, findings = drive(engine, car, 8 * LAP_S)
    assert set(findings) == {"whole_car"}
    finding = findings["whole_car"][-1]
    summary = finding["summary"]
    assert summary["kind"] == "whole_car" and summary["baseline"] == "session_start"
    top = summary["channels"][0]
    assert top["channel"] == "car.oil_pressure" == summary["target"]
    assert top["direction"] == "below" and top["unit"] == "kPa"
    assert top["observed"] == pytest.approx(0.7 * top["expected"], rel=0.05)
    assert summary["expected"] == top["expected"] and summary["observed"] == top["observed"]
    assert -35 < top["percent"] < -25
    assert set(top["predicted_from"]) >= {"car.rpm"}
    assert summary["message"].startswith("car.oil_pressure ")
    assert "% below expected from car.rpm" in summary["message"]
    assert finding["peak_score"] == 1.0
    # Restore, then the same numbers come out of the checkpoint.
    model = json.loads(json.dumps(engine.monitors["whole_car"].snapshot()))
    restarted = WatchEngine(whole_car_config())
    restarted.monitors["whole_car"].restore(model)
    assert restarted.monitors["whole_car"].finding["finding_id"] == finding["finding_id"]
    _, again = drive(restarted, car, LAP_S, start=8 * LAP_S)
    assert again["whole_car"][-1]["summary"]["channels"][0]["channel"] == "car.oil_pressure"


def test_every_kind_refuses_a_checkpoint_from_a_different_configuration():
    for config, changed in (
        (drift_config(), drift_config(baseline_laps=6)),
        (whole_car_config(), whole_car_config(baseline_minutes=4)),
    ):
        engine = WatchEngine(config)
        drive(engine, Car(), 2 * LAP_S)
        (name,) = engine.monitors
        model = json.loads(json.dumps(engine.monitors[name].snapshot()))
        with pytest.raises(ValueError, match="configuration changed"):
            WatchEngine(changed).monitors[name].restore(model)


# --- the profile, end to end --------------------------------------------------------------


def test_the_example_profile_names_every_kind_and_the_synthetic_run_opens_no_finding():
    config = load_config(PROFILE / "watch.yaml")
    kinds = {m.kind for m in config.monitors.values()}
    assert kinds == {"envelope", "drift", "ratio", "counter", "whole_car"}
    assert config.monitors["whole_car"].baseline == "stint_start"
    catalog = yaml.safe_load((PROFILE / "catalog.yaml").read_text())["channels"]
    engine = WatchEngine(config)
    car = Car()
    extra = {c: 10.0 for c in engine.input_channels if c not in UNITS and c != "lap.event"}
    extra.update({"car.ambient_air_temp": 293, "car.injection_stage1_duty": 40})
    original = car.readings

    def readings(t):
        values = original(t)
        values.update(extra)
        return values

    car.readings = readings
    units = dict(UNITS)
    units.update({c: catalog[c].get("units", "") for c in extra})
    findings = {}
    for t in range(6 * LAP_S):
        if t % LAP_S == 0 and t > 0:
            engine.observe(
                "lap.event",
                json.dumps(
                    dict(
                        type="lap_completed", lap_number=t // LAP_S, valid=True, pit_status="track"
                    )
                ),
                t - 0.02,
            )
        engine.observe("lap.event", json.dumps(dict(pit_status="track")), t - 0.01)
        for channel, value in car.readings(t).items():
            engine.observe(channel, value, t - 0.01, units[channel])
        for name, row in engine.tick(t).items():
            if row["finding"]:
                findings[name] = row["finding"]
    assert not findings
    assert engine.monitors["trigger_errors"].frozen
    assert engine.monitors["driveline_ratio"].frozen


# --- stint re-learning in the service -----------------------------------------------


def test_a_driver_change_restarts_only_the_stint_start_monitors(tmp_path, monkeypatch):
    cfg = whole_car_config(baseline="stint_start")
    cfg.monitors["oil"] = drift_config().monitors["oil_drift"]
    (tmp_path / "watch.yaml").write_text(yaml.safe_dump(cfg.model_dump()))
    (tmp_path / "catalog.yaml").write_text((PROFILE / "catalog.yaml").read_text())
    service = WatchService(Settings(tmp_path / "watch.yaml", "v", "unused"))
    stint = {"number": 1}
    closed: list[tuple] = []
    monkeypatch.setattr(service.store, "session", lambda *args: "s")
    monkeypatch.setattr(service.store, "stint", lambda *args: stint["number"])
    monkeypatch.setattr(service.store, "load", lambda *args: {})
    monkeypatch.setattr(
        service.store, "close_previous", lambda *args, **kwargs: closed.append((args, kwargs))
    )
    service._context(10)
    assert service.stint == 1
    assert service.engine.monitors["whole_car"].source_stint == 1
    assert service._stints() == {"whole_car": 1, "oil": 0}
    whole_car, drift = service.engine.monitors["whole_car"], service.engine.monitors["oil"]
    whole_car.learned, drift.learned = 50, 2
    stint["number"] = 2
    service._context(20)
    assert service.session == "s" and service.stint == 2
    assert service.engine.monitors["oil"] is drift and drift.learned == 2
    replaced = service.engine.monitors["whole_car"]
    assert replaced is not whole_car and replaced.learned == 0 and replaced.source_stint == 2
    assert closed[-1][1] == {"reason": "stint_changed"} and closed[-1][0][1] == ["whole_car"]
