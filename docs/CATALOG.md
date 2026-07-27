# Catalog schema

This document defines the two config files that make a car a "profile" in
openlaps: `vehicle.yaml` (what hardware exists and how to talk to it) and
`catalog.yaml` (what the signals from that hardware *mean*, named so apps
never need to know about buses, DBCs, or wire formats). Together they are
the entire difference between one car and another -- everything upstream
(collectors, the vehicle agent, the timing engine, dashboards, exports)
is generic and reads only canonical channel names.

`profiles/example-club-racer/` is a complete, real example: a single 1 Mbps
CAN bus carrying a Haltech Elite ECU, a Haltech PD16A power-distribution
module, a Haltech WB1 wideband controller and an FDI DETA10A IMU, plus a
UM980 GNSS receiver on serial0. Read it alongside this document.

## `vehicle.yaml` schema

Declares the physical transports on the car: what buses and serial ports
exist, which DBCs and drivers are attached to them, and whether host
metrics are collected. It says nothing about what any signal *means* --
that is `catalog.yaml`'s job.

```yaml
vehicle:
  id: <string>                # unique id for this vehicle/profile

buses:
  - name: <string>            # local name used by catalog `from:` refs, e.g. "can0"
    interface: <string>       # socketCAN interface name (usually == name)
    bitrate: <int>            # bits/sec, e.g. 1000000
    dbcs:
      - device: <string>      # short alias used in catalog `from:` refs, e.g. "haltech"
        file: <path>          # path to the .dbc, relative to this profile directory

serial:
  - name: <string>            # local name used by catalog `from:` refs, e.g. "serial0"
    port: <string>            # device path, e.g. "/dev/ttyUSB0"
    baud: <int>
    decoder: <string>         # sentence/frame decoder, e.g. "nmea"
    driver:                   # optional: device-specific startup config + write-back
      name: <string>          # e.g. "um980"
      config:                 # driver-specific; um980 supports:
        rate_hz: <int>        #   output rate applied to the receiver on start
        sentences: [<string>] #   NMEA sentences to enable, e.g. [RMC]
        configure_on_start: <bool>  # apply rate/sentences via receiver commands at startup
      # Drivers that support RTK may also accept RTCM correction bytes
      # written back over core NATS on subject `rtcm.<vehicle>` (published
      # by the pit-side ntrip-client) and forward them straight to the
      # receiver -- this is automatic once a capable driver is attached to
      # a serial source; it is not something this file configures per se.

host:
  enabled: <bool>              # collect host/system metrics (cpu, mem, disk, net) as sys.* channels
  interval: <duration>         # polling interval, e.g. "5s"
```

Buses and serial sources are generic transports: they decode to
*source-native* signals (`MESSAGE.SIGNAL` for CAN, `SENTENCE.field` for
serial) and carry no domain meaning. A GPS receiver moving from serial to
CAN, or a second CAN bus being added, is a `vehicle.yaml` + `catalog.yaml`
edit -- no code changes, no consumer changes. See the worked example below.

## `catalog.yaml` schema

Maps source-native signals to canonical channel names, the vocabulary every
other part of the system (timing engine, dashboards, lap-analysis exports
in downstream tooling, Timescale queries) uses.

```yaml
channels:
  <canonical.name>:
    from: <source-ref>          # "<bus>:<device>.<MESSAGE>.<SIGNAL>" (CAN)
                                 # or "<serial>:<driver>.<SENTENCE>.<field>" (serial)
                                 # or "host:<metric>" (host collector)
    units: <string>              # physical unit as decoded, omitted for bool/int/string channels
    type: double|int|bool|string # optional, defaults to "double"
    rbe:                         # optional report-by-exception policy (link/store path only)
      deadband: <number>         #   suppress samples that haven't moved by at least this much
      min_interval: <duration>   #   never publish more often than this even if noisy
      max_interval: <duration>   #   always publish at least this often (heartbeat, bounds fill-forward)
    live_hz: <number>            # optional decimation hint for the pit live-decoder (MQTT-Live path)

apps:
  lap_timing:
    position: <channel-glob>     # e.g. "position.*" -- which channels feed the timing engine
    track: <string>              # track definition name, resolved under profiles/<profile>/tracks/
```

