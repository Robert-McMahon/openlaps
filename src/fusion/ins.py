"""15-state error-state Kalman filter for a strapdown IMU aided by GNSS.

Nominal state (propagated by the IMU, never linearised):
    p   position, local NED, m
    v   velocity, NED, m/s
    q   attitude, body(FRD) -> NED unit quaternion [w, x, y, z]
    bg  gyro bias, body, rad/s
    ba  accelerometer bias, body, m/s^2

Error state (what the Kalman filter estimates, 15 x 1):
    dp(3) dv(3) dtheta(3) dbg(3) dba(3)

Attitude error is defined in the navigation frame ("global" error):
``R_true = exp([dtheta]x) R_est``. That makes the measurement Jacobians
below short and keeps the propagation Jacobian free of the body rate.

Earth rotation and transport rate are ignored: over a circuit they are
smaller than any automotive MEMS gyro's noise. Gravity is the WGS-84 normal
value at the frame origin and is held constant.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

P, V, TH, BG, BA = slice(0, 3), slice(3, 6), slice(6, 9), slice(9, 12), slice(12, 15)
N_STATES = 15


@dataclass(frozen=True, slots=True)
class ImuNoise:
    """Continuous-time noise densities.

    Defaults suit an automotive-grade MEMS IMU hard-mounted in a race car:
    the white-noise terms are well above datasheet figures because engine
    and road vibration dominate in the car, not sensor noise.
    """

    accel_noise: float = 0.05  # m/s^2/sqrt(Hz), velocity random walk
    gyro_noise: float = 0.002  # rad/s/sqrt(Hz), angle random walk
    accel_bias_rw: float = 2e-3  # m/s^3/sqrt(Hz)
    gyro_bias_rw: float = 5e-5  # rad/s^2/sqrt(Hz)


def skew(v: np.ndarray) -> np.ndarray:
    return np.array([[0.0, -v[2], v[1]], [v[2], 0.0, -v[0]], [-v[1], v[0], 0.0]])


def quat_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return np.array(
        [
            aw * bw - ax * bx - ay * by - az * bz,
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
        ]
    )


def quat_from_rotvec(r: np.ndarray) -> np.ndarray:
    angle = math.sqrt(float(r @ r))
    if angle < 1e-12:
        q = np.array([1.0, 0.5 * r[0], 0.5 * r[1], 0.5 * r[2]])
        return q / np.linalg.norm(q)
    half = 0.5 * angle
    s = math.sin(half) / angle
    return np.array([math.cos(half), r[0] * s, r[1] * s, r[2] * s])


def quat_to_dcm(q: np.ndarray) -> np.ndarray:
    w, x, y, z = q
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
        ]
    )


def quat_from_euler(roll: float, pitch: float, yaw: float) -> np.ndarray:
    """ZYX (yaw, pitch, roll) Euler angles in radians -> body-to-NED quaternion."""
    cr, sr = math.cos(roll / 2), math.sin(roll / 2)
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
    return np.array(
        [
            cr * cp * cy + sr * sp * sy,
            sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy,
            cr * cp * sy - sr * sp * cy,
        ]
    )


def dcm_to_euler(r: np.ndarray) -> tuple[float, float, float]:
    """Body-to-NED DCM -> (roll, pitch, yaw) radians, ZYX convention."""
    pitch = -math.asin(max(-1.0, min(1.0, float(r[2, 0]))))
    roll = math.atan2(float(r[2, 1]), float(r[2, 2]))
    yaw = math.atan2(float(r[1, 0]), float(r[0, 0]))
    return roll, pitch, yaw


class ErrorStateIns:
    """Strapdown mechanisation plus the error-state Kalman filter."""

    def __init__(self, gravity: float, noise: ImuNoise | None = None) -> None:
        self.g_n = np.array([0.0, 0.0, gravity])
        self.noise = noise or ImuNoise()
        self.p = np.zeros(3)
        self.v = np.zeros(3)
        self.q = np.array([1.0, 0.0, 0.0, 0.0])
        self.bg = np.zeros(3)
        self.ba = np.zeros(3)
        self.cov = np.eye(N_STATES)
        # Last bias-corrected IMU readings and nav-frame acceleration: used
        # to extrapolate the state back to a delayed measurement's epoch.
        self.omega_b = np.zeros(3)
        self.f_b = np.array([0.0, 0.0, -gravity])
        self.a_n = np.zeros(3)

    # ---- initialisation -------------------------------------------------

    def initialize(
        self,
        p: np.ndarray,
        v: np.ndarray,
        q: np.ndarray,
        sigma_p: tuple[float, float, float],
        sigma_v: float,
        sigma_att: tuple[float, float, float],
        sigma_bg: float,
        sigma_ba: float,
        bg: np.ndarray | None = None,
    ) -> None:
        self.p = np.asarray(p, dtype=float).copy()
        self.v = np.asarray(v, dtype=float).copy()
        self.q = np.asarray(q, dtype=float) / np.linalg.norm(q)
        self.bg = np.zeros(3) if bg is None else np.asarray(bg, dtype=float).copy()
        self.ba = np.zeros(3)
        diag = np.concatenate(
            [
                np.square(sigma_p),
                np.full(3, sigma_v**2),
                np.square(sigma_att),
                np.full(3, sigma_bg**2),
                np.full(3, sigma_ba**2),
            ]
        )
        self.cov = np.diag(diag)

    @property
    def dcm(self) -> np.ndarray:
        return quat_to_dcm(self.q)

    # ---- propagation ----------------------------------------------------

    def propagate(self, f_meas: np.ndarray, w_meas: np.ndarray, dt: float) -> None:
        """Advance the nominal state and covariance by one IMU interval."""
        f = f_meas - self.ba
        w = w_meas - self.bg
        r = self.dcm
        a = r @ f + self.g_n

        self.p = self.p + self.v * dt + 0.5 * a * dt * dt
        self.v = self.v + a * dt
        self.q = quat_mul(self.q, quat_from_rotvec(w * dt))
        self.q /= np.linalg.norm(self.q)
        self.f_b, self.omega_b, self.a_n = f, w, a

        phi = np.eye(N_STATES)
        phi[P, V] = np.eye(3) * dt
        phi[V, TH] = -skew(r @ f) * dt
        phi[V, BA] = -r * dt
        phi[TH, BG] = -r * dt

        n = self.noise
        qd = np.zeros(N_STATES)
        qd[V] = n.accel_noise**2 * dt
        qd[TH] = n.gyro_noise**2 * dt
        qd[BG] = n.gyro_bias_rw**2 * dt
        qd[BA] = n.accel_bias_rw**2 * dt
        cov = phi @ self.cov @ phi.T
        cov[np.diag_indices(N_STATES)] += qd
        self.cov = 0.5 * (cov + cov.T)

    # ---- measurement update ----------------------------------------------

    def update(
        self, residual: np.ndarray, h: np.ndarray, r_diag: np.ndarray, gate: float | None = None
    ) -> tuple[bool, float]:
        """Generic error-state update. Returns (accepted, NIS).

        ``gate`` is a chi-square threshold on the normalised innovation
        squared; a measurement above it is rejected and leaves the state
        untouched (the caller counts and acts on rejections).
        """
        s = h @ self.cov @ h.T + np.diag(r_diag)
        try:
            s_inv = np.linalg.inv(s)
        except np.linalg.LinAlgError:
            return False, math.inf
        nis = float(residual @ s_inv @ residual)
        if gate is not None and nis > gate:
            return False, nis
        k = self.cov @ h.T @ s_inv
        dx = k @ residual
        ikh = np.eye(N_STATES) - k @ h
        cov = ikh @ self.cov @ ikh.T + k @ np.diag(r_diag) @ k.T  # Joseph form
        self.cov = 0.5 * (cov + cov.T)
        self._inject(dx)
        return True, nis

    def _inject(self, dx: np.ndarray) -> None:
        self.p += dx[P]
        self.v += dx[V]
        self.q = quat_mul(quat_from_rotvec(dx[TH]), self.q)
        self.q /= np.linalg.norm(self.q)
        self.bg += dx[BG]
        self.ba += dx[BA]

    # ---- measurement models ------------------------------------------------

    def antenna_position(self, lever_arm: np.ndarray, lag_s: float = 0.0) -> np.ndarray:
        """Predicted antenna NED position ``lag_s`` seconds before now."""
        r = self.dcm
        arm_n = r @ lever_arm
        v_ant = self.v + r @ np.cross(self.omega_b, lever_arm)
        return self.p + arm_n - v_ant * lag_s

    def antenna_velocity(self, lever_arm: np.ndarray, lag_s: float = 0.0) -> np.ndarray:
        r = self.dcm
        return self.v + r @ np.cross(self.omega_b, lever_arm) - self.a_n * lag_s

    def position_jacobian(self, lever_arm: np.ndarray) -> np.ndarray:
        h = np.zeros((3, N_STATES))
        h[:, P] = np.eye(3)
        h[:, TH] = -skew(self.dcm @ lever_arm)
        return h

    def velocity_jacobian(self, lever_arm: np.ndarray) -> np.ndarray:
        h = np.zeros((3, N_STATES))
        h[:, V] = np.eye(3)
        h[:, TH] = -skew(self.dcm @ np.cross(self.omega_b, lever_arm))
        return h

    def body_velocity_jacobian(self) -> np.ndarray:
        """d(R^T v)/dx: for non-holonomic and zero-velocity constraints."""
        rt = self.dcm.T
        h = np.zeros((3, N_STATES))
        h[:, V] = rt
        h[:, TH] = rt @ skew(self.v)
        return h
