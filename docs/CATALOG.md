# Catalog schema

This document defines the two config files that make up a car's profile in
openlaps: `vehicle.yaml` (what hardware exists and how to talk to it) and
`catalog.yaml` (what the signals from that hardware mean, named so that apps
do not need to know about buses, DBCs, or wire formats). These files hold
everything that differs between one car and another. The rest of the system
(collectors, the vehicle agent, the timing engine, dashboards, exports)
is generic and reads only canonical channel names.

`profiles/example-club-racer/` is a complete example from a real car: a
single 1 Mbps CAN bus carrying a Haltech Elite ECU, a Haltech PD16A power-distribution
module, a Haltech WB1 wideband controller and an FDI DETA10A IMU, plus a
UM980 GNSS receiver on serial0. Read it alongside this document.

## `vehicle.yaml` schema

Declares the physical transports on the car: what buses and serial ports
exist, which DBCs and drivers are attached to them, and whether host
metrics are collected. It does not say what any signal means; `catalog.yaml`
does that.

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
      # by the pit-side ntrip-client) and forward them to the receiver.
      # This happens automatically when a capable driver is attached to
      # a serial source. This file does not configure it.

host:
  enabled: <bool>              # collect host/system metrics (cpu, mem, disk, net) as sys.* channels
  interval: <duration>         # polling interval, e.g. "5s"
  temperatures:                # optional: board-neutral aliases for thermal sensors
    <alias>: <chip>.<label>    #   emits host:temp.<alias> from host:temp.<chip>.<label>
```

### `interface`, `port` and `host.temperatures` belong to the board, not the car

Three settings above describe the SBC and not the vehicle: `interface` is a
socketCAN device name, `port` is a path under `/dev`, and `host.temperatures`
names the thermal sensors the kernel exposes. If the same car moves to
a different SBC, every DBC, channel and policy is unchanged and these three
settings change.

They are written here because a profile must be complete on its own.
They can also be overridden per board without editing this file, by a
`hardware.yaml` under `deploy/targets/` selected with `OPENLAPS_HARDWARE`
(ADR 0010):

```yaml
target: luckfox-omni3576
buses:
  can0: { interface: can0, bitrate: 1000000 }
serial:
  serial0: { port: /dev/ttyS4, baud: 115200 }
host:
  temperatures: { cpu: soc_thermal.0 }
```

The catalog maps temperatures through the aliases (`from: "host:temp.cpu"`)
and never through a sensor name, so a channel such as
`sys.host.temp_cpu_package` is the same channel on every board. The raw
`host:temp.<chip>.<label>` refs are emitted too. A profile can map one to
use a specific sensor, and is then tied to that board. When an alias names a sensor the host does not
have, the agent logs the board's actual sensor names once and emits nothing
for that alias.

The overlay addresses transports by the `name` above (the same identifier
that catalog `from:` refs use) and fails at load if it names a
transport this file does not define. Besides `interface`, `bitrate`, `port`
and `baud` it may set two things that concern the host and not the car:

- `buses.<name>.link` (`fd`, `dbitrate`): arguments that `tools/can_up.py`
  passes to `ip link`, for a controller that does not come up without them.
- `serial.<name>.driver.configure_on_start`: whether a real receiver is
  connected to this port. `tools/bench-hardware.yaml` sets it false because
  the bench rig's `serial0` is a pty. Nothing else in a driver block can be
  overridden: `rate_hz`, `sentences`, `pps` and `timing_output` describe the
  receiver the car carries and belong here.

`deploy/targets/README.md` is the operator document.

Buses and serial sources are generic transports. They decode to
source-native signals (`MESSAGE.SIGNAL` for CAN, `SENTENCE.field` for
serial) and carry no domain meaning. Moving a GPS receiver from serial to
CAN, or adding a second CAN bus, requires an edit to `vehicle.yaml` and
`catalog.yaml`. It requires no change to code or to any consumer. See the
worked example below.

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
                                 #   (0 = report on change; the only deadband a bool or string takes)
      min_interval: <duration>   #   never publish more often than this even if noisy
      max_interval: <duration>   #   always publish at least this often (heartbeat, bounds fill-forward)
    encode:                       # optional wire-encoding override, see "Wire encoding" below
      type: double|float|uint|int #   protobuf value arm to encode this channel's samples with
      scale: <number>             #   fixed-point scale (uint/int only), see below
      offset: <number>            #   fixed-point offset (uint/int only), see below
                                   #   shorthand: `encode: float32` == `encode: {type: float}`,
                                   #   `encode: int64` == `encode: {type: int}`; a bare
                                   #   `encode: uint`/`int`/`float`/`double` is also accepted

apps:
  lap_timing:
    position: <channel-glob>     # e.g. "position.*" -- which channels feed the timing engine
    track: <string>              # track definition name, resolved under profiles/<profile>/tracks/
  fusion:                        # optional IMU/GNSS fusion -> fused.* channels; see FUSION.md
    imu: {accel: [x, y, z channels], gyro: [x, y, z channels], axes: [x, y, z], mount_rpy_deg: [0, 0, 0]}
    gnss: {lat, lon, speed, course, mode?, alt?, lat_std?, lon_std?, alt_std?, latency_ms, lever_arm_m}
    output_rate_hz: 50
```