`rbe` and `live_hz` are independent: `rbe` governs what reaches the durable
JetStream stream and Timescale; `live_hz` only decimates the *pit live
decoder*'s republish to MQTT for gauge panels. Neither affects the timing
engine, which taps the sample stream **pre-RBE** so lap timing never sees
decimated position data (see the vehicle agent design spec for the tap
architecture).

## Canonical naming convention

Dotted lowercase, `<domain>.<name>`. Fixed top-level domains:

| Domain | Covers |
|---|---|
| `engine.*` | ECU-reported engine/drivetrain-control state: RPM, pressures, temperatures, lambda, ignition/injection timing, knock, engine protection, ECU/wideband-controller diagnostics |
| `chassis.*` | IMU/dynamics: accelerometer, gyroscope, fused Euler angles, and driver-input switches that describe vehicle dynamics inputs (brake/clutch/accelerator pedal, traction control state) |
| `position.*` | GNSS: lat, lon, speed, heading, fix quality |
| `wheels.*` | Wheel-speed sensors and ECU-derived vehicle/driveline speed signals |
| `fuel.*` | Fuel system: level, flow, consumption, trim |
| `electrics.*` | Electrical system: battery, lighting, power-distribution module (`electrics.pd16.*`), CAN keypad (`electrics.keypad.*`), raw PDM diagnostic bytes |
| `sys.*` | Host metrics and vehicle-agent health (queue depths, dropped-sample counters, link status) -- not populated via `catalog.yaml`; these are internal agent/host channels, not mapped source signals |
| `lap.*`, `timing.*` | **Reserved, derived.** Produced by the timing engine from `apps.lap_timing`'s output (lap/sector events, `delta_best`, `predicted_lap`, distance). Never appear as a `from:` target in `catalog.yaml` -- they are channels the timing engine *writes*, re-entering the sample bus like any other channel. |

Judgement calls worth a second look (flagged for review, not blocking):

- **`wheels.vehicle_speed`, `wheels.driveshaft_rpm`, `wheels.trip_distance_m`**:
  these are ECU-calculated (not raw wheel-sensor) values, grouped under
  `wheels.*` as "speed/distance metrics" rather than `engine.*` or a
  dedicated drivetrain domain (none exists in the current convention).
- **`chassis.accel_pedal_pos`, `chassis.brake_pedal_switch`,
  `chassis.clutch_switch`**: grouped under `chassis.*` as driver-dynamics
  inputs rather than `engine.*`, for consistency with each other -- even
  though the ECU is the physical source for all three.
- **`engine.ambient_air_temp`, `engine.fuel_temp`, `engine.gearbox_oil_temp`**:
  kept under `engine.*` (matching how the Haltech DBC groups its own
  temperature messages) even though the physical medium (ambient air, fuel,
  gearbox oil) isn't strictly "engine".
- **`electrics.pd16.avi1_v` / `avi1_state` / `spi3_v` naming**: no
  underscore between the port prefix and its number (matches this plan's
  own worked example), while the HCO25/HCO8/HBO output channels *do* keep
  the underscore (`hco25_1_v`, `hco8_1_v`, `hbo_1_v`), mirroring the DBC's
  own `HCO25_1`, `HCO8_1`, `HBO_1` signal-name style. This is a deliberate
  but slightly inconsistent choice within `electrics.pd16.*` -- flagging
  for review.

## Worked example: adding a second CAN bus carrying GPS

Say GPS moves from `serial0` (NMEA over UART) to a second CAN bus,
`can1`, decoded via a new `gps.dbc`. Only the profile changes; the timing
engine, dashboards, and exports all keep reading `position.*` and are
untouched.

**`vehicle.yaml` diff:**

```diff
 buses:
   - name: can0
     interface: can0
     bitrate: 1000000
     dbcs:
       - device: haltech
         file: dbcs/haltech-ecu.dbc
       - device: haltech2
         file: dbcs/haltech-multiplexed.dbc
       - device: wideband
         file: dbcs/haltech-wideband.dbc
       - device: imu
         file: dbcs/fdi-imu.dbc
+  - name: can1
+    interface: can1
+    bitrate: 500000
+    dbcs:
+      - device: gps
+        file: dbcs/gps.dbc

 serial:
   - name: serial0
     port: /dev/ttyUSB0
     baud: 115200
     decoder: nmea
-    driver:
-      name: um980
-      config:
-        rate_hz: 50
-        sentences: [RMC]
-        configure_on_start: true
+    # um980 driver removed -- GPS now arrives over can1, not this port
```

