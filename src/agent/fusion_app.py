"""In-agent IMU/GNSS fusion fed by the pre-RBE tap.

Like ``LapTimingApp``, this runs on the pipeline thread and sees its input
channels at full rate before RBE (``docs/FUSION.md``). It reassembles the
per-signal samples into IMU frames and GNSS fixes, drives
``fusion.engine.FusionEngine``, and emits ``fused.*`` derived samples that
re-enter the pipeline -- so lap timing can subscribe to ``fused.*`` and
time on fused position at IMU rate, bridging short GNSS dropouts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from core.catalog import DerivedChannel, RuntimeCatalog
from core.config import ACCEL_UNITS, GYRO_UNITS, SPEED_UNITS, CatalogConfig, FusionConfig
from core.pb import telemetry_pb2 as pb
from core.samples import Sample
from fusion.engine import EngineSettings, FusionEngine, FusionOutput
from fusion.ins import ImuNoise, quat_from_euler, quat_to_dcm

# Appended after the agent's other derived channels so their IDs never move.
FUSION_DERIVED_CHANNELS = (
    DerivedChannel("fused.status", pb.STRING),
    DerivedChannel("fused.lat", pb.DOUBLE, "deg"),
    DerivedChannel("fused.lon", pb.DOUBLE, "deg"),
    DerivedChannel("fused.alt", pb.DOUBLE, "m"),
    DerivedChannel("fused.speed", pb.DOUBLE, "km/h"),
    DerivedChannel("fused.course", pb.DOUBLE, "deg"),
    DerivedChannel("fused.vel_up", pb.DOUBLE, "m/s"),
    DerivedChannel("fused.roll", pb.DOUBLE, "deg"),
    DerivedChannel("fused.pitch", pb.DOUBLE, "deg"),
    DerivedChannel("fused.yaw", pb.DOUBLE, "deg"),
    DerivedChannel("fused.yaw_rate", pb.DOUBLE, "deg/s"),
    DerivedChannel("fused.accel_long", pb.DOUBLE, "g"),
    DerivedChannel("fused.accel_lat", pb.DOUBLE, "g"),
    DerivedChannel("fused.accel_vert", pb.DOUBLE, "g"),
    DerivedChannel("fused.slip_angle", pb.DOUBLE, "deg"),
    DerivedChannel("fused.grade", pb.DOUBLE, "%"),
    DerivedChannel("fused.pos_std", pb.DOUBLE, "m"),
    DerivedChannel("fused.gnss_age", pb.DOUBLE, "s"),
)

# Two halves of one IMU reading (accel and gyro come in separate CAN frames)
# further apart than this are not the same reading.
_MAX_PAIR_SKEW_NS = 20_000_000

_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}
_FIX_ROLES = ("lat", "lon", "speed", "course")
_STD_ROLES = ("lat_std", "lon_std", "alt_std")


@dataclass(slots=True)
class _Vector:
    """Three per-axis samples of one frame, reassembled by capture stamp."""

    t_mono_ns: int = -1
    t_wall_ms: float = 0.0
    values: list[float | None] | None = None
    fresh: bool = False

    def offer(self, axis: int, sample: Sample) -> bool:
        if sample.t_mono_ns != self.t_mono_ns or self.values is None:
            self.t_mono_ns, self.t_wall_ms = sample.t_mono_ns, sample.t_wall_ms
            self.values = [None, None, None]
        self.values[axis] = float(sample.value)  # type: ignore[arg-type]
        if all(v is not None for v in self.values):
            self.fresh = True
            return True
        return False


class FusionApp:
    """Host the fusion engine on the pipeline thread; not thread-safe."""

    def __init__(
        self, catalog: RuntimeCatalog, catalog_config: CatalogConfig, config: FusionConfig
    ) -> None:
        roles = config.input_channels()
        channels = catalog_config.channels
        self._role_by_id: dict[int, str] = {}
        for role, name in roles.items():
            channel_id = catalog.channel_ids.get(name)
            if channel_id is None:
                raise ValueError(f"apps.fusion: channel {name!r} is not in the registry")
            self._role_by_id[channel_id] = role
        self.subscribed_channel_ids = frozenset(self._role_by_id)

        self._accel_scale = ACCEL_UNITS[channels[config.imu.accel[0]].units]
        self._gyro_scale = GYRO_UNITS[channels[config.imu.gyro[0]].units]
        self._speed_scale = SPEED_UNITS[channels[config.gnss.speed].units]
        self._mount = _imu_to_vehicle(config.imu.axes, config.imu.mount_rpy_deg)
        self._needs_mode = config.gnss.mode is not None

        noise = config.noise
        self.engine = FusionEngine(
            EngineSettings(
                antenna_lever_arm=config.gnss.lever_arm_m,
                latency_s=config.gnss.latency_ms / 1000.0,
                align_speed_mps=config.align_speed_kmh / 3.6,
                max_outage_s=config.max_outage_s,
                noise=ImuNoise(
                    accel_noise=noise.accel_noise,
                    gyro_noise=noise.gyro_noise,
                    accel_bias_rw=noise.accel_bias_rw,
                    gyro_bias_rw=noise.gyro_bias_rw,
                ),
            )
        )
        self._emit_period_ns = round(1e9 / config.output_rate_hz)
        self._last_emit_ns: int | None = None
        self._last_status: str | None = None
        self._accel = _Vector()
        self._gyro = _Vector()
        self._fix: dict[str, Sample] = {}
        self._std: dict[str, Sample] = {}

    def observe(self, channel_id: int, sample: Sample) -> list[Sample]:
        """Feed one pre-RBE sample; returns derived samples to re-enter."""
        role = self._role_by_id.get(channel_id)
        if role is None:
            return []
        kind, _, axis = role.partition(".")
        if kind == "accel":
            if self._accel.offer(int(axis), sample):
                return self._maybe_step()
            return []
        if kind == "gyro":
            if self._gyro.offer(int(axis), sample):
                return self._maybe_step()
            return []
        try:
            self._observe_gnss(role, sample)
        except (TypeError, ValueError):
            pass  # a mis-typed value costs one fix, never the pipeline
        return []

    # ---- IMU ----------------------------------------------------------------

    def _maybe_step(self) -> list[Sample]:
        accel, gyro = self._accel, self._gyro
        if not (accel.fresh and gyro.fresh):
            return []
        if abs(accel.t_mono_ns - gyro.t_mono_ns) > _MAX_PAIR_SKEW_NS:
            # Keep whichever is newer and wait for its partner.
            (gyro if accel.t_mono_ns > gyro.t_mono_ns else accel).fresh = False
            return []
        accel.fresh = gyro.fresh = False
        later = accel if accel.t_mono_ns >= gyro.t_mono_ns else gyro
        f = self._mount @ (np.asarray(accel.values) * self._accel_scale)
        w = self._mount @ (np.asarray(gyro.values) * self._gyro_scale)
        self.engine.imu(later.t_mono_ns / 1e9, f, w)
        return self._outputs(later.t_mono_ns, later.t_wall_ms)

    def _outputs(self, t_mono_ns: int, t_wall_ms: float) -> list[Sample]:
        emitted: list[Sample] = []

        def emit(name: str, value: object) -> None:
            emitted.append(Sample(f"derived:fused.{name}", t_mono_ns, t_wall_ms, value))

        out = self.engine.output()
        status = out.status if out is not None else "aligning"
        if status != self._last_status:
            self._last_status = status
            emit("status", status)
        if out is None:
            return emitted
        last = self._last_emit_ns
        if last is not None and t_mono_ns - last < self._emit_period_ns - 1_000_000:
            return emitted
        self._last_emit_ns = t_mono_ns
        _emit_output(out, emit)
        return emitted

    # ---- GNSS ---------------------------------------------------------------

    def _observe_gnss(self, role: str, sample: Sample) -> None:
        t = sample.t_mono_ns / 1e9
        if role == "alt":
            self.engine.gnss_alt(t, float(sample.value))  # type: ignore[arg-type]
            return
        if role in _STD_ROLES:
            self._std[role] = sample
            if _complete(self._std, _STD_ROLES, sample.t_mono_ns):
                values = [float(self._std[r].value) for r in _STD_ROLES]  # type: ignore[arg-type]
                self.engine.gnss_std(t, *values)
                self._std.clear()
            return
        self._fix[role] = sample
        roles = (*_FIX_ROLES, "mode") if self._needs_mode else _FIX_ROLES
        if not _complete(self._fix, roles, sample.t_mono_ns):
            return
        fix = self._fix
        self.engine.gnss_fix(
            t,
            float(fix["lat"].value),  # type: ignore[arg-type]
            float(fix["lon"].value),  # type: ignore[arg-type]
            float(fix["speed"].value) * self._speed_scale,  # type: ignore[arg-type]
            float(fix["course"].value),  # type: ignore[arg-type]
            str(fix["mode"].value) if self._needs_mode else "A",
        )
        self._fix = {}


def _complete(parts: dict[str, Sample], roles: tuple[str, ...], t_mono_ns: int) -> bool:
    return all(r in parts and parts[r].t_mono_ns == t_mono_ns for r in roles)


def _emit_output(out: FusionOutput, emit) -> None:
    if out.lat is not None and out.lon is not None:
        # Same stamp on both: lap timing pairs lat/lon by capture time.
        emit("lat", out.lat)
        emit("lon", out.lon)
    if out.alt is not None:
        emit("alt", out.alt)
    emit("speed", out.speed_mps * 3.6)
    emit("course", out.course_deg)
    emit("vel_up", out.vel_up_mps)
    emit("roll", out.roll_deg)
    emit("pitch", out.pitch_deg)
    emit("yaw", out.yaw_deg)
    emit("yaw_rate", out.yaw_rate_dps)
    emit("accel_long", out.accel_long_g)
    emit("accel_lat", out.accel_lat_g)
    emit("accel_vert", out.accel_vert_g)
    if out.slip_angle_deg is not None:
        emit("slip_angle", out.slip_angle_deg)
    if out.grade_pct is not None:
        emit("grade", out.grade_pct)
    emit("pos_std", out.pos_std_m)
    if math.isfinite(out.gnss_age_s):
        emit("gnss_age", out.gnss_age_s)


def _imu_to_vehicle(axes: tuple[str, str, str], mount_rpy_deg: tuple[float, float, float]):
    """Rotation taking an IMU-frame vector to the vehicle FRD frame."""
    remap = np.zeros((3, 3))
    for row, axis in enumerate(axes):
        remap[row, _AXIS_INDEX[axis[-1]]] = -1.0 if axis.startswith("-") else 1.0
    if np.linalg.det(remap) < 0:
        raise ValueError(f"apps.fusion.imu.axes {list(axes)} is a mirror, not a rotation")
    roll, pitch, yaw = (math.radians(a) for a in mount_rpy_deg)
    return quat_to_dcm(quat_from_euler(roll, pitch, yaw)) @ remap