See [Track setup](TRACK_SETUP.md) for preparing the KML timing lines and
optional JSON metadata selected by `apps.lap_timing.track`.

`rbe` controls what reaches the durable JetStream stream and Timescale. It
does not affect the timing engine, which reads the sample stream **before
the RBE filter**, so lap timing always receives every position sample (see
`AGENT_DESIGN.md`).

The catalog does not configure the pit's live gauge panels. Which channels
are republished to MQTT for live viewing, and at what rate, is **pit-side
configuration** owned by the `live-decoder` service. The pit can change
what it views without editing the file that describes the car. The catalog
has no live-rate setting.

## `alarms.yaml` schema

Alarm limits for the car, rendered into Grafana alert rules by
`tools/gen_alert_rules.py` (ADR 0011). They are in the profile because
they are facts about the car (eg the oil pressure this engine needs at a
given RPM) and because the catalog in the same profile gives each limit its
unit. The rendered provisioning file under
`deploy/pit-config/grafana/provisioning/alerting/` is generated output; a
test fails when it is stale.

```yaml
group: <string>                  # rendered file and rule-group name

alarms:
  <rule-uid>:                    # stable; alert history is keyed on it
    kind: threshold|stale|watch|heartbeat   # optional, default threshold
    channel: <canonical.name>
    units: <string>|none         # the unit the numbers below are in -- required
    above: <number>              # firing threshold (threshold kind: exactly one
    below: <number>              #   of above / below)
    clear_below: <number>        # optional hysteresis for an `above` alarm
    clear_above: <number>        # optional hysteresis for a `below` alarm
    older_than_s: <number>       # stale kind: seconds without a sample
    severity_at_least: critical|warning     # watch kind: open findings to count
    for: <duration>              # how long the condition holds before firing
    severity: critical|warning|none         # optional, default critical
    gate: on_track|always|engine_running    # optional, default on_track
    resting: <number>            # value evaluated while gated off / no sample
    when: { channel: <name>, at_least: <n> }   # optional second-channel condition
    summary: <string>            # the alert title
    panel: <int>                 # reliability-dashboard panel that explains it
```

**Units are declared, converted, and checked.** The generator reads the
channel's `units` from `catalog.yaml` and converts the declared limit into
it. For example, `110 °C` on a channel the catalog reports in `K` renders as
`383.15`, with both values written into the query as a comment. Temperature
and pressure units are converted. Spelling aliases (`V`/`Volts`,
`degC`/`°C`) are treated as equal. Any other unit must match the catalog
exactly or the generator refuses. A channel outside the catalog (`sys.*`,
`lap.*`, `timing.*`) must still declare `units` and is never converted. An
alarm with no `units` is an error. There is no default, because a missing
unit can put a Celsius limit on a Kelvin channel.

**`resting` is the value the rule evaluates when it is gated off.** An
`on_track` alarm evaluates as `resting` while the last `lap.event` says the
car is in the pits, and when no sample exists yet. `resting` must be on the
non-firing side of the threshold. A refuelling stop produces the same data
as a CAN failure (the ECU is powered down while the logger keeps
publishing), so every car-channel alarm is gated. `live-feed-stale` is the
alarm that detects a real loss of data while the car is on track.

## Wire encoding (`encode:`)

