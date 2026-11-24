# Vehicle agent design

The vehicle agent is one process. It maps raw collector output to catalog
channels, applies the RBE filter, batches the samples and publishes them
to the local JetStream. It also runs the on-vehicle timing engine and the
sensor fusion filter. This document describes the current
implementation. `ARCHITECTURE.md` has the system context, `WIRE_FORMAT.md`
the wire encoding, and `CATALOG.md` the configuration schema.

## Process model

```
main thread          config load, wiring, signal handling
collector threads    one per configured collector (can0..n, serial0..n, host)
pipeline thread      mapper → timing and fusion tap → RBE → batcher (single consumer)
publisher thread     async JetStream publish with bounded in-flight window
live thread          live sampler: newest values of watched channels, by UDP
health thread        collector supervision, sys.agent.* samples (1 Hz)
```

- **One pipeline thread, many producers.** Collectors are I/O-bound
  (blocking socketCAN and serial reads) and do three things: decode,
  timestamp, enqueue. All work that depends on order (mapping, RBE state,
  batching) is done on the single pipeline thread, so the queues are the
  only shared state that needs locking.
- **Queues** are bounded (default 10 000 samples per collector,
  configurable). On overflow the oldest sample is dropped and a
  per-collector drop counter is incremented. A collector thread never
  blocks on a slow pipeline. A blocked bus reader would lose hardware
  frames, which is worse than dropping the agent's own oldest samples.
- **The live thread** exists only when `OPENLAPS_LIVE_PORT` is set
  (`src/agent/live.py`). Each tick the pipeline thread hands it the same
  serialized batches that go to the publisher. It keeps the newest value
  of each channel the pit has selected and sends the changed values to
  the pit as UDP datagrams every `OPENLAPS_LIVE_TICK_MS` (default 100 ms).
  It queues and retries nothing. See `WIRE_FORMAT.md`, "Live link".
- Threads are used instead of asyncio for three reasons: python-can and
  pyserial are blocking libraries, the sample rates involved (thousands
  per second) are far below what a thread handoff can carry, and the
  timing modules are synchronous.

## Sample lifecycle

```
Sample = (source_ref: str-interned, t_mono_ns: int, t_wall_ms: float, value)
```

1. **Capture.** The collector records `t_mono_ns` (monotonic) as close to
   the hardware read as possible (for CAN, the socketCAN frame timestamp;
   for serial, the arrival of the sentence terminator). `t_wall_ms` is
   derived from the clock mapping (below).
2. **Map.** The catalog mapper resolves `source_ref → channel_id` with a
   dict built at startup. Unmapped refs increment a counter and are dropped.
   Each distinct unmapped ref is logged once at INFO. This is normal and
   not an error: a car broadcasts many signals that a profile deliberately
   ignores.
   The agent gives each CAN collector the catalog's source refs, so a frame
   with no mapped signal is counted (`unwanted_frames`) and skipped before
   decoding, and unmapped signals of the remaining frames never reach the
   mapper. On the example car that is a third of the frames and two thirds
   of the samples. A collector built without that set (the bench and replay
   tools) still emits everything its DBCs know.
   **Compiled CAN ingest.** When the `openlaps_canfast` extension
   (`rust/canfast`) is installed, steps 1, 2, 4 and 5 for a CAN bus run
   on a separate thread outside the interpreter. That thread owns the bus
   socket, timestamps and decodes frames, applies the RBE policies and
   serializes the bus's `SampleBatch` payloads. Python builds its plan from
   the DBCs and the catalog at start (`collectors/can_fast.py`), and the
   pipeline thread collects finished batches once a tick, together with
   the pre-RBE samples the timing and fusion apps subscribe to (step 3).
   `tests/test_can_fast.py` checks that the two paths produce
   byte-identical output. `OPENLAPS_CAN_INGEST` selects `auto` (default),
   `python` or `compiled`.
3. **Timing and fusion tap.** If the channel is one the timing engine or
   the fusion filter subscribes to, the sample is handed to it here, before
   RBE (see below).
