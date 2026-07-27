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

**Flat `car.*` namespace, snake_case, one level.** Every on-vehicle
sensor/actuator reading -- engine, chassis dynamics, wheels, fuel,
electrics, whatever -- is a single `car.<name>` channel. There is no
domain sub-taxonomy (no `engine.*`, `chassis.*`, `wheels.*`, `fuel.*`,
`electrics.*`). This was a deliberate simplification (see ADR 0004's
2026-07-27 amendment): a domain taxonomy invites pointless categorization
debates -- does throttle position belong to `engine.*` or `chassis.*`? does
a wheel-speed-derived vehicle speed belong to `wheels.*` or `engine.*`? --
that have no correct answer and no bearing on how any consumer actually
uses the channel. Consumers match on the full channel name or a glob; they
don't need or benefit from a domain prefix to group by.

Reserved namespaces, unchanged and outside `car.*`:

| Namespace | Covers |
|---|---|
| `car.*` | Every on-vehicle sensor/actuator reading: ECU engine/drivetrain state, IMU/dynamics, wheel speeds, fuel system, electrics, power-distribution/keypad device health. Flat, one level, snake_case. |
| `position.*` | GNSS: lat, lon, speed, heading, fix quality |
| `sys.*` | Host metrics and vehicle-agent health (queue depths, dropped-sample counters, link status) -- not populated via `catalog.yaml`; these are internal agent/host channels, not mapped source signals |
| `lap.*`, `timing.*` | **Reserved, derived.** Produced by the timing engine from `apps.lap_timing`'s output (lap/sector events, `delta_best`, `predicted_lap`, distance). Never appear as a `from:` target in `catalog.yaml` -- they are channels the timing engine *writes*, re-entering the sample bus like any other channel. |

Where the bare, flattened name would be ambiguous on its own, keep the
former domain word as part of the name instead of the channel name itself:
`car.engine_demand`, `car.engine_limiting_active`,
`car.engine_protection_severity`, `car.gearbox_oil_temp`. Where it wouldn't
be ambiguous, drop it: `engine.rpm` -> `car.rpm`, `chassis.accel_x` ->
`car.accel_x`. Per-corner wheel-speed channels keep an explicit `wheel_`
infix because the bare name (`car.speed_fl`) would be indistinguishable
from a GPS-derived speed: `car.wheel_speed_fl`, `_fr`, `_rl`, `_rr`.

**Name the measurement, not the wire.** Canonical names describe physical
meaning, not wiring. This mainly bites on generic PDM (power-distribution
module) I/O: a PD16A analog input (`AVI1`) or output driver (`HCO8_3`) has
no fixed meaning -- it reports whatever sensor or load happens to be wired
to that pin on *this* car. A different build of the same car (or a
different car using the same PD16A) could have `AVI1` wired to a fuel
surge-tank level sender, a brake-bias potentiometer, or nothing at all.
Mapping `car.avi1_v` in the example catalog would document a wiring
decision, not a measurement -- so the example catalog leaves these
unmapped and documents the pattern with a commented template instead (see
`profiles/example-club-racer/catalog.yaml`, PDM/keypad section):

```yaml
# car.fuel_surge_tank_level: { from: "can0:haltech2.PD16A_AVI_VOLTAGES.PD16A_AVI_1_VOLTAGE", units: "V" }
```

Device-health diagnostics are the exception: a PD16A's own temperature,
battery voltage, and total current describe the module itself, not
whatever is wired to it, so they're meaningful in any build and stay
mapped as `car.pd16_temp`-style names (`car.pd16_cpu_temp`,
`car.pd16_battery_v`, `car.pd16_total_current`, etc). Generic device
signals like the CAN keypad's buttons are likewise always meaningful
regardless of car-specific wiring and stay mapped (`car.keypad_button_1`,
...).

Judgement calls worth a second look (flagged for review, not blocking):

- **`car.driveshaft_rpm`, `car.trip_distance_m`, `car.vehicle_speed`**:
  ECU-calculated (not raw wheel-sensor) values; previously grouped under
  `wheels.*`, now just flat `car.*` names with no domain marker since the
  bare names aren't ambiguous.