Every channel's samples are encoded as one of four protobuf value arms:
`double` (8-byte, the default), `float` (4-byte single-precision), `uint`
(unsigned varint) or `int` (signed varint). `encode:` lets a catalog
author replace the `double` default for a channel with a smaller wire
representation. It is the setting with the largest effect on bandwidth:
applied broadly it reduced offered load by **24% to 26%** (see
[`docs/LINK_BUDGET.md`](LINK_BUDGET.md) section 6 for the measurement).
Every pit decoder (`tools/decode.py`, and every pit service that
decodes `SampleBatch`) must apply a channel's declared `encode`. It
changes which oneof arm is populated on the wire and, for `uint`/`int`,
which transform recovers the physical value.

- `type: double`: the default. It does not need to be written.
- `type: float`: single-precision float. Suitable for real-valued channels
  (temperatures, pressures, voltages) whose physical precision is well
  below single-float resolution.
- `type: uint` / `type: int`: a varint, as few as 1-2 bytes for
  small-magnitude values. Suitable for integer channels (counters,
  raw RPM), and, combined with `scale`/`offset`, for fractional physical
  quantities that do not need the dynamic range of a double.

`scale` and `offset` are **only valid when `type` is `uint` or `int`**.
The catalog loader (`EncodeConfig` in `src/core/config.py`) rejects them
on `double`/`float` channels, and rejects `encode:` on `bool`/
`string` channels (which have no fixed-point representation).
A `scale` of `0` (the default) means "unscaled": the wire value is the
physical value. A non-zero `scale` activates the fixed-point convention
`docs/WIRE_FORMAT.md` defines at the wire level:

```
wire_value = round((physical - offset) / scale)
physical   = wire_value * scale + offset
```

**Worked example:** `car.coolant_temp` (`profiles/example-club-racer/`) is
coolant temperature in Kelvin, physically ranging roughly 250-400 K:

```yaml
channels:
  car.coolant_temp:
    from: "can0:haltech.TEMPERATURE1.COOLANT_TEMPERATURE"
    units: "K"
    encode: { type: uint, scale: 0.1, offset: 0 }
```

A reading of 300.0 K encodes as `wire_value = round((300.0 - 0) / 0.1) =
3000`, which fits a 1-2 byte varint instead of an 8-byte double. It
decodes back to `3000 * 0.1 + 0 = 300.0 K`, with 0.1 K precision.

## Canonical naming convention

**Flat `car.*` namespace, snake_case, one level.** Every on-vehicle
sensor/actuator reading (engine, chassis dynamics, wheels, fuel,
electrics and so on) is a single `car.<name>` channel. There is no
domain sub-taxonomy (no `engine.*`, `chassis.*`, `wheels.*`, `fuel.*`,
`electrics.*`). The reason is in ADR 0004's 2026-07-27 amendment: a domain
taxonomy raises classification questions that have no correct answer and
do not affect how a consumer uses the channel (eg whether throttle
position belongs to `engine.*` or `chassis.*`, or whether a vehicle speed
derived from wheel speed belongs to `wheels.*` or `engine.*`). Consumers
match on the full channel name or a glob and do not need a domain prefix
to group by.

Reserved namespaces outside `car.*`:

| Namespace | Covers |
|---|---|
| `car.*` | Every on-vehicle sensor/actuator reading: ECU engine/drivetrain state, IMU/dynamics, wheel speeds, fuel system, electrics, power-distribution/keypad device health. Flat, one level, snake_case. |
| `position.*` | GNSS: lat, lon, speed, heading, fix quality; altitude, satellites and HDOP (GGA); 1-sigma error (GST) |
| `sys.*` | Host metrics and vehicle-agent health (queue depths, dropped-sample counters, link status). `sys.host.*` channels are mapped in `catalog.yaml` from `host:` refs; `sys.agent.*` channels are internal to the agent and are not mapped source signals |
| `lap.*`, `timing.*` | **Reserved, derived.** Produced by the timing engine from `apps.lap_timing`'s output (lap/sector events, `delta_best`, `predicted_lap`, distance, `lap_elapsed`). Never appear as a `from:` target in `catalog.yaml`. The timing engine writes them, and they enter the sample stream like any other channel. |
| `fused.*` | **Reserved, derived.** IMU/GNSS fusion output from `apps.fusion` (position, speed, attitude, sideslip, body-frame acceleration, grade, status). See [Sensor fusion](FUSION.md). |

Pit-owned namespaces, which are not vehicle channels at all (ADR 0011):

