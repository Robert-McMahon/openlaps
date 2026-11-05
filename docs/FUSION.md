# Sensor fusion

The vehicle agent can fuse the IMU and GNSS into a single navigation
solution: a 15-state error-state Kalman filter (an INS aided by GNSS). It
runs in-agent on the pre-RBE tap, like lap timing, and emits `fused.*`
derived channels at a configurable rate (default 50 Hz).

It exists for two jobs:

- **Vehicle dynamics.** Attitude that stays right through sustained
  high-g corners, sideslip angle, gravity-free body-frame acceleration,
  road grade and bias-corrected yaw rate. None of these are directly
  measured by either sensor.
- **Better position.** IMU-rate position that rides through short GNSS
  dropouts, and that lap timing can use instead of raw fixes.

## Model

| | |
|---|---|
| Frames | Vehicle body FRD (forward, right, down); navigation is a local NED tangent frame anchored at the first fix (WGS-84, via ECEF, mm-exact over a circuit). |
| Nominal state | position, velocity, attitude quaternion, gyro bias, accel bias |
| Error state | 15: δp, δv, δθ (navigation-frame attitude error), δb<sub>g</sub>, δb<sub>a</sub> |
| Propagation | Strapdown on every IMU frame (~100 Hz); first-order covariance transition |
| Updates | GNSS horizontal position (RMC), horizontal Doppler velocity (RMC speed/course), height (GGA); zero-velocity and gyro-bias updates when parked |
| Not modelled | Earth rotation and transport rate (below MEMS gyro noise over a circuit); scale factor errors |

Code lives in `src/fusion/` (pure: `geodesy.py`, `ins.py`, `engine.py`)
and `src/agent/fusion_app.py` (catalog wiring). `src/fusion/simulate.py`
generates synthetic drives with truth for testing and tuning.

### Behaviour

- **Alignment.** Roll and pitch come from averaging the accelerometer
  while parked; the parked gyro average seeds the gyro bias. Heading is not
  observable at rest, so the filter initialises on the first fix above
  `align_speed_kmh` (default 18) with heading from GNSS course. Heading
  tightens to a fraction of a degree in the first corners. The status
  channel reads `aligning` until then.
- **Weighting.** Each fix is weighted by the receiver's own GST 1-sigma
  estimate when one is less than 1 s old, otherwise by its RMC mode
  (RTK fixed 3 cm, float 0.4 m, DGNSS 0.8 m, autonomous 2.5 m).
- **Latency.** A fix describes where the car was `latency_ms` before the
  agent read it. The filter compares it with its state extrapolated back
  to that epoch. Unmodelled, every 10 ms costs 0.4 m at 40 m/s.
- **Outliers.** A fix failing a 99.9 % chi-square gate is dropped. If
  fixes keep disagreeing for more than a second (a real solution jump such
  as float to fixed), the filter re-anchors to them.
- **Outages.** Through a dropout the filter dead-reckons on the IMU alone
  (status `dead_reckoning`). Past `max_outage_s` (default 10) it stops
  reporting position and re-aligns when GNSS returns. There is
  deliberately no "car goes where it points" constraint: it would erase
  the sideslip this exists to measure.

## Output channels

| Channel | Units | |
|---|---|---|
| `fused.status` | | `aligning`, `rtk_fixed`, `rtk_float`, `dgnss`, `gnss`, `dead_reckoning` (on change) |
| `fused.lat`, `fused.lon` | deg | IMU position; same capture stamp, so lap timing can pair them |
| `fused.alt` | m | Once GGA has given a height reference |
| `fused.speed` | km/h | 3D speed |
| `fused.course` | deg | Direction of travel over ground |
| `fused.vel_up` | m/s | |
| `fused.roll`, `fused.pitch`, `fused.yaw` | deg | Body attitude, ZYX Euler; roll positive right side down, pitch positive nose up |
| `fused.yaw_rate` | deg/s | Bias-corrected body z rate |
| `fused.accel_long`, `fused.accel_lat`, `fused.accel_vert` | g | Kinematic acceleration in the body frame, gravity removed. Positive forward / right / up |
| `fused.slip_angle` | deg | Course minus yaw (SAE β; negative with the nose inside a right-hander). Above 3 m/s only |
| `fused.grade` | % | Road grade along the velocity. Above 3 m/s only |
| `fused.pos_std` | m | Horizontal 1-sigma position uncertainty |
| `fused.gnss_age` | s | Time since the last accepted fix |