**`catalog.yaml` diff:**

```diff
-  position.lat:  { from: "serial0:um980.RMC.lat", units: "deg" }
-  position.lon:  { from: "serial0:um980.RMC.lon", units: "deg" }
-  position.speed: { from: "serial0:um980.RMC.speed", units: "km/h" }
-  position.heading: { from: "serial0:um980.RMC.heading", units: "deg" }
-  position.fix_quality: { from: "serial0:um980.RMC.mode", type: string }
+  position.lat:  { from: "can1:gps.GPS_POS.LATITUDE", units: "deg" }
+  position.lon:  { from: "can1:gps.GPS_POS.LONGITUDE", units: "deg" }
+  position.speed: { from: "can1:gps.GPS_VEL.SPEED", units: "km/h" }
+  position.heading: { from: "can1:gps.GPS_VEL.HEADING", units: "deg" }
+  position.fix_quality: { from: "can1:gps.GPS_STATUS.FIX_TYPE", type: string }
```

`apps.lap_timing` (`position: position.*`, `track: Wanneroo`) is untouched --
it never referenced a bus or decoder, only canonical names. Every consumer
downstream of the catalog mapper is equally unaffected.

## Coverage: old `signals_config.yaml` -> canonical channels

Every `include_signals` entry from the old repo's
`config/signals_config.yaml`, expanded against the four DBCs
(`haltech-ecu.dbc`, `haltech-multiplexed.dbc`, `haltech-wideband.dbc`,
`fdi-imu.dbc`) and mapped to its canonical channel(s) in
`profiles/example-club-racer/catalog.yaml`. Wildcard entries (e.g.
`WHEEL_SPEED_*`, `PD16A_HCO*_VOLTAGE`) are expanded to every signal name
actually present in the DBCs, honoring the old config's `exclude_signals`
list (`IMU_*_SYNC`, `PD16A_*_CFG_*`, `PD16A_*_IN_*`).

**101 old include-list entries -> 212 canonical channels, 100 entries
mapped, 1 unmapped** (see note below).