- **`car.ambient_air_temp`, `car.fuel_temp`, `car.gearbox_oil_temp`**:
  previously kept under `engine.*` (matching how the Haltech DBC groups its
  own temperature messages) even though the physical medium (ambient air,
  fuel, gearbox oil) isn't strictly "engine". Flattening removes the
  question entirely; `car.gearbox_oil_temp` keeps its full descriptive name
  since bare `car.gearbox_temp` would collide conceptually with nothing
  else here but reads better fully spelled out.
- **`car.fuel_pressure`, `car.fuel_temp` vs. `car.fuel_level`,
  `car.fuel_flow_rate`, ...**: previously split across `engine.*` (the
  ECU-reported pressure/temp signals) and `fuel.*` (level, flow,
  consumption, trim) -- an inconsistency the old convention's own
  judgement-call notes flagged. Flattening incidentally resolves it: all
  fuel-system channels are now consistently `car.fuel_*`.
- **`car.pd16_hco25_12_temp_status`, `car.pd16_hco25_34_temp_status`**:
  these come from the `PD16A_DIAGNOSTICS` message (per-driver-pair thermal
  status), not `PD16A_OUTPUT_STATUS`, so -- unlike the per-output
  current/status/load channels -- they describe PD16A hardware health, not
  wiring. Kept mapped alongside the other PD16 diagnostics rather than
  removed to the wiring-dependent template.
- **`car.trigger_counter`, `car.trigger_error_count`,
  `car.trigger_sync_level`**: kept bare (no `engine_` prefix) on the
  judgement that "trigger" unambiguously reads as the ignition/crank
  trigger system in this context; flagging in case a future channel
  (e.g. a lap/sector "trigger") would collide.

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

`position.*` is a reserved namespace, not `car.*`, so this example is
unaffected by the flat-`car.*` renaming -- it illustrates the same point
either way: only the `from:` refs change when a source moves buses or
transports.

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

**101 old include-list entries -> 116 canonical `car.*` channels, 86
entries mapped, 14 unmapped by design (wiring-dependent PDM I/O), 1
unmapped (stale)** (see notes below). The 2026-07-27 flat-`car.*` renaming
also collapsed the PDM per-output current/status/load and AVI/SPI analog
input entries from mapped channels to the commented wiring-dependent
template in `catalog.yaml` -- see "name the measurement, not the wire"
above.

