# profiles/example-club-racer/dbcs/

DBC files referenced by this profile's `vehicle.yaml`, all on `can0`:

| File                          | Device alias | Covers |
|--------------------------------|--------------|--------|
| `haltech-ecu.dbc`              | `haltech`    | Haltech Elite ECU broadcast stream: engine (RPM, MAP, temps, pressures, lambda, ignition/injection timing, knock), wheel speed, vehicle speed, fuel system, driver-input switches, lighting, and the PDM_INFO diagnostic frame. |
| `haltech-multiplexed.dbc`      | `haltech2`   | Haltech PD16A power-distribution module (multiplexed CAN: rail voltages, per-output current/voltage/load/retry status, analog/switched inputs) and the 15-button CAN keypad (CANopen node 12). |
| `haltech-wideband.dbc`         | `wideband`   | Haltech WB1 CAN wideband lambda controller: lambda, sense resistor, diagnostic, and controller supply voltage. |
| `fdi-imu.dbc`                  | `imu`        | FDI DETA10A IMU: accelerometer, gyroscope, UKF-fused Euler angles (roll/pitch/yaw), and board temperature. |

`catalog.yaml` maps the signals in these files to canonical channel names;
see `docs/CATALOG.md` for the full coverage table and naming convention.