| Namespace | Covers |
|---|---|
| `watch.*` | Anomaly-monitor scores and findings computed at the pit from received telemetry (`WATCH.md`). |
| `strategy.*` | Fuel, stop-plan and driver-time numbers computed at the pit. |
| `field.*` | The other cars: standings, laps, passings and flag state ingested from a timing provider. |

These never appear in a catalog, never carry a `from:`, and never cross the
link. A pit service may read any vehicle channel but may publish only
under a pit-owned namespace, so the name of a channel shows whether its
value was derived at the pit or received from the vehicle. The exception is
the two `timing.*_pit` channels published by the pit-side timing
extrapolator. They are older than this rule and are the only
vehicle-namespace names a pit service publishes.

Where the flat name would be ambiguous on its own, a domain word is kept as
part of the name:
`car.engine_demand`, `car.engine_limiting_active`,
`car.engine_protection_severity`, `car.gearbox_oil_temp`. Where it would
not be ambiguous, there is no domain word: `car.rpm`, `car.accel_x`.
Per-corner wheel-speed channels keep an explicit `wheel_`
infix because the bare name (`car.speed_fl`) could not be distinguished
from a GPS-derived speed: `car.wheel_speed_fl`, `_fr`, `_rl`, `_rr`.

**Name the measurement, not the wire.** Canonical names describe physical
meaning and not wiring. This matters most for generic PDM
(power-distribution module) I/O. A PD16A analog input (`AVI1`) or output
driver (`HCO8_3`) has no fixed meaning: it reports whichever sensor or load
is wired to that pin on this car. A different build of the same car (or a
different car using the same PD16A) could have `AVI1` wired to a fuel
surge-tank level sender, a brake-bias potentiometer, or nothing at all.
A channel named `car.avi1_v` would record a wiring decision and not a
measurement. The example catalog therefore names each PDM pin after what
is wired to it on this car, and does not map a pin that has nothing
assigned (see `profiles/example-club-racer/catalog.yaml`, PDM/keypad
section). For example:

```yaml
car.fuel_level_volts: { from: "can0:haltech2.PD16A_AVI_VOLTAGES.PD16A_AVI_4_VOLTAGE", units: "V", rbe: { deadband: 0.05, min_interval: 100ms, max_interval: 5s } }
car.lift_pump_current: { from: "can0:haltech2.PD16A_OUTPUT_STATUS.PD16A_HCO8_8_CURRENT", units: "A" }
```

A profile for a different car must replace these names and refs with its
own wiring.

Device-health diagnostics are different. A PD16A's own temperature,
battery voltage, and total current describe the module itself and not
what is wired to it, so they are meaningful in any build and are
mapped under `car.pd16_*` names (`car.pd16_cpu_temp`,
`car.pd16_battery_v`, `car.pd16_total_current`, etc). The CAN keypad's
buttons are named by function in the same way as the PDM pins
(`car.wiper_button`, `car.start_button`); one button keeps its number
(`car.keypad_button_5`).

Naming decisions that were judgement calls:

- **`car.driveshaft_rpm`, `car.trip_distance_m`, `car.vehicle_speed`**:
  ECU-calculated values (not raw wheel-sensor values). They are flat
  `car.*` names with no domain word because the bare names are not
  ambiguous.
- **`car.ambient_air_temp`, `car.fuel_temp`, `car.gearbox_oil_temp`**:
  the Haltech DBC groups these with its engine temperature messages,
  although the physical medium (ambient air, fuel, gearbox oil) is not
  strictly the engine. The flat namespace avoids that question.
  `car.gearbox_oil_temp` keeps its full descriptive name because it reads
  more clearly than `car.gearbox_temp`.
- **`car.fuel_pressure`, `car.fuel_temp`, `car.fuel_level`,
  `car.fuel_flow_rate`, ...**: all fuel-system channels are named
  `car.fuel_*`, whether the ECU reports them with its engine signals
  (pressure, temperature) or its fuel signals (level, flow, consumption,
  trim).
- **`car.pd16_hco25_12_temp_status`, `car.pd16_hco25_34_temp_status`**:
  these come from the `PD16A_DIAGNOSTICS` message (per-driver-pair thermal
  status) and not from `PD16A_OUTPUT_STATUS`. Unlike the per-output
  current and load channels, they describe PD16A hardware health and not
  wiring. They are mapped with the other PD16 diagnostics.
- **`car.trigger_counter`, `car.trigger_error_count`,
  `car.trigger_sync_level`**: these have no `engine_` prefix, on the
  judgement that "trigger" here clearly means the ignition/crank
  trigger system. A future channel (eg a lap or sector "trigger") could
  collide with them.