| Old `signals_config.yaml` entry | Canonical channel(s) |
|---|---|
| `ENGINE_SPEED` | `car.rpm` |
| `MANIFOLD_PRESSURE` | `car.map` |
| `THROTTLE_POSITION` | `car.throttle_pos` |
| `COOLANT_PRESSURE` | `car.coolant_pressure` |
| `FUEL_PRESSURE` | `car.fuel_pressure` |
| `OIL_PRESSURE` | `car.oil_pressure` |
| `ENGINE_DEMAND` | `car.engine_demand` |
| `WASTEGATE_PRESSURE` | `car.wastegate_pressure` |
| `COOLANT_TEMPERATURE` | `car.coolant_temp` |
| `AIR_TEMPERATURE` | `car.air_temp` |
| `FUEL_TEMPERATURE` | `car.fuel_temp` |
| `OIL_TEMPERATURE` | `car.oil_temp` |
| `GEARBOX_TEMPERATURE` | **unmapped** -- no signal with this name in any DBC (see note below) |
| `WHEEL_SPEED_*` | `car.wheel_speed_fl`, `car.wheel_speed_fr`, `car.wheel_speed_rl`, `car.wheel_speed_rr` |
| `ENGINE_LIMITING_ACTIVE` | `car.engine_limiting_active` |
| `BOOST_CONTROL_OUTPUT` | `car.boost_control_output` |
| `VEHICLE_SPEED` | `car.vehicle_speed` |
| `BATTERY_VOLTAGE` | `car.battery_v` |
| `TARGET_BOOST_LEVEL` | `car.target_boost` |
| `BAROMETRIC_PRESSURE` | `car.baro_pressure` |
| `FUEL_FLOW` | `car.fuel_flow_rate` |
| `LAMBDA_SENSOR1` | `car.lambda1` |
| `TRIGGER_SYSTEM_ERROR_COUNT` | `car.trigger_error_count` |
| `AMBIENT_AIR_TEMPERATURE` | `car.ambient_air_temp` |
| `FUEL_LEVEL` | `car.fuel_level` |
| `BRAKE_PEDAL_SWITCH` | `car.brake_pedal_switch` |
| `CLUTCH_SWITCH` | `car.clutch_switch` |
| `OIL_PRESSURE_LIGHT` | `car.oil_pressure_light` |
| `TRACTION_CONTROL_ENABLED` | `car.tc_enabled` |
| `TRACTION_CONTROL_ACTIVE` | `car.tc_active` |
| `CHECK_ENGINE_LIGHT` | `car.check_engine_light` |
| `GEAR` | `car.gear` |
| `ENGINE_PROTECTION_SEVERITY_LEVEL` | `car.engine_protection_severity` |
| `ENGINE_PROTECTION_REASON_LETTER` | `car.engine_protection_reason_letter` |
| `ENGINE_PROTECTION_REASON_NUMBER` | `car.engine_protection_reason_number` |
| `TOTAL_FUEL_USED_TRIP_METER1` | `car.fuel_trip_used` |
| `DISTANCE_TRIP_METER1` | `car.trip_distance_m` |
| `KNOCK_*` | `car.knock_level1`, `car.knock_level2` |
| `PARK_LIGHT_STATE` | `car.park_light` |
| `HEAD_LIGHT_STATE` | `car.head_light` |
| `HIGH_BEAM_LIGHT_STATE` | `car.high_beam_light` |
| `LEFT_INDICATOR_STATE` | `car.left_indicator` |
| `RIGHT_INDICATOR_STATE` | `car.right_indicator` |
| `GENERIC_OUTPUT_STATES` | `car.generic_output_states` |
| `CALCULATED_AIR_TEMPERATURE` | `car.calculated_air_temp` |
| `PDM_DATA_BYTE_*` | `car.pdm_raw_byte_0`, `car.pdm_raw_byte_1`, `car.pdm_raw_byte_2`, `car.pdm_raw_byte_3` |
| `IMU_ACCEL_*` | `car.accel_x`, `car.accel_y`, `car.accel_z` |
| `IMU_GYRO_*` | `car.gyro_x`, `car.gyro_y`, `car.gyro_z` |
| `IMU_ROLL` | `car.roll` |
| `IMU_PITCH` | `car.pitch` |
| `IMU_YAW` | `car.yaw` |
| `IMU_TEMPERATURE` | `car.imu_temp` |
| `LAMBDA_OVERALL` | `car.lambda_overall` |
| `TARGET_LAMBDA` | `car.target_lambda` |
| `IGNITION_ANGLE_LEADING` | `car.ignition_angle_leading` |
| `IGNITION_ANGLE_BANK1` | `car.ignition_angle_bank1` |
| `IGNITION_ANGLE_BANK2` | `car.ignition_angle_bank2` |
| `INJECTION_STAGE1_DUTY_CYCLE` | `car.injection_stage1_duty` |
| `INJECTION_STAGE1_AVG_TIME` | `car.injection_stage1_avg_time` |
| `FUEL_TRIM_LONG_TERM_BANK1` | `car.fuel_trim_long_term_bank1` |
| `ACCELERATOR_PEDAL_POSITION` | `car.accel_pedal_pos` |
| `INJECTOR_PRESSURE_DIFFERENTIAL` | `car.injector_pressure_diff` |
| `EXHAUST_MANIFOLD_PRESSURE` | `car.exhaust_manifold_pressure` |
| `LAUNCH_CONTROL_ACTIVE` | `car.launch_control_active` |
| `LAUNCH_CONTROL_IGNITION_RETARD` | `car.launch_control_ign_retard` |
| `DRIVESHAFT_RPM` | `car.driveshaft_rpm` |
| `TOTAL_FUEL_USED` | `car.fuel_total_used` |
| `PRIMARY_FUEL_PUMP_OUTPUT` | `car.fuel_primary_pump_output` |
| `ECU_TEMPERATURE` | `car.ecu_temp` |
| `GEARBOX_OIL_TEMPERATURE` | `car.gearbox_oil_temp` |
| `TRIGGER_COUNTER` | `car.trigger_counter` |
| `TRIGGER_SYNC_LEVEL` | `car.trigger_sync_level` |
| `WB1_LAMBDA_1` | `car.wb1_lambda1` |
| `WB1_DIAGNOSTIC_1` | `car.wb1_diagnostic1` |
| `WB1_SENSE_RESISTOR_1` | `car.wb1_sense_resistor1` |
| `WB1_BATTERY_VOLTAGE` | `car.wb1_battery_v` |
| `PD16A_TOTAL_CURRENT` | `car.pd16_total_current` |
| `PD16A_BATTERY_VOLTAGE` | `car.pd16_battery_v` |
| `PD16A_MAIN_RAIL_VOLTAGE` | `car.pd16_main_rail_v` |
| `PD16A_PROT_RAIL_VOLTAGE` | `car.pd16_prot_rail_v` |
| `PD16A_IGNITION_SWITCH` | `car.pd16_ignition_switch` |
| `PD16A_CPU_TEMP` | `car.pd16_cpu_temp` |
| `PD16A_MAIN_RAIL_TEMP` | `car.pd16_main_rail_temp` |
| `PD16A_THERMISTOR_*_TEMP` | `car.pd16_thermistor_1_temp`, `car.pd16_thermistor_2_temp`, `car.pd16_thermistor_3_temp` |
| `PD16A_*_TEMP_STATUS` | `car.pd16_hco25_12_temp_status`, `car.pd16_hco25_34_temp_status`, `car.pd16_main_rail_temp_status`, `car.pd16_tvs_temp_status` |
| `PD16A_*_PIN_STATE` | **unmapped by design** -- wiring-dependent, name when assigned (per-output pin state on `PD16A_OUTPUT_STATUS`; see the commented template in `catalog.yaml`) |
| `PD16A_*_RETRY_COUNT` | **unmapped by design** -- wiring-dependent, name when assigned |
| `PD16A_HCO*_VOLTAGE` | **unmapped by design** -- wiring-dependent, name when assigned |
| `PD16A_HCO*_CURRENT` | **unmapped by design** -- wiring-dependent, name when assigned |
| `PD16A_HCO*_LOAD` | **unmapped by design** -- wiring-dependent, name when assigned |
| `PD16A_HBO_*_VOLTAGE` | **unmapped by design** -- wiring-dependent, name when assigned |
| `PD16A_HBO_*_HS_CURRENT` | **unmapped by design** -- wiring-dependent, name when assigned |
| `PD16A_HBO_*_LOAD` | **unmapped by design** -- wiring-dependent, name when assigned |
| `PD16A_AVI_*_STATE` | **unmapped by design** -- wiring-dependent, name when assigned |
| `PD16A_AVI_*_VOLTAGE` | **unmapped by design** -- wiring-dependent, name when assigned (previously the six `rbe`-policed live analog voltage channels; the policy is documented in the `catalog.yaml` template comment for reuse when mapped) |
| `PD16A_SPI_3_STATE` | **unmapped by design** -- wiring-dependent, name when assigned |
| `PD16A_SPI_4_STATE` | **unmapped by design** -- wiring-dependent, name when assigned |
| `PD16A_SPI_3_VOLTAGE` | **unmapped by design** -- wiring-dependent, name when assigned |
| `PD16A_SPI_4_VOLTAGE` | **unmapped by design** -- wiring-dependent, name when assigned |
| `KEYPAD_BUTTON_*` | `car.keypad_button_1`, `car.keypad_button_2`, `car.keypad_button_3`, `car.keypad_button_4`, `car.keypad_button_5`, `car.keypad_button_6`, `car.keypad_button_7`, `car.keypad_button_8`, `car.keypad_button_9`, `car.keypad_button_10`, `car.keypad_button_11`, `car.keypad_button_12`, `car.keypad_button_13`, `car.keypad_button_14`, `car.keypad_button_15` |
| `KEYPAD_NMT_STATE` | `car.keypad_nmt_state` |

**Note on `GEARBOX_TEMPERATURE`:** no DBC signal has this exact name --
`TEMPERATURE2` (message `BO_ 993` in `haltech-ecu.dbc`) defines
`GEARBOX_OIL_TEMPERATURE`, not `GEARBOX_TEMPERATURE`. The old
`signals_config.yaml` includes both names (one under a `# TEMPERATURE2`
comment, one later under `# ECU channels confirmed present...`); the
second (`GEARBOX_OIL_TEMPERATURE`) matches a real signal and is already
covered above as `car.gearbox_oil_temp`. `GEARBOX_TEMPERATURE` itself
is almost certainly a stale typo/duplicate in the old config that never
matched anything at runtime -- flagging for the owner to confirm rather
than silently dropping it.
