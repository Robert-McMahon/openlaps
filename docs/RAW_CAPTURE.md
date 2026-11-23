# Raw input capture

While the system runs, the vehicle can also capture every raw input (CAN
frames before DBC decode, serial NMEA lines before parsing) to local
files. A race weekend then produces development data that can be
replayed. Capture stays on the vehicle's disk (`OPENLAPS_RAW_CAPTURE_DIR`,
normally on the NVMe). Nothing is sent over the radio.

Captures can be used as test fixtures without conversion.
`tools/replay.py` reads these formats directly (`--candump`, `--nmea`),
and `tests/fixtures/candump/` holds slices of the same candump text. A
stint captured at a race replays through the real collector → pipeline →
publisher path.

## What captures, and how

| Input | Mechanism | Format |
| --- | --- | --- |
| CAN bus | `openlaps-canlog <iface>` service relaying `candump -L` (socketCAN is broadcast; the agent's collector is unaffected) | candump log text: `(sec.usec) can0 360#0000040300510000` |
| Serial GNSS | The agent's `SerialCollector` copies each received line before decoding, enabled by `raw_log: true` on the profile's serial source | `(wall-clock seconds) $GNRMC,...`: the raw line as received, including lines the decoder would skip |

Both write through the same `RawLogWriter`
(`src/collectors/rawlog.py`). Each capture start creates one *run
directory*, holding a `manifest.json` (source, format, start wall and
monotonic clocks) and numbered segment files:

```
$OPENLAPS_RAW_CAPTURE_DIR/
  can0/20260912T083000Z/manifest.json 0001.log 0002.log ...
  serial0/20260912T083001Z/manifest.json 0001.log ...
```

Runs are independent per source. Sources are correlated by the wall-clock
timestamps in the lines, not by matching directories.

## Durability: what a power cut costs

Power to a race car is often cut abruptly, usually at the end of a
session. The writer limits what is lost:

- the current segment is flushed **and fsync'd every 1 s**, so a hard cut
  loses about the last second. Without this, the kernel's writeback
  timers would allow 5–35 s to be lost;
- segments **rotate every 10 min** and are fsync'd (file and directory
  entry) on close, so everything before the current segment is durable;
- the format is append-only text, so the only damage to recover from is a
  truncated final line, which every consumer skips.

A capture failure does not affect telemetry. A write failure in the
serial copy counts `raw_log_failures`, drops the writer, and retries with
a new run after 60 s. `openlaps-canlog` exits nonzero so that systemd
restarts it into a new run.

## Storage budget

Measured on the example profile's bus load (~1,530 frames/s on one 1 Mbps
bus) and 50 Hz RMC: **~280 MB/hour total, under 7 GB for a 24 h race**,
which is about 1% of the vehicle NVMe's free space. To archive many
events, compress closed runs after the session (`zstd -T0 --rm ...`,
~5.7×). Do not compress while capturing, because a power cut would lose
the buffered compression frame.

## Deploying

1. Set `OPENLAPS_RAW_CAPTURE_DIR` in `/etc/openlaps/agent.env`
   (eg `/mnt/data/captures`).
2. CAN, one instance per bus:
   `systemctl enable --now openlaps-canlog@can0`
   (`deploy/systemd/openlaps-canlog@.service`).
3. Serial: set `raw_log: true` on the source in the profile's
   `vehicle.yaml`, and uncomment the capture `ReadWritePaths`/
   `ExecStartPre` lines in `openlaps-agent.service` so the agent's sandbox
   can reach the capture root.

The Compose deployment (`deploy/vehicle-compose.yaml`) is not set up for
capture. The bench and the car run the systemd units.
