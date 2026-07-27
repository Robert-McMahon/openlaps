# candump replay fixtures

Trimmed `candump`-format captures of real CAN traffic from the car the
example profile describes, used by `tests/test_can_collector.py` to exercise
the production decode path (the same DBCs shipped in
`profiles/example-club-racer/dbcs/`) without any hardware.

| File | Contents |
| --- | --- |
| `candump-sample.log` | 8 000 frames of Haltech ECU + PD16A + WB1 wideband traffic, captured at idle/walking pace while loading the car onto a trailer |
| `candump-imu-sample.log` | 1 255 frames of FDI DETA10A IMU traffic |

Both are raw bus traffic only — no session, driver, or event metadata. Read
them with `can.CanutilsLogReader`; the collector's `replay()` takes the
resulting `can.Message` iterable directly.

The captures pre-date openlaps and came from the predecessor logger's own
replay fixtures; they are checked in here so the tests are self-contained.