All are reported at the IMU's location. Translating to the CoG or rear
axle is a lever-arm calculation a consumer can do from these channels.

## Configuration

`apps.fusion` in `catalog.yaml` (see the example profile):

```yaml
apps:
  fusion:
    imu:
      accel: [car.accel_x, car.accel_y, car.accel_z]   # units g or m/s^2
      gyro: [car.gyro_x, car.gyro_y, car.gyro_z]       # units deg/s or rad/s
      axes: [x, y, z]            # IMU axis along vehicle forward, right, down
      mount_rpy_deg: [0, 0, 0]   # small residual misalignment
    gnss:
      lat: position.lat
      lon: position.lon
      speed: position.speed      # km/h, m/s or kn
      course: position.heading   # deg
      mode: position.fix_quality # optional, RMC posMode
      alt: position.alt          # optional, GGA
      lat_std: position.lat_std  # optional, GST (all three or none)
      lon_std: position.lon_std
      alt_std: position.alt_std
      latency_ms: 30
      lever_arm_m: [0.0, 0.0, 0.0]  # antenna from IMU: forward, right, down
    output_rate_hz: 50
    align_speed_kmh: 18
    max_outage_s: 10
    noise:                       # optional; defaults suit an automotive MEMS IMU
      accel_noise: 0.05          # m/s^2/sqrt(Hz)
      gyro_noise: 0.002          # rad/s/sqrt(Hz)
      accel_bias_rw: 0.002       # m/s^3/sqrt(Hz)
      gyro_bias_rw: 5.0e-5       # rad/s^2/sqrt(Hz)
```

The profile is validated at load: every input must be a catalog channel
with units the app can convert.

The UM980 needs GGA and GST enabled for altitude and error estimates. At
115200 baud, run them slower than RMC:

```yaml
sentences: [RMC, GGA, GST]
sentence_rates_hz: { GGA: 10, GST: 10 }
```

## Bring-up

1. **Axes.** Park level and start the agent. Parked, the vehicle-down
   accelerometer axis must read about −1 g (specific force points up). If
   it reads +1 g, the agent logs a warning: fix `axes`. Then drive a
   right-hander and check `fused.yaw_rate` is positive and
   `fused.accel_lat` positive (to the right).
2. **Lever arm.** Measure antenna minus IMU in vehicle axes to a
   centimetre or two. An error here shows up as position and heading
   error that changes with yaw rate.
3. **Latency.** Start at 30 ms. Correct latency shows as `fused.pos_std`
   staying low and no rejected fixes under hard braking. A wrong value
   shows as fused position leading or lagging RTK fixes along the track.
4. **Mounting misalignment.** On a long straight at steady speed,
   `fused.slip_angle` should average zero. A consistent offset is yaw
   misalignment: put it in `mount_rpy_deg`.
5. **Lap timing.** Once the above check out, switch
   `apps.lap_timing.position` from `position.*` to `fused.*`.

## Expected accuracy

From the simulator (`tests/test_fusion.py`): RTK, 100 Hz IMU with
realistic biases, 30 ms GNSS latency, a lap with 1.2 g corners, braking,
sideslip and ±4 % grade.

| | After first corners |
|---|---|
| Horizontal position | ~1 cm RMS |
| Height | < 7 cm |
| Roll / pitch / yaw | < 0.5° |
| Sideslip | < 0.3° |
| Gyro z bias | within 0.01°/s |
| 5 s GNSS outage | < 0.2 m drift |

The real car will be worse than the simulator, mainly from vibration and
unmodelled timing jitter. Treat these as a ceiling to tune towards.

## Known limits and next steps

- **Latency is a constant.** RMC carries its UTC epoch and the agent clock
  is PPS-disciplined, so latency could be measured live per fix.
- **Real-time only.** A forward-backward (RTS) smoother over stored data
  would improve post-session analysis, especially through outages.
- **The FDI's own attitude** (`car.roll`/`pitch`/`yaw`) is not used. It
  could seed alignment or serve as a cross-check.
- **Outputs at the IMU.** CoG / rear-axle referenced channels are a natural
  addition for vehicle-dynamics work.
