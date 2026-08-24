# candump replay fixtures

Trimmed `candump`-format captures of real CAN traffic from the car the
example profile describes, used by `tests/test_can_collector.py` to exercise
the production decode path (the same DBCs shipped in
`profiles/example-club-racer/dbcs/`) without any hardware.

| File | Contents |
| --- | --- |
| `candump-sample.log` | 45 861 frames (30.0 s) of Haltech ECU + PD16A + WB1 wideband traffic, spanning a real engine start: ~6 s key-on, the cranking transient (battery sags to 10.4 V), fire-up to a 2,015 rpm peak, then idle. `TOTAL_FUEL_USED` climbs 0 → 11 cc in monotonic 1 cc steps, so fuel-counter deltas are testable against it. |
| `candump-keyon-sample.log` | 8 000 frames (5.3 s) of the same bus, key-on/engine-off: `ENGINE_SPEED` 0, manifold pressure atmospheric, hot coolant. Kept because "engine off" is a legitimate state — a derived channel reading 0 here is correct, not dead. |
| `candump-imu-sample.log` | 1 255 frames of FDI DETA10A IMU traffic |

All are raw bus traffic only — no session, driver, or event metadata. Read
them with `can.CanutilsLogReader`; the collector's `replay()` takes the
resulting `can.Message` iterable directly.

`candump-sample.log` and `candump-keyon-sample.log` are windows of the same
garage capture (`candump-2026-05-23_085133.log`, 154.9 s, kept off-repo on
the vehicle SBC): the key-on file is its first 8,000 frames, the engine-start
file is t+12 s → t+42 s. `candump-imu-sample.log` pre-dates openlaps and came
from the predecessor logger's replay fixtures. They are checked in here so
the tests are self-contained.