| Old `signals_config.yaml` entry | Canonical channel(s) |
|---|---|
| `ENGINE_SPEED` | `engine.rpm` |
| `MANIFOLD_PRESSURE` | `engine.map` |
| `THROTTLE_POSITION` | `engine.throttle_pos` |
| `COOLANT_PRESSURE` | `engine.coolant_pressure` |
| `FUEL_PRESSURE` | `engine.fuel_pressure` |
| `OIL_PRESSURE` | `engine.oil_pressure` |
| `ENGINE_DEMAND` | `engine.demand` |
| `WASTEGATE_PRESSURE` | `engine.wastegate_pressure` |
| `COOLANT_TEMPERATURE` | `engine.coolant_temp` |
| `AIR_TEMPERATURE` | `engine.air_temp` |
| `FUEL_TEMPERATURE` | `engine.fuel_temp` |
| `OIL_TEMPERATURE` | `engine.oil_temp` |
| `GEARBOX_TEMPERATURE` | **unmapped** -- no signal with this name in any DBC (see note below) |
| `WHEEL_SPEED_*` | `wheels.speed_fl`, `wheels.speed_fr`, `wheels.speed_rl`, `wheels.speed_rr` |
| `ENGINE_LIMITING_ACTIVE` | `engine.limiting_active` |
| `BOOST_CONTROL_OUTPUT` | `engine.boost_control_output` |
| `VEHICLE_SPEED` | `wheels.vehicle_speed` |
| `BATTERY_VOLTAGE` | `electrics.battery_v` |
| `TARGET_BOOST_LEVEL` | `engine.target_boost` |
| `BAROMETRIC_PRESSURE` | `engine.baro_pressure` |
| `FUEL_FLOW` | `fuel.flow_rate` |
| `LAMBDA_SENSOR1` | `engine.lambda1` |
| `TRIGGER_SYSTEM_ERROR_COUNT` | `engine.trigger_error_count` |
| `AMBIENT_AIR_TEMPERATURE` | `engine.ambient_air_temp` |
| `FUEL_LEVEL` | `fuel.level` |
| `BRAKE_PEDAL_SWITCH` | `chassis.brake_pedal_switch` |
| `CLUTCH_SWITCH` | `chassis.clutch_switch` |
| `OIL_PRESSURE_LIGHT` | `engine.oil_pressure_light` |
| `TRACTION_CONTROL_ENABLED` | `chassis.tc_enabled` |
| `TRACTION_CONTROL_ACTIVE` | `chassis.tc_active` |
| `CHECK_ENGINE_LIGHT` | `engine.check_engine_light` |
| `GEAR` | `engine.gear` |
| `ENGINE_PROTECTION_SEVERITY_LEVEL` | `engine.protection_severity` |
| `ENGINE_PROTECTION_REASON_LETTER` | `engine.protection_reason_letter` |
| `ENGINE_PROTECTION_REASON_NUMBER` | `engine.protection_reason_number` |
| `TOTAL_FUEL_USED_TRIP_METER1` | `fuel.trip_used` |
| `DISTANCE_TRIP_METER1` | `wheels.trip_distance_m` |
| `KNOCK_*` | `engine.knock_level1`, `engine.knock_level2` |
| `PARK_LIGHT_STATE` | `electrics.park_light` |
| `HEAD_LIGHT_STATE` | `electrics.head_light` |
| `HIGH_BEAM_LIGHT_STATE` | `electrics.high_beam_light` |
| `LEFT_INDICATOR_STATE` | `electrics.left_indicator` |
| `RIGHT_INDICATOR_STATE` | `electrics.right_indicator` |
| `GENERIC_OUTPUT_STATES` | `electrics.generic_output_states` |
| `CALCULATED_AIR_TEMPERATURE` | `engine.calculated_air_temp` |
| `PDM_DATA_BYTE_*` | `electrics.pdm_raw_byte_0`, `electrics.pdm_raw_byte_1`, `electrics.pdm_raw_byte_2`, `electrics.pdm_raw_byte_3` |
| `IMU_ACCEL_*` | `chassis.accel_x`, `chassis.accel_y`, `chassis.accel_z` |
| `IMU_GYRO_*` | `chassis.gyro_x`, `chassis.gyro_y`, `chassis.gyro_z` |
| `IMU_ROLL` | `chassis.roll` |
| `IMU_PITCH` | `chassis.pitch` |
| `IMU_YAW` | `chassis.yaw` |
| `IMU_TEMPERATURE` | `chassis.imu_temp` |
| `LAMBDA_OVERALL` | `engine.lambda_overall` |
| `TARGET_LAMBDA` | `engine.target_lambda` |
| `IGNITION_ANGLE_LEADING` | `engine.ignition_angle_leading` |
| `IGNITION_ANGLE_BANK1` | `engine.ignition_angle_bank1` |
| `IGNITION_ANGLE_BANK2` | `engine.ignition_angle_bank2` |
| `INJECTION_STAGE1_DUTY_CYCLE` | `engine.injection_stage1_duty` |
| `INJECTION_STAGE1_AVG_TIME` | `engine.injection_stage1_avg_time` |
| `FUEL_TRIM_LONG_TERM_BANK1` | `fuel.trim_long_term_bank1` |
| `ACCELERATOR_PEDAL_POSITION` | `chassis.accel_pedal_pos` |
| `INJECTOR_PRESSURE_DIFFERENTIAL` | `engine.injector_pressure_diff` |
| `EXHAUST_MANIFOLD_PRESSURE` | `engine.exhaust_manifold_pressure` |
| `LAUNCH_CONTROL_ACTIVE` | `engine.launch_control_active` |
| `LAUNCH_CONTROL_IGNITION_RETARD` | `engine.launch_control_ign_retard` |
| `DRIVESHAFT_RPM` | `wheels.driveshaft_rpm` |
| `TOTAL_FUEL_USED` | `fuel.total_used` |
| `PRIMARY_FUEL_PUMP_OUTPUT` | `fuel.primary_pump_output` |
| `ECU_TEMPERATURE` | `engine.ecu_temp` |
| `GEARBOX_OIL_TEMPERATURE` | `engine.gearbox_oil_temp` |
| `TRIGGER_COUNTER` | `engine.trigger_counter` |
| `TRIGGER_SYNC_LEVEL` | `engine.trigger_sync_level` |
| `WB1_LAMBDA_1` | `engine.wb1_lambda1` |
| `WB1_DIAGNOSTIC_1` | `engine.wb1_diagnostic1` |
| `WB1_SENSE_RESISTOR_1` | `engine.wb1_sense_resistor1` |
| `WB1_BATTERY_VOLTAGE` | `electrics.wb1_battery_v` |
| `PD16A_TOTAL_CURRENT` | `electrics.pd16.total_current` |
| `PD16A_BATTERY_VOLTAGE` | `electrics.pd16.battery_v` |
| `PD16A_MAIN_RAIL_VOLTAGE` | `electrics.pd16.main_rail_v` |
| `PD16A_PROT_RAIL_VOLTAGE` | `electrics.pd16.prot_rail_v` |
| `PD16A_IGNITION_SWITCH` | `electrics.pd16.ignition_switch` |
| `PD16A_CPU_TEMP` | `electrics.pd16.cpu_temp` |
| `PD16A_MAIN_RAIL_TEMP` | `electrics.pd16.main_rail_temp` |
| `PD16A_THERMISTOR_*_TEMP` | `electrics.pd16.thermistor_1_temp`, `electrics.pd16.thermistor_2_temp`, `electrics.pd16.thermistor_3_temp` |
| `PD16A_*_TEMP_STATUS` | `electrics.pd16.hco25_12_temp_status`, `electrics.pd16.hco25_34_temp_status`, `electrics.pd16.main_rail_temp_status`, `electrics.pd16.tvs_temp_status` |
| `PD16A_*_PIN_STATE` | `electrics.pd16.hbo_1_pin_state`, `electrics.pd16.hbo_2_pin_state`, `electrics.pd16.hco8_1_pin_state`, `electrics.pd16.hco8_2_pin_state`, `electrics.pd16.hco8_3_pin_state`, `electrics.pd16.hco8_4_pin_state`, `electrics.pd16.hco8_5_pin_state`, `electrics.pd16.hco8_6_pin_state`, `electrics.pd16.hco8_7_pin_state`, `electrics.pd16.hco8_8_pin_state`, `electrics.pd16.hco8_9_pin_state`, `electrics.pd16.hco8_10_pin_state`, `electrics.pd16.hco25_1_pin_state`, `electrics.pd16.hco25_2_pin_state`, `electrics.pd16.hco25_3_pin_state`, `electrics.pd16.hco25_4_pin_state` |
| `PD16A_*_RETRY_COUNT` | `electrics.pd16.hbo_1_retry_count`, `electrics.pd16.hbo_2_retry_count`, `electrics.pd16.hco8_1_retry_count`, `electrics.pd16.hco8_2_retry_count`, `electrics.pd16.hco8_3_retry_count`, `electrics.pd16.hco8_4_retry_count`, `electrics.pd16.hco8_5_retry_count`, `electrics.pd16.hco8_6_retry_count`, `electrics.pd16.hco8_7_retry_count`, `electrics.pd16.hco8_8_retry_count`, `electrics.pd16.hco8_9_retry_count`, `electrics.pd16.hco8_10_retry_count`, `electrics.pd16.hco25_1_retry_count`, `electrics.pd16.hco25_2_retry_count`, `electrics.pd16.hco25_3_retry_count`, `electrics.pd16.hco25_4_retry_count` |
| `PD16A_HCO*_VOLTAGE` | `electrics.pd16.hco8_1_v`, `electrics.pd16.hco8_2_v`, `electrics.pd16.hco8_3_v`, `electrics.pd16.hco8_4_v`, `electrics.pd16.hco8_5_v`, `electrics.pd16.hco8_6_v`, `electrics.pd16.hco8_7_v`, `electrics.pd16.hco8_8_v`, `electrics.pd16.hco8_9_v`, `electrics.pd16.hco8_10_v`, `electrics.pd16.hco25_1_v`, `electrics.pd16.hco25_2_v`, `electrics.pd16.hco25_3_v`, `electrics.pd16.hco25_4_v` |
| `PD16A_HCO*_CURRENT` | `electrics.pd16.hco8_1_current`, `electrics.pd16.hco8_2_current`, `electrics.pd16.hco8_3_current`, `electrics.pd16.hco8_4_current`, `electrics.pd16.hco8_5_current`, `electrics.pd16.hco8_6_current`, `electrics.pd16.hco8_7_current`, `electrics.pd16.hco8_8_current`, `electrics.pd16.hco8_9_current`, `electrics.pd16.hco8_10_current`, `electrics.pd16.hco25_1_hs_current`, `electrics.pd16.hco25_1_ls_current`, `electrics.pd16.hco25_2_hs_current`, `electrics.pd16.hco25_2_ls_current`, `electrics.pd16.hco25_3_hs_current`, `electrics.pd16.hco25_3_ls_current`, `electrics.pd16.hco25_4_hs_current`, `electrics.pd16.hco25_4_ls_current` |
| `PD16A_HCO*_LOAD` | `electrics.pd16.hco8_1_load`, `electrics.pd16.hco8_2_load`, `electrics.pd16.hco8_3_load`, `electrics.pd16.hco8_4_load`, `electrics.pd16.hco8_5_load`, `electrics.pd16.hco8_6_load`, `electrics.pd16.hco8_7_load`, `electrics.pd16.hco8_8_load`, `electrics.pd16.hco8_9_load`, `electrics.pd16.hco8_10_load`, `electrics.pd16.hco25_1_load`, `electrics.pd16.hco25_2_load`, `electrics.pd16.hco25_3_load`, `electrics.pd16.hco25_4_load` |
| `PD16A_HBO_*_VOLTAGE` | `electrics.pd16.hbo_1_v`, `electrics.pd16.hbo_2_v` |
| `PD16A_HBO_*_HS_CURRENT` | `electrics.pd16.hbo_1_hs_current`, `electrics.pd16.hbo_2_hs_current` |
| `PD16A_HBO_*_LOAD` | `electrics.pd16.hbo_1_load`, `electrics.pd16.hbo_2_load` |
| `PD16A_AVI_*_STATE` | `electrics.pd16.avi1_state`, `electrics.pd16.avi2_state`, `electrics.pd16.avi3_state`, `electrics.pd16.avi4_state` |
| `PD16A_AVI_*_VOLTAGE` | `electrics.pd16.avi1_v`, `electrics.pd16.avi2_v`, `electrics.pd16.avi3_v`, `electrics.pd16.avi4_v` |
| `PD16A_SPI_3_STATE` | `electrics.pd16.spi3_state` |
| `PD16A_SPI_4_STATE` | `electrics.pd16.spi4_state` |
| `PD16A_SPI_3_VOLTAGE` | `electrics.pd16.spi3_v` |
| `PD16A_SPI_4_VOLTAGE` | `electrics.pd16.spi4_v` |
| `KEYPAD_BUTTON_*` | `electrics.keypad.button_1`, `electrics.keypad.button_2`, `electrics.keypad.button_3`, `electrics.keypad.button_4`, `electrics.keypad.button_5`, `electrics.keypad.button_6`, `electrics.keypad.button_7`, `electrics.keypad.button_8`, `electrics.keypad.button_9`, `electrics.keypad.button_10`, `electrics.keypad.button_11`, `electrics.keypad.button_12`, `electrics.keypad.button_13`, `electrics.keypad.button_14`, `electrics.keypad.button_15` |
| `KEYPAD_NMT_STATE` | `electrics.keypad.nmt_state` |

**Note on `GEARBOX_TEMPERATURE`:** no DBC signal has this exact name --
`TEMPERATURE2` (message `BO_ 993` in `haltech-ecu.dbc`) defines
`GEARBOX_OIL_TEMPERATURE`, not `GEARBOX_TEMPERATURE`. The old
`signals_config.yaml` includes both names (one under a `# TEMPERATURE2`
comment, one later under `# ECU channels confirmed present...`); the
second (`GEARBOX_OIL_TEMPERATURE`) matches a real signal and is already
covered above as `engine.gearbox_oil_temp`. `GEARBOX_TEMPERATURE` itself
is almost certainly a stale typo/duplicate in the old config that never
matched anything at runtime -- flagging for the owner to confirm rather
than silently dropping it.
