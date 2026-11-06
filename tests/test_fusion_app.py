"""FusionApp wiring: profile config, frame reassembly, units, axes, pipeline tap."""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import yaml
from pydantic import ValidationError

from agent.agent import agent_derived_channels
from agent.fusion_app import FusionApp
from agent.pipeline import Pipeline
from core.catalog import build_runtime_catalog
from core.config import CatalogConfig, load_profile
from core.samples import Sample
from fusion.simulate import SensorModel, lap_profile, simulate

EXAMPLE = Path(__file__).resolve().parents[1] / "profiles" / "example-club-racer"
G0 = 9.80665


@pytest.fixture(scope="module")
def profile():
    return load_profile(EXAMPLE)


@pytest.fixture
def catalog(profile, tmp_path):
    return build_runtime_catalog(
        profile,
        state_path=tmp_path / "registry.json",
        created_unix_ms=1,
        derived_channels=agent_derived_channels(["can0", "serial0"], fusion=True),
    )


def _catalog_dict() -> dict:
    return yaml.safe_load((EXAMPLE / "catalog.yaml").read_text(encoding="utf-8"))


def _samples(catalog, events, *, imu_rotation: np.ndarray | None = None):
    """Simulator events -> (channel_id, Sample) in catalog units, per signal."""
    ids = catalog.channel_ids
    out: list[tuple[int, Sample]] = []

    def add(name: str, t: float, value) -> None:
        t_ns = round(t * 1e9)
        out.append((ids[name], Sample(name, t_ns, t * 1000.0, value)))

    for event in events:
        t = event.t
        if event.kind == "imu":
            f, w = event.data
            if imu_rotation is not None:
                f, w = imu_rotation @ f, imu_rotation @ w
            # Accel and gyro arrive in separate CAN frames, 0.2 ms apart.
            for axis, value in zip("xyz", f, strict=True):
                add(f"car.accel_{axis}", t, float(value) / G0)
            for axis, value in zip("xyz", w, strict=True):
                add(f"car.gyro_{axis}", t + 0.0002, math.degrees(float(value)))
        elif event.kind == "fix":
            lat, lon, speed, course, mode = event.data
            for name, value in (
                ("position.lat", lat),
                ("position.lon", lon),
                ("position.speed", speed * 3.6),
                ("position.heading", course),
                ("position.fix_quality", mode),
            ):
                add(name, t, value)
        elif event.kind == "alt":
            add("position.alt", t, event.data[0])
        elif event.kind == "gst":
            for name, value in zip(
                ("position.lat_std", "position.lon_std", "position.alt_std"),
                event.data,
                strict=True,
            ):
                add(name, t, value)
    return out


# Simulate the antenna where the example profile says it is, so these tests
# follow the profile's lever arm rather than assume one.
SENSORS = SensorModel(
    lever_arm=tuple(
        yaml.safe_load((EXAMPLE / "catalog.yaml").read_text(encoding="utf-8"))["apps"]["fusion"][
            "gnss"
        ]["lever_arm_m"]
    )
)


def test_example_profile_configures_fusion_and_registers_fused_channels(profile, catalog):
    fusion = profile.catalog.apps.fusion
    assert fusion is not None
    assert fusion.gnss.lat == "position.lat"
    for name in ("fused.lat", "fused.lon", "fused.slip_angle", "fused.status"):
        assert name in catalog.channel_ids


def test_fused_channels_are_absent_without_fusion():
    names = {c.name for c in agent_derived_channels(["can0"])}
    assert not any(name.startswith("fused.") for name in names)


def test_fusion_input_must_be_a_catalog_channel():
    data = _catalog_dict()
    data["apps"]["fusion"]["imu"]["accel"][0] = "car.not_a_channel"
    with pytest.raises(ValidationError, match="car.not_a_channel"):
        CatalogConfig.model_validate(data)


def test_fusion_rejects_unconvertible_units():
    data = _catalog_dict()
    data["channels"]["car.gyro_z"]["units"] = "rpm"
    with pytest.raises(ValidationError, match="rpm"):
        CatalogConfig.model_validate(data)


def test_gst_channels_must_come_as_a_set():
    data = _catalog_dict()
    del data["apps"]["fusion"]["gnss"]["alt_std"]
    with pytest.raises(ValidationError, match="together"):
        CatalogConfig.model_validate(data)


def test_axes_must_be_a_proper_rotation(profile, catalog):
    data = _catalog_dict()
    data["apps"]["fusion"]["imu"]["axes"] = ["x", "y", "-z"]
    config = CatalogConfig.model_validate(data)
    with pytest.raises(ValueError, match="mirror"):
        FusionApp(catalog, config, config.apps.fusion)