## Worked example: adding a second CAN bus carrying GPS

Say GPS moves from `serial0` (NMEA over UART) to a second CAN bus,
`can1`, decoded via a new `gps.dbc`. Only the profile changes. The timing
engine, dashboards, and exports all keep reading `position.*` and do not
change.

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

Only the `from:` refs change when a source moves to another bus or
transport.

`apps.lap_timing` (`position: position.*`, `track: Wanneroo`) does not
change, because it refers only to canonical names and never to a bus or
decoder. The same is true of every consumer downstream of the catalog
mapper.

## Example profile coverage: DBC signals -> canonical channels

This table lists the CAN signals from the four DBCs (`haltech-ecu.dbc`,
`haltech-multiplexed.dbc`, `haltech-wideband.dbc`, `fdi-imu.dbc`) that
`profiles/example-club-racer/catalog.yaml` maps, and the canonical channel
each one maps to. A `*` stands for every matching signal that the catalog
maps. The catalog file is the source; this table repeats it by signal name
for lookup.

The table covers all **145 `car.*` channels**. The `position.*` channels
(serial GNSS) and `sys.host.*` channels (host collector) are not CAN
signals and are not listed.

The PDM and keypad rows (`PD16A_AVI_*`, `PD16A_SPI_*`, `PD16A_HBO_*`,
`PD16A_HCO*`, `KEYPAD_BUTTON_*`) depend on this car's wiring (see "Name the
measurement, not the wire" above). The catalog does not map the PD16A
per-output pin state, retry count and voltage signals, the input state
signals other than `PD16A_SPI_4_STATE`, or the configuration (`_CFG_`) and
`_IN_` signals.

| DBC signal | Canonical channel(s) |
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
| `BRAKE_PEDAL_SWITCH` | `car.brake_pedal_switch_ecu` (the ECU's copy; `car.brake_pedal_switch` is the PDM's 100 Hz input state, `PD16A_SPI_4_STATE`) |
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
| `PD16A_AVI_*_VOLTAGE` | `car.transmission_oil_temp_volts`, `car.indicator_switch_volts`, `car.fuel_level_volts` (inputs 2 to 4; input 1 is not mapped) |
| `PD16A_SPI_*_VOLTAGE` | `car.surge_tank_switch`, `car.brake_pressure_front_volts`, `car.clutch_switch_input`, `car.brake_switch_input` (inputs 1 to 4) |
| `PD16A_SPI_4_STATE` | `car.brake_pedal_switch` |
| `PD16A_HBO_*_HS_CURRENT` | `car.power_steering_pump_1_current`, `car.power_steering_pump_2_current` |
| `PD16A_HCO8_*_CURRENT` | `car.starter_relay_current`, `car.ecu_inj_wideband_supply_current`, `car.head_light_current`, `car.brake_light_current`, `car.wiper_current`, `car.water_injection_pump_current`, `car.fuel_pump_changeover_current`, `car.lift_pump_current`, `car.heater_fan_current`, `car.cam_scavenge_pump_current` (outputs 1 to 10) |
| `PD16A_HCO25_*_HS_CURRENT` | `car.gearbox_pump_current`, `car.thermo_fan_1_current`, `car.ign_coils_current`, `car.fuel_pump_current` (outputs 1 to 4) |
| `PD16A_*_LOAD` | `car.power_steering_pump_1_load` (HBO 1), `car.thermo_fan_1_load` (HCO25 2), `car.fuel_pump_load` (HCO25 4), `car.lift_pump_load` (HCO8 8), `car.cam_scavenge_pump_load` (HCO8 10); the other outputs' loads are not mapped |
| `KEYPAD_BUTTON_*` | `car.wiper_button`, `car.head_light_button`, `car.fuel_pump_changeover_button`, `car.boost_button`, `car.keypad_button_5`, `car.dim_light_button`, `car.pit_speed_limit_button`, `car.thermo_fan_1_overide_button`, `car.hazard_light_button`, `car.rotational_idle_button`, `car.start_button`, `car.heater_fan_overide_button`, `car.gearbox_pump_overide_button`, `car.engine_protection_ack_button`, `car.fuel_pump_overide_button` (buttons 1 to 15) |
| `KEYPAD_NMT_STATE` | `car.keypad_nmt_state` |
