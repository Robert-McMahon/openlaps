# Raw input capture

Alongside the running system, the vehicle can capture every raw input —
CAN frames before DBC decode, serial NMEA lines before parsing — to local
files, so a race weekend leaves behind replayable development data. Capture
stays on the vehicle's disk (`OPENLAPS_RAW_CAPTURE_DIR`, normally on the
NVMe); nothing crosses the radio.

The payoff is that captures are **already fixtures**: `tools/replay.py`
consumes exactly these formats (`--candump`, `--nmea`), and
`tests/fixtures/candump/` holds slices of the same candump text. A stint
captured at a race replays through the real collector → pipeline →
publisher path with no conversion step.

## What captures, and how

| Input | Mechanism | Format |
| --- | --- | --- |
| CAN bus | `openlaps-canlog <iface>` service relaying `candump -L` (socketCAN is broadcast; the agent's collector is unaffected) | candump log text: `(sec.usec) can0 360#0000040300510000` |
| Serial GNSS | The agent's `SerialCollector` tees each received line pre-decode, enabled by `raw_log: true` on the profile's serial source | `(wall-clock seconds) $GNRMC,...` — raw line as received, including lines decode would skip |

Both write through the same `RawLogWriter`
(`src/collectors/rawlog.py`): one *run directory* per capture start,
holding a `manifest.json` (source, format, start wall/monotonic clocks) and
numbered segment files:

```
$OPENLAPS_RAW_CAPTURE_DIR/
  can0/20260912T083000Z/manifest.json 0001.log 0002.log ...
  serial0/20260912T083001Z/manifest.json 0001.log ...
```

Runs are independent per source — correlation is by the wall-clock
timestamps in the lines, not by directory pairing.

## Durability: what a power cut costs

Race power gets cut abruptly, usually right at the end of a session — the
data you most want. The writer bounds that:

- the current segment is flushed **and fsync'd every 1 s**, so a hard cut
  loses roughly the last second, not the 5–35 s the kernel's writeback
  timers would otherwise allow;
- segments **rotate every 10 min** and are fsync'd (file and directory
  entry) on close, so everything before the current segment is durable
  outright;
- the format is append-only text: the recovery case is a truncated final
  line, which every consumer trivially skips.

Capture never endangers telemetry: a write failure in the serial tee counts
`raw_log_failures`, drops the writer, and retries a fresh run after 60 s;
`openlaps-canlog` exits nonzero so systemd restarts it into a fresh run.

## Storage budget

Measured on the example profile's bus load (~1,530 frames/s on one 1 Mbps
bus) and 50 Hz RMC: **~280 MB/hour total, under 7 GB for a 24 h race** —
about 1% of the vehicle NVMe's free space. Compress closed runs *after*
the session (`zstd -T0 --rm ...`, ~5.7×) if you want to archive many
events; never compress in-flight, because a power cut would take the
buffered compression frame with it.

## Deploying

1. Set `OPENLAPS_RAW_CAPTURE_DIR` in `/etc/openlaps/agent.env`
   (e.g. `/mnt/data/captures`).
2. CAN, one instance per bus:
   `systemctl enable --now openlaps-canlog@can0`
   (`deploy/systemd/openlaps-canlog@.service`).
3. Serial: set `raw_log: true` on the source in the profile's
   `vehicle.yaml`, and uncomment the capture `ReadWritePaths`/
   `ExecStartPre` lines in `openlaps-agent.service` so the agent's sandbox
   can reach the capture root.

The docker path (`deploy/vehicle-compose.yaml`) is not wired for capture;
the bench and car run the systemd units.