4. **RBE / rate filter.** Per-channel policy from the catalog:
   - `deadband: x`: suppress if `|value − last_sent| ≤ x`
     (`deadband: 0` is report-on-change, and works on booleans and strings)
   - `min_interval: t`: rate cap; suppress if `t` has not elapsed since last sent
   - `max_interval: t`: heartbeat; send if `t` has elapsed even if unchanged
   Evaluation order: `max_interval` forces a send; else `min_interval`
   suppresses; else `deadband` suppresses; else send. State is per-channel
   `(last_sent_value, last_sent_t_mono)` owned by the pipeline thread.
   Channels with no policy pass through unchanged.
5. **Batch.** The remaining samples accumulate per source class. Every
   tick (default 20 ms, configurable 10–50) the batcher serializes one
   `SampleBatch` per non-empty class and hands it to the publisher and,
   when the live link is enabled, to the live sampler.
6. **Publish.** Async JetStream publish to `tele.<vehicle>.<class>` with a
   bounded unacked window (default 1 000). If the window fills (local NATS
   down or not responding), the publisher buffers up to a byte budget and
   then drops whole batches, oldest first, and counts them. The agent
   never blocks capture because storage is unavailable. Local NATS being
   down is still reported as a fault (see Failure modes).

## Clock discipline

- `t_mono_ns` is used for all ordering and intervals inside the agent.
- The externally disciplined system clock is the only wall-time authority.
  Each conversion samples realtime and monotonic together, so chrony's
  runtime slew is observed without a second in-process steering loop.
- Chrony state is ordinary host telemetry:
  `sys.host.clock_offset_s`, `sys.host.clock_source`,
  `sys.host.clock_stratum` and `sys.host.clock_root_dispersion_s`.
- Each `SampleBatch` records both epochs (`batch_epoch_unix_ms`,
  `batch_epoch_mono_ns`). The per-sample `t_offset_us` is relative to the
  batch epoch. Consumers therefore do not depend on the vehicle's absolute
  clock being correct at capture time. If needed, wall times can be
  derived again later from the monotonic epochs with a corrected mapping.

## Timing engine integration

- The timing engine (pure modules: `timing_core`, `distance_model`,
  `reference_lap`) runs inside the pipeline thread. It is fed by the
  pre-RBE tap on its subscribed channels (`apps.lap_timing` in the catalog
  names them; normally `position.*`).
- **The tap must be before RBE.** Timing interpolates line crossings
  between consecutive fixes. Position data that has been decimated or
  deadbanded gives less accurate crossings.
- Outputs are emitted as derived channels (`lap.*`, `timing.*`). They
  enter the pipeline again at the mapper stage with reserved channel IDs
  from the registry. Derived channels get the same RBE and batching as
  any channel (most have no RBE policy) and are published in the
  `derived` source class.
- Events (line crossings, lap completions) are samples too, eg
  `lap.event` with an encoded event payload, so the wire format has one
  message shape. The pit ingest-writer turns them into relational rows.
- Session identity (from `cmd.<vehicle>.session`, with the last known
  state persisted to disk) is added to derived channels by the timing
  wrapper, so every lap is attributed to a session, driver and stint.
- The sensor fusion filter (`src/agent/fusion_app.py`, `FUSION.md`) is
  hosted the same way: it runs in the pipeline thread on the pre-RBE tap
  and emits `fused.*` derived channels.

## Registry lifecycle

- At startup the agent builds the `ChannelRegistry` from the catalog.
  Integer IDs are stable and assigned in catalog order. `registry_seq` is
  a counter persisted together with a hash of the catalog (in
  `OPENLAPS_STATE_DIR`, or beside the profile when that is unset). It is
  incremented whenever the hash changes.
- The registry is published into `TELE` (subject
  `tele.<vehicle>.catalog`) at startup and republished at a slow interval
  (default 5 min), so that a pit stream trimmed by retention still
  contains at least one copy ahead of any batch it holds.
- Every batch carries `registry_seq`. A consumer that sees an unknown seq
  scans back or forward in the stream for the matching registry before
  decoding (see `WIRE_FORMAT.md`).

## Health and status channels

All agent health data is ordinary telemetry under `sys.agent.*`. It is
shown on the same dashboards and stored in the same database as every
other channel, and has no separate transport:

| Channel | Meaning |
| --- | --- |
| `sys.agent.status` | heartbeat (1 Hz); the pit alerts when it goes stale |
| `sys.agent.drops.<collector>` | cumulative drop-oldest count per queue |
| `sys.agent.stall_s.<bus>` | seconds the kernel has been counting frames on the bus that the collector never received (0 when healthy or quiet); the vehicle-side watchdog reboots on the same condition (`collectors/can_stall.py`) |
| `sys.agent.socket_drops.<bus>` | cumulative frames the kernel discarded on the bus socket because its buffer was full |
| `sys.agent.capture_lag_ms.<bus>` | worst delay since the last report between a frame's kernel timestamp and the collector decoding it; it rises while the socket buffer is filling, before anything is dropped |
| `sys.agent.unmapped_refs` | cumulative samples with no catalog mapping |
| `sys.agent.publish_drops` | batches dropped because local NATS was unavailable |
| `sys.agent.publish_duplicates` | batches the local server discarded because their dedupe id was already taken |
| `sys.agent.publish_lag_ms` | age of oldest unacked publish |
| `sys.agent.rbe_suppressed` | cumulative samples suppressed by RBE (a check on the policies) |
| `sys.agent.encode_failures` | cumulative tick windows discarded by an encode error. A catalog channel with the wrong type removes data from the stream without any other sign, so alert on any rise |
| `sys.agent.cpu.<role>` | share of one core used by each thread role (each collector, `pipeline`, `publisher`, `live`, `health`, `main`) since the last report |

## Startup and shutdown

Startup order: load profile → validate catalog (a schema error stops the
agent with an error, so that a mistyped catalog cannot silently drop
channels) → build registry → connect NATS and ensure streams exist
(idempotent `add_stream`) → publish registry → start the live thread, if
enabled → start pipeline → start collectors (serial drivers run their
device configuration routine here, eg the UM980 rate and sentence setup,
before entering the read loop) → start health loop.

Shutdown (SIGTERM): stop collectors → drain queues through the pipeline →
flush final partial batches → await publisher acks (bounded, 5 s) → publish
a final `sys.agent.status = stopping` sample → exit. A crash loses at most
the unacked window plus one tick. This loss is accepted, and it can be
measured afterwards from sequence and timestamp continuity.

## Failure modes

| Failure | Behaviour |
| --- | --- |
| Local NATS down at startup | Retry with backoff. Capture continues and batches that cannot be buffered are dropped; `sys.agent.publish_drops` counts them. The agent does not exit, so the collectors keep their state while the server restarts |
| Local NATS dies while running | Same as above; publisher reconnects and resumes. JetStream dedupe (per-batch msg-id = `class:tick_epoch:sequence`, unique per batch and kept across retries) makes redelivery after reconnect idempotent |
| Collector thread dies (bus off, unplugged serial) | Supervisor in the health loop restarts it with backoff; restarts and current state are visible in the `sys.agent.status` payload. The agent keeps running with the collectors it has |
| Serial device absent at startup | Collector retries open with backoff; not fatal. An unplugged GPS must not stop CAN logging |
| RTCM subject delivers while receiver busy | The driver writes corrections to the receiver on a best-effort basis and does not retry; corrections are stateless |
| Malformed catalog / DBC | Fatal at startup with a precise error (file, key, reason). Configuration errors are the only failures that stop the agent |
| Clock steps backward (NTP/GPS at boot) | No effect on ordering, which uses monotonic time only; the wall mapping slews |
| Sustained overload (queue drops nonzero) | The agent keeps running; drops are visible per collector. The operator's response is a catalog edit (rate caps / RBE), not a restart |

## Configuration surface

Everything comes from the profile (`vehicle.yaml`, `catalog.yaml`) plus a
small set of environment variables (documented in `example.env`): NATS URL
and creds path, vehicle id override, tick length, queue and window sizes,
and the live link port and key. No signal names, rates or policies are in
the environment. The profile is the only place where a car is described.

One optional file sits between the two: `OPENLAPS_HARDWARE` names a host
wiring overlay (ADR 0010) that substitutes socketCAN interface names and
serial device paths into the loaded profile. It is deliberately not part of
the environment set. A device path is structured configuration with a
schema, and it fails at load with a precise error like every other
configuration error. The profile describes the car; the overlay describes
the board the car's wiring is connected to.