def _run_app(catalog, config, sensors=SENSORS, duration=40.0, imu_rotation=None):
    events, truth, frame = simulate(lap_profile(), duration, sensors)
    app = FusionApp(catalog, config, config.apps.fusion)
    emitted: list[Sample] = []
    for channel_id, sample in _samples(catalog, events, imu_rotation=imu_rotation):
        emitted.extend(app.observe(channel_id, sample))
    return app, emitted, truth, frame


def _last_position_error(emitted, truth, frame) -> float:
    lat = [s for s in emitted if s.source_ref == "derived:fused.lat"][-1]
    lon = [s for s in emitted if s.source_ref == "derived:fused.lon"][-1]
    assert lat.t_mono_ns == lon.t_mono_ns
    i = truth.at(lat.t_mono_ns / 1e9)
    ned = frame.to_ned(lat.value, lon.value)
    return float(np.linalg.norm(ned[:2] - truth.p[i, :2]))


def test_app_reassembles_frames_and_emits_fused_channels(profile, catalog):
    app, emitted, truth, frame = _run_app(catalog, profile.catalog)

    statuses = [s.value for s in emitted if s.source_ref == "derived:fused.status"]
    assert statuses[0] == "aligning"
    assert statuses[-1] == "rtk_fixed"
    assert app.engine.stats.alignments == 1
    assert _last_position_error(emitted, truth, frame) < 0.1

    # Output is decimated to output_rate_hz (50) from the 100 Hz IMU.
    lats = [s for s in emitted if s.source_ref == "derived:fused.lat"]
    span_s = (lats[-1].t_mono_ns - lats[0].t_mono_ns) / 1e9
    assert len(lats) / span_s == pytest.approx(50.0, rel=0.05)

    names = {s.source_ref.removeprefix("derived:") for s in emitted}
    assert {"fused.slip_angle", "fused.accel_lat", "fused.grade", "fused.alt"} <= names


def test_upside_down_imu_is_corrected_by_the_axes_remap(catalog):
    data = _catalog_dict()
    data["apps"]["fusion"]["imu"]["axes"] = ["x", "-y", "-z"]
    config = CatalogConfig.model_validate(data)
    # The IMU's y and z point the other way from the vehicle's.
    flip = np.diag([1.0, -1.0, -1.0])
    app, emitted, truth, frame = _run_app(catalog, config, imu_rotation=flip)
    assert app.engine.stats.alignments == 1
    assert _last_position_error(emitted, truth, frame) < 0.1


def test_fused_output_reenters_the_pipeline_for_lap_timing(profile, catalog):
    class _Timing:
        def __init__(self, ids):
            self.subscribed_channel_ids = frozenset(ids)
            self.seen: list[Sample] = []

        def observe(self, channel_id, sample):
            self.seen.append(sample)
            return []

    timing = _Timing({catalog.channel_ids["fused.lat"], catalog.channel_ids["fused.lon"]})
    app = FusionApp(catalog, profile.catalog, profile.catalog.apps.fusion)
    pipeline = Pipeline(catalog, tick_ms=20, timing_app=timing, fusion_app=app)
    events, _, _ = simulate(lap_profile(), 15.0, replace(SENSORS))
    by_id = {cid: name for name, cid in catalog.channel_ids.items()}
    ref_by_name = {
        name: policy_ref
        for policy_ref, (cid, _) in catalog.source_map.items()
        for name in [by_id[cid]]
    }
    for channel_id, sample in _samples(catalog, events):
        source_ref = ref_by_name[by_id[channel_id]]
        pipeline.ingest("sim", replace(sample, source_ref=source_ref))
    assert len(timing.seen) > 100
    assert {s.source_ref for s in timing.seen} == {"derived:fused.lat", "derived:fused.lon"}


def test_imu_failure_keeps_fused_position_flowing_at_the_fix_rate(profile, catalog):
    sensors = replace(SENSORS, imu_outages=((25.0, 35.0),))
    app, emitted, truth, frame = _run_app(catalog, profile.catalog, sensors, duration=40.0)

    statuses = [s.value for s in emitted if s.source_ref == "derived:fused.status"]
    # Back from the fault it re-aligns (GNSS passthrough meanwhile), then fuses.
    assert statuses == ["aligning", "rtk_fixed", "gnss_only", "aligning", "rtk_fixed"]
    window = [
        s
        for s in emitted
        if s.source_ref == "derived:fused.lat" and 26.0 <= s.t_mono_ns / 1e9 < 34.0
    ]
    assert len(window) / 8.0 == pytest.approx(50.0, rel=0.05)
    # Attitude-derived channels go quiet rather than freezing on stale values.
    quiet = {
        s.source_ref
        for s in emitted
        if 26.0 <= s.t_mono_ns / 1e9 < 34.0 and s.source_ref.endswith(("roll", "slip_angle"))
    }
    assert quiet == set()
