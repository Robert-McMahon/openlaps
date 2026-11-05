"""Alignment, GNSS weighting, outage handling and vehicle-dynamics outputs.

``FusionEngine`` wraps ``ErrorStateIns`` with everything a car needs around
the bare filter:

* **Alignment.** Roll and pitch come from the accelerometer while the car
  sits still; yaw is unobservable at rest, so the filter initialises on the
  first fix above ``align_speed_mps`` with yaw seeded from GNSS course.
* **GNSS weighting.** Each fix is weighted by the receiver's own GST error
  estimate when one is fresh, else by its RMC fix mode.
* **Latency.** A fix describes the car ``latency_s`` before it reached the
  agent; the filter compares it with its own state extrapolated back to
  that epoch, which at 40 m/s is worth ~1.6 m per 40 ms of latency.
* **Outliers.** Fixes failing a chi-square gate are dropped; a persistent
  disagreement (a real jump, e.g. RTK float -> fixed) forces a re-anchor.
* **Outages.** Through a dropout the filter dead-reckons on the IMU alone.
  Past ``max_outage_s`` it stops reporting position and re-aligns when
  GNSS returns.
* **IMU failure.** An IMU that goes silent, freezes, or reports impossible
  values drops the filter to ``gnss_only``: position, speed and course pass
  straight through from the receiver (moved from the antenna to the IMU
  position by course), and the attitude-derived outputs go quiet. Once the
  IMU has been healthy for ``imu_recovery_s`` the filter re-aligns.
  Before the first alignment the same passthrough runs with status
  ``aligning``, so fused position exists from the first fix.

Everything is driven by the caller's timestamps (seconds, monotonic).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np

from fusion.geodesy import LocalFrame
from fusion.ins import BG, ErrorStateIns, ImuNoise, V, dcm_to_euler, quat_from_euler

logger = logging.getLogger(__name__)

# RMC posMode -> (horizontal 1-sigma m, vertical 1-sigma m, velocity 1-sigma m/s)
# when no fresh GST estimate is available. Deliberately conservative.
MODE_SIGMAS = {
    "R": (0.03, 0.05, 0.05),  # RTK fixed
    "F": (0.4, 0.6, 0.08),  # RTK float
    "D": (0.8, 1.5, 0.12),  # DGNSS / SBAS
    "A": (2.5, 4.0, 0.15),  # autonomous / SPP
    "E": (10.0, 15.0, 1.0),  # receiver's own dead reckoning
}
MODE_STATUS = {"R": "rtk_fixed", "F": "rtk_float", "D": "dgnss", "A": "gnss", "E": "gnss"}

# Beyond any automotive IMU's range: a reading past these is a fault, not a
# hard kerb strike (the FDI's DBC range is +/-33 g and +/-1877 deg/s).
MAX_SPECIFIC_FORCE = 30.0 * 9.80665
MAX_RATE = math.radians(1800.0)
# On a moving car, vibration keeps every axis changing; this many
# bit-identical readings in a row means the sensor (or the frames carrying
# it) has frozen. Parked, a quiet sensor can legitimately repeat the same
# quantised CAN value, so the check only runs above FROZEN_CHECK_SPEED.
FROZEN_SAMPLES = 50
FROZEN_CHECK_SPEED = 3.0  # m/s

CHI2_2DOF = 13.8  # 99.9 %
CHI2_1DOF = 10.8


@dataclass(frozen=True, slots=True)
class EngineSettings:
    antenna_lever_arm: tuple[float, float, float] = (0.0, 0.0, 0.0)
    latency_s: float = 0.03
    align_speed_mps: float = 5.0
    max_outage_s: float = 10.0
    gst_max_age_s: float = 1.0
    min_course_speed_mps: float = 1.0
    imu_timeout_s: float = 0.25
    imu_recovery_s: float = 1.0
    noise: ImuNoise = field(default_factory=ImuNoise)
    # Floors on reported receiver error: GST is often optimistic.
    min_horizontal_sigma: float = 0.015
    min_vertical_sigma: float = 0.03


@dataclass(frozen=True, slots=True)
class FusionOutput:
    """
    One snapshot of the fused state. In ``gnss_only`` and ``aligning``
    (passthrough) the attitude-derived fields are None.
    """

    status: str
    lat: float | None
    lon: float | None
    alt: float | None
    speed_mps: float
    course_deg: float
    vel_up_mps: float | None
    roll_deg: float | None
    pitch_deg: float | None
    yaw_deg: float | None
    yaw_rate_dps: float | None
    accel_long_g: float | None
    accel_lat_g: float | None
    accel_vert_g: float | None
    slip_angle_deg: float | None
    grade_pct: float | None
    pos_std_m: float
    yaw_std_deg: float | None
    gnss_age_s: float
    gyro_bias_dps: tuple[float, float, float] | None


@dataclass(slots=True)
class EngineStats:
    imu_steps: int = 0
    imu_gaps: int = 0
    fixes: int = 0
    fixes_rejected: int = 0
    forced_reanchors: int = 0
    alignments: int = 0
    outages_lost: int = 0
    imu_failovers: int = 0
    imu_bad_samples: int = 0


class FusionEngine:
    """Owns the filter's lifecycle; see the module docstring."""

    def __init__(self, settings: EngineSettings | None = None) -> None:
        self.settings = settings or EngineSettings()
        self.stats = EngineStats()
        self._arm = np.asarray(self.settings.antenna_lever_arm, dtype=float)
        self._frame: LocalFrame | None = None
        self._ins: ErrorStateIns | None = None
        self._t_imu: float | None = None
        self._last_f: np.ndarray | None = None
        self._last_fix_t: float | None = None
        self._last_mode = "A"
        self._gst: tuple[float, float, float, float] | None = None  # t, lat, lon, alt std
        self._have_alt_ref = False
        self._rejected_since: float | None = None
        self._last_nhc_t = -math.inf
        # Static levelling: running mean of specific force while still.
        self._level_sum = np.zeros(3)
        self._level_n = 0
        self._gyro_sum = np.zeros(3)
        self._still = False
        # IMU health: a fault latches until this many seconds of good data.
        self._imu_fault = False
        self._imu_good_since: float | None = None
        self._frozen_run = 0
        self._prev_raw: tuple[bytes, bytes] | None = None
        # Last raw fix and height, for passthrough output.
        self._fix: tuple[float, float, float, float, float, str] | None = None
        self._last_alt: float | None = None
        self._heading_deg: float | None = None

    # ---- state --------------------------------------------------------------

    @property
    def aligned(self) -> bool:
        return self._ins is not None

    @property
    def ins(self) -> ErrorStateIns | None:
        return self._ins

    def imu_healthy(self, now: float) -> bool:
        """IMU data is current, plausible and not latched faulty."""
        if self._imu_fault or self._t_imu is None:
            return False
        return now - self._t_imu <= self.settings.imu_timeout_s

    # ---- inputs -------------------------------------------------------------

    def imu(self, t: float, f_b: np.ndarray, w_b: np.ndarray) -> None:
        """One IMU sample: specific force m/s^2 and body rate rad/s (FRD)."""
        if not self._imu_sample_ok(t, f_b, w_b):
            return
        previous, self._t_imu = self._t_imu, t
        if previous is not None and t - previous > self.settings.imu_timeout_s:
            # Back from silence: the gap is not something to integrate across,
            # and recovery time counts from now.
            self._fail_over()
            self._imu_good_since = t
            previous = None
        self._last_f = f_b
        self._track_stillness(f_b, w_b)
        ins = self._ins
        if ins is None or previous is None:
            return
        dt = t - previous
        if dt <= 0.0:
            return
        if dt > 0.1:
            # A CAN gap: propagating one huge step would be wildly wrong, but
            # skipping it loses the motion in between. Integrate in pieces on
            # the last reading and let the covariance growth say how unsure
            # that makes us.
            self.stats.imu_gaps += 1
            steps = min(50, math.ceil(dt / 0.01))
            for _ in range(steps):
                ins.propagate(f_b, w_b, dt / steps)
        else:
            ins.propagate(f_b, w_b, dt)
        self.stats.imu_steps += 1
        self._check_outage(t)
        self._constraints(t)

    def gnss_std(self, t: float, lat_std: float, lon_std: float, alt_std: float) -> None:
        self._gst = (t, lat_std, lon_std, alt_std)

    def gnss_fix(
        self,
        t: float,
        lat: float,
        lon: float,
        speed_mps: float,
        course_deg: float,
        mode: str = "A",
    ) -> None:
        """One horizontal fix with Doppler speed and course (RMC)."""
        mode = mode if mode in MODE_SIGMAS else "A"
        self._last_mode = mode
        self._fix = (t, lat, lon, speed_mps, course_deg, mode)
        if speed_mps > 2.0:
            self._heading_deg = course_deg  # course is noise when crawling
        if self._frame is None:
            self._frame = LocalFrame(lat, lon, 0.0)
        if self._ins is not None and not self.imu_healthy(t):
            self._fail_over()
        if self._ins is None:
            if self.imu_healthy(t) and self._imu_recovered(t):
                self._try_align(t, lat, lon, speed_mps, course_deg, mode)
            return
        self.stats.fixes += 1
        ins = self._ins
        lag = self._lag(t)
        sig_h, _, sig_v = self._sigmas(t, mode)

        # Coming back from an outage the state has drifted by however much
        # it drifted; the covariance says how unsure that is, so take the
        # first fix back ungated rather than reject it as an outlier.
        recovering = self._last_fix_t is not None and t - self._last_fix_t > 1.0
        gate = None if recovering else CHI2_2DOF

        z = self._frame.to_ned(lat, lon)[:2]
        predicted = ins.antenna_position(self._arm, lag)[:2]
        h = ins.position_jacobian(self._arm)[:2]
        accepted, _ = ins.update(z - predicted, h, np.full(2, sig_h**2), gate=gate)
        if accepted:
            self._rejected_since = None
            self._last_fix_t = t
        else:
            self.stats.fixes_rejected += 1
            if self._rejected_since is None:
                self._rejected_since = t
            elif t - self._rejected_since > 1.0:
                # A second of consistent disagreement is the receiver moving
                # its solution (float -> fixed, a reconverge), not multipath.
                self._reanchor(z - predicted, sig_h)
                ins.update(z - ins.antenna_position(self._arm, lag)[:2], h, np.full(2, sig_h**2))
                self.stats.forced_reanchors += 1
                self._rejected_since = None
                self._last_fix_t = t
            return

        if speed_mps >= self.settings.min_course_speed_mps:
            course = math.radians(course_deg)
            zv = np.array([speed_mps * math.cos(course), speed_mps * math.sin(course)])
            pv = ins.antenna_velocity(self._arm, lag)[:2]
            hv = ins.velocity_jacobian(self._arm)[:2]
            ins.update(zv - pv, hv, np.full(2, sig_v**2), gate=gate)

    def gnss_alt(self, t: float, alt: float) -> None:
        """Height from GGA (any consistent datum; the frame is relative)."""
        self._last_alt = alt
        ins = self._ins
        if ins is None or self._frame is None:
            return
        if not self._have_alt_ref:
            # Anchor the frame's height so the current estimate is the
            # measurement: the filter has no vertical reference before this.
            antenna_down = ins.antenna_position(self._arm, self._lag(t))[2]
            self._frame = LocalFrame(self._frame.lat0, self._frame.lon0, alt + antenna_down)
            self._have_alt_ref = True
            return
        _, sig_z, _ = self._sigmas(t, self._last_mode)
        lag = self._lag(t)
        down = self._frame.h0 - alt
        predicted = ins.antenna_position(self._arm, lag)[2]
        h = ins.position_jacobian(self._arm)[2:3]
        ins.update(np.array([down - predicted]), h, np.array([sig_z**2]), gate=CHI2_1DOF)

    # ---- outputs ------------------------------------------------------------

    def output(self) -> FusionOutput | None:
        ins = self._ins
        if ins is None or self._t_imu is None:
            return self._passthrough()
        gnss_age = self._t_imu - self._last_fix_t if self._last_fix_t is not None else math.inf
        r = ins.dcm
        roll, pitch, yaw = dcm_to_euler(r)
        v = ins.v
        speed = float(math.hypot(v[0], v[1]))
        course = math.atan2(v[1], v[0])
        # Kinematic acceleration in the body frame: specific force minus the
        # body-frame gravity component, i.e. what the car is actually doing.
        a_b = r.T @ ins.a_n
        g0 = 9.80665
        slip = grade = None
        if speed > 3.0:
            slip = math.degrees(_wrap(course - yaw))
            grade = -100.0 * float(v[2]) / speed
        cov = ins.cov
        pos_std = math.sqrt(max(0.0, cov[0, 0] + cov[1, 1]))
        yaw_std = math.degrees(math.sqrt(max(0.0, cov[8, 8])))
        status = "dead_reckoning" if gnss_age > 0.5 else MODE_STATUS.get(self._last_mode, "gnss")
        lat, lon, h = self._frame.to_geodetic(ins.p)
        return FusionOutput(
            status=status,
            lat=lat,
            lon=lon,
            alt=h if self._have_alt_ref else None,
            speed_mps=float(np.linalg.norm(v)),
            course_deg=math.degrees(course) % 360.0,
            vel_up_mps=-float(v[2]),
            roll_deg=math.degrees(roll),
            pitch_deg=math.degrees(pitch),
            yaw_deg=math.degrees(yaw) % 360.0,
            yaw_rate_dps=math.degrees(float(ins.omega_b[2])),
            accel_long_g=float(a_b[0]) / g0,
            accel_lat_g=float(a_b[1]) / g0,
            accel_vert_g=-float(a_b[2]) / g0,
            slip_angle_deg=slip,
            grade_pct=grade,
            pos_std_m=pos_std,
            yaw_std_deg=yaw_std,
            gnss_age_s=gnss_age,
            gyro_bias_dps=tuple(math.degrees(b) for b in ins.bg),  # type: ignore[arg-type]
        )

    def _passthrough(self) -> FusionOutput | None:
        """GNSS-only output: the last fix, moved to the IMU and to the present."""
        fix = self._fix
        if fix is None or self._frame is None:
            return None
        t, lat, lon, speed, course_deg, mode = fix
        if self._t_imu is not None and self._t_imu - t > 0.5:
            return None  # no current fix to pass through
        # The fix is ``latency_s`` old on arrival: project it along its own
        # velocity to the time it is reported at (~1.2 m at 40 m/s, 30 ms).
        course = math.radians(course_deg)
        lead = speed * self.settings.latency_s
        ned = self._frame.to_ned(lat, lon) + lead * np.array(
            [math.cos(course), math.sin(course), 0]
        )
        # Without attitude, take heading from the last course seen at speed
        # (sideslip ignored) and the car as level: a few cm on a ~1 m arm.
        # Before the car has ever moved, heading is unknown: stay on the antenna.
        arm = self._arm
        if self._heading_deg is not None:
            heading = math.radians(self._heading_deg)
            cy, sy = math.cos(heading), math.sin(heading)
            ned -= np.array([cy * arm[0] - sy * arm[1], sy * arm[0] + cy * arm[1], 0.0])
        lat_i, lon_i, _ = self._frame.to_geodetic(np.array([ned[0], ned[1], 0.0]))
        alt = self._last_alt + arm[2] if self._last_alt is not None else None
        sig_h, _, _ = self._sigmas(t, mode)
        status = "aligning" if self.imu_healthy(t) else "gnss_only"
        return FusionOutput(
            status=status,
            lat=lat_i,
            lon=lon_i,
            alt=alt,
            speed_mps=speed,
            course_deg=course_deg % 360.0,
            vel_up_mps=None,
            roll_deg=None,
            pitch_deg=None,
            yaw_deg=None,
            yaw_rate_dps=None,
            accel_long_g=None,
            accel_lat_g=None,
            accel_vert_g=None,
            slip_angle_deg=None,
            grade_pct=None,
            pos_std_m=sig_h * math.sqrt(2.0),
            yaw_std_deg=None,
            gnss_age_s=0.0,
            gyro_bias_dps=None,
        )

    # ---- internals ----------------------------------------------------------

    def _imu_sample_ok(self, t: float, f_b: np.ndarray, w_b: np.ndarray) -> bool:
        """Screen one IMU reading; latch a fault on impossible or frozen data."""
        bad = not (np.all(np.isfinite(f_b)) and np.all(np.isfinite(w_b)))
        if not bad:
            bad = (
                float(np.linalg.norm(f_b)) > MAX_SPECIFIC_FORCE
                or float(np.linalg.norm(w_b)) > MAX_RATE
            )
        raw = (np.asarray(f_b).tobytes(), np.asarray(w_b).tobytes())
        moving = self._fix is not None and self._fix[3] > FROZEN_CHECK_SPEED
        if not bad and moving and raw == self._prev_raw:
            self._frozen_run += 1
            bad = self._frozen_run >= FROZEN_SAMPLES
        else:
            self._frozen_run = 0
        self._prev_raw = raw
        if bad:
            self.stats.imu_bad_samples += 1
            self._imu_good_since = None
            if not self._imu_fault:
                self._imu_fault = True
                self._fail_over()
            return False
        if self._imu_good_since is None:
            self._imu_good_since = t
        if self._imu_fault and t - self._imu_good_since >= self.settings.imu_recovery_s:
            logger.info("fusion: IMU healthy again; re-aligning")
            self._imu_fault = False
        return True

    def _imu_recovered(self, now: float) -> bool:
        since = self._imu_good_since
        return since is not None and (
            self.stats.imu_failovers == 0 or now - since >= self.settings.imu_recovery_s
        )

    def _fail_over(self) -> None:
        """Drop the inertial solution; output falls back to GNSS passthrough."""
        if self._ins is None:
            return
        logger.warning("fusion: IMU lost; falling back to GNSS only")
        self._ins = None
        self._have_alt_ref = False
        self._level_sum[:] = 0.0
        self._gyro_sum[:] = 0.0
        self._level_n = 0
        self.stats.imu_failovers += 1

    def _lag(self, t_meas: float) -> float:
        """How far the filter's state is ahead of the measurement epoch."""
        t_state = self._t_imu if self._t_imu is not None else t_meas
        return t_state - (t_meas - self.settings.latency_s)

    def _sigmas(self, t: float, mode: str) -> tuple[float, float, float]:
        sig_h, sig_z, sig_v = MODE_SIGMAS[mode]
        gst = self._gst
        if gst is not None and t - gst[0] <= self.settings.gst_max_age_s:
            sig_h = max(math.hypot(gst[1], gst[2]) / math.sqrt(2.0), 1e-3)
            sig_z = gst[3]
        s = self.settings
        return max(sig_h, s.min_horizontal_sigma), max(sig_z, s.min_vertical_sigma), sig_v

    def _track_stillness(self, f_b: np.ndarray, w_b: np.ndarray) -> None:
        g = self._frame.gravity if self._frame is not None else 9.8
        still = abs(float(np.linalg.norm(f_b)) - g) < 0.4 and float(
            np.linalg.norm(w_b)
        ) < math.radians(2.0)
        if still and self._ins is None:
            if self._level_n >= 500:  # ~5 s at 100 Hz: a sliding-ish window
                self._level_sum *= 0.5
                self._gyro_sum *= 0.5
                self._level_n //= 2
            self._level_sum += f_b
            self._gyro_sum += w_b
            self._level_n += 1
        self._still = still

    def _try_align(
        self, t: float, lat: float, lon: float, speed: float, course_deg: float, mode: str
    ) -> None:
        if speed < self.settings.align_speed_mps or self._t_imu is None:
            return
        assert self._frame is not None
        if self._level_n >= 50:
            f = self._level_sum / self._level_n
            if f[2] > 0.5 * self._frame.gravity:
                logger.warning(
                    "fusion: IMU down axis reads %+.2f g parked; expected about -1 g. "
                    "Check apps.fusion.imu.axes",
                    f[2] / 9.80665,
                )
            roll = math.atan2(-f[1], -f[2])
            pitch = math.atan2(f[0], math.hypot(f[1], f[2]))
            sig_tilt = math.radians(1.0)
            bg = self._gyro_sum / self._level_n
        else:
            roll = pitch = 0.0
            sig_tilt = math.radians(4.0)
            bg = None
        course = math.radians(course_deg)
        yaw = course  # sideslip unknown at alignment; it sits inside sigma_yaw
        q = quat_from_euler(roll, pitch, yaw)
        sig_h, _, sig_v = self._sigmas(t, mode)
        ins = ErrorStateIns(self._frame.gravity, self.settings.noise)
        # The fix is ``lag`` old: carry it forward to the state's epoch, or the
        # filter spends its first second blaming that offset on accel bias
        # (which trades one-for-one with pitch on a straight).
        lag = self._lag(t)
        a_long = float(self._last_f[0]) if self._last_f is not None else 0.0
        heading = np.array([math.cos(course), math.sin(course), 0.0])
        v = heading * (speed + a_long * lag)
        p = self._frame.to_ned(lat, lon) + heading * (speed * lag + 0.5 * a_long * lag * lag)
        ins.initialize(
            p=p,
            v=v,
            q=q,
            sigma_p=(sig_h, sig_h, 50.0),
            sigma_v=max(sig_v, 0.3),
            sigma_att=(sig_tilt, sig_tilt, math.radians(6.0)),
            sigma_bg=math.radians(0.1 if bg is not None else 0.5),
            sigma_ba=0.1,
            bg=bg,
        )
        # The antenna, not the IMU, is at the fix: move the IMU there.
        ins.p = p - ins.dcm @ self._arm
        self._ins = ins
        self._last_fix_t = t
        self._have_alt_ref = False
        self.stats.alignments += 1

    def _reanchor(self, residual: np.ndarray, sigma: float) -> None:
        ins = self._ins
        assert ins is not None
        extra = np.square(residual) + sigma**2
        ins.cov[0, 0] += extra[0]
        ins.cov[1, 1] += extra[1]
        # A position that was wrong for a second says velocity and heading
        # were too; open those up so the next fixes can pull them back.
        ins.cov[3, 3] += 1.0
        ins.cov[4, 4] += 1.0
        ins.cov[8, 8] += math.radians(3.0) ** 2

    def _check_outage(self, t: float) -> None:
        if self._last_fix_t is None:
            return
        if t - self._last_fix_t > self.settings.max_outage_s:
            # Too long on inertial alone to trust: drop back to aligning.
            self._ins = None
            self._level_sum[:] = 0.0
            self._gyro_sum[:] = 0.0
            self._level_n = 0
            self._have_alt_ref = False
            self.stats.outages_lost += 1

    def _constraints(self, t: float) -> None:
        """Pseudo-measurements at 10 Hz.

        No non-holonomic ("goes where it points") constraint: a race car's
        sideslip is the signal we want, and forcing lateral velocity to zero
        through a dropout corrupts heading more than it saves in drift.
        """
        ins = self._ins
        if ins is None or t - self._last_nhc_t < 0.1:
            return
        self._last_nhc_t = t
        speed = float(np.linalg.norm(ins.v))
        if self._still and speed < 0.5:
            # Parked: velocity is zero and the gyro reads its own bias.
            h = ins.body_velocity_jacobian()
            ins.update(-(ins.dcm.T @ ins.v), h, np.full(3, 0.02**2))
            hw = np.zeros((3, 15))
            hw[:, BG] = np.eye(3)
            ins.update(ins.omega_b.copy(), hw, np.full(3, math.radians(0.05) ** 2))
            return
        if not self._have_alt_ref:
            # No height source yet: hold vertical velocity loosely at zero so
            # the inertial vertical channel (unstable on its own) can't run off.
            hz = np.zeros((1, 15))
            hz[0, V.start + 2] = 1.0
            ins.update(np.array([-ins.v[2]]), hz, np.array([0.5**2]))


def _wrap(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi
