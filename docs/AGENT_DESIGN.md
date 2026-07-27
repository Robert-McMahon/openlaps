# Vehicle agent design

The vehicle agent is one process that turns raw collector output into
catalog-mapped, RBE-filtered, batched telemetry on the local JetStream — and
hosts the on-vehicle timing engine. This document is the implementation spec
for Phases 2–3; `ARCHITECTURE.md` has the system context, `WIRE_FORMAT.md`
the wire encoding, `CATALOG.md` the configuration schema.

## Process model

```
main thread          — config load, wiring, health loop, signal handling
collector threads    — one per configured collector (can0..n, serial0..n, host)
pipeline thread      — mapper → timing tap → RBE → batcher (single consumer)
publisher            — async JetStream publish with bounded in-flight window
```

- **One pipeline thread, many producers.** Collectors are I/O-bound
  (blocking socketCAN / serial reads) and stay dumb: decode, stamp, enqueue.
  All ordering-sensitive work (mapping, RBE state, batching) happens on the
  single pipeline thread, so no shared mutable state needs locking beyond
  the queues.
- **Queues** are bounded (default 10 000 samples per collector,
  configurable). On overflow: drop-oldest, increment a per-collector drop
  counter. Never block a collector thread on a slow pipeline — a stalled
  bus reader loses hardware frames, which is strictly worse than shedding
  our own oldest samples.
- Rationale for threads over asyncio: python-can and pyserial are natively
  blocking; the sample rates involved (thousands/s) are far below thread
  handoff limits; and the pure timing modules are synchronous.

## Sample lifecycle

```
Sample = (source_ref: str-interned, t_mono_ns: int, t_wall_ms: float, value)
```

1. **Capture** — collector stamps `t_mono_ns` (monotonic) as close to the
   hardware read as possible (for CAN, the socketCAN frame timestamp; for
   serial, arrival of the sentence terminator). `t_wall_ms` derives from the
   clock mapping (below).
2. **Map** — the catalog mapper resolves `source_ref → channel_id` via a
   dict built at startup. Unmapped refs increment a counter and are dropped;
   each distinct unmapped ref is logged once at INFO (a car broadcasts
   plenty a profile deliberately ignores — this is normal, not an error).
3. **Timing tap** — if the channel is one the timing engine subscribes to,
   the sample is handed to it *here, before RBE* (see below).
4. **RBE / rate filter** — per-channel policy from the catalog:
   - `deadband: x` — suppress if `|value − last_sent| ≤ x`
   - `min_interval: t` — rate cap; suppress if `t` hasn't elapsed since last sent
   - `max_interval: t` — heartbeat; force-send if `t` elapsed even if unchanged
   Evaluation order: `max_interval` forces a send; else `min_interval`
   suppresses; else `deadband` suppresses; else send. State is per-channel
   `(last_sent_value, last_sent_t_mono)` owned by the pipeline thread.
   Channels with no policy pass through untouched.
5. **Batch** — surviving samples accumulate per source-class; every tick
   (default 20 ms, configurable 10–50) the batcher serializes one
   `SampleBatch` per non-empty class and hands it to the publisher.
6. **Publish** — async JetStream publish to `tele.<vehicle>.<class>` with a
   bounded unacked window (default 1 000). If the window fills (local NATS
   down or wedged), the publisher buffers up to a byte budget then drops
   whole batches oldest-first, counting them. The agent never blocks capture
   because storage is unavailable — but see Failure modes: local NATS being
   down is a loud condition, not a quiet one.

## Clock discipline

- `t_mono_ns` is the ordering and interval truth everywhere internally.
- The wall clock maps from monotonic via an affine `(offset)` estimate:
  initialised from the system clock, and — when a GNSS time source is
  present among the mapped channels (the agent looks for the canonical
  channel `position.time_unix_ms`) — steered gently toward GPS time
  (slew, never step, while running; a step is allowed only at startup —
  including the first GNSS acquisition after boot, since the vehicle has
  no RTC guarantee and slewing away a large system-clock error at ppm
  rates would take hours).
  The current offset and its source (`system` / `gnss`) are published as
  `sys.agent.clock_offset_ms` / `sys.agent.clock_source`.
- Each `SampleBatch` records both epochs (`batch_epoch_unix_ms`,
  `batch_epoch_mono_ns`); per-sample `t_offset_us` is against the batch
  epoch. Consumers therefore never depend on the vehicle's absolute clock
  being right at capture time — a post-hoc corrected mapping can re-derive
  wall times from monotonic epochs if it ever matters.

## Timing engine integration

- The timing engine (ported pure modules: `timing_core`, `distance_model`,
  `reference_lap`) runs inside the pipeline thread, fed by the pre-RBE tap
  on its subscribed channels (`apps.lap_timing` in the catalog names them;
  canonically `position.*`).
- **Pre-RBE is non-negotiable**: timing interpolates line crossings between
  consecutive fixes; feeding it decimated or deadbanded position data
  degrades crossing accuracy for no benefit.
- Outputs are emitted as derived channels (`lap.*`, `timing.*`) by
  re-entering the pipeline *at the mapper stage* with reserved channel IDs
  from the registry. Derived channels get the same RBE/batching treatment as
  any channel (they mostly won't have RBE policies) and land in the
  `derived` source class.
- Event-like outputs (line crossings, lap completions) are samples too —
  e.g. `lap.event` with an encoded event payload — so the wire format stays
  uniform; the pit ingest-writer materialises them into relational rows.
- Session identity (from `cmd.<vehicle>.session`, cached with last-known
  state persisted to disk) is stamped onto derived channels by the timing
  wrapper, mirroring how the predecessor system tagged laps per driver
  stint.

## Registry lifecycle

- At startup the agent builds the `ChannelRegistry` from the catalog:
  stable integer IDs assigned in catalog order, `registry_seq` =
  hash-derived monotonic counter persisted beside the profile (bump on any
  catalog change).
- The registry is published into `TELE` (subject
  `tele.<vehicle>.catalog`) at startup and republished on a slow interval
  (default 5 min) so a pit stream trimmed by retention still always
  contains at least one copy ahead of any batch it holds.
- Every batch carries `registry_seq`; a consumer seeing an unknown seq
  scans back/forward in the stream for the matching registry before
  decoding (see `WIRE_FORMAT.md`).

## Health and status channels

All agent introspection is ordinary telemetry under `sys.agent.*` — visible
on the same dashboards, stored in the same DB, no side channel:

| Channel | Meaning |
| --- | --- |
| `sys.agent.status` | heartbeat (1 Hz); the pit alerts on staleness — replaces the old MQTT LWT |
| `sys.agent.drops.<collector>` | cumulative drop-oldest count per queue |
| `sys.agent.unmapped_refs` | cumulative samples with no catalog mapping |
| `sys.agent.publish_drops` | batches shed because local NATS was unavailable |
| `sys.agent.publish_lag_ms` | age of oldest unacked publish |
| `sys.agent.clock_offset_ms`, `sys.agent.clock_source` | clock discipline state |
| `sys.agent.rbe_suppressed` | cumulative samples suppressed by RBE (sanity check on policies) |

## Startup and shutdown

Startup order: load profile → validate catalog (fail fast and loudly on
schema errors — a mis-typed catalog must not silently drop channels) →
build registry → connect NATS + ensure streams exist (idempotent
`add_stream`) → publish registry → start pipeline → start collectors
(serial drivers run their device configuration routine here, e.g. the UM980
rate/sentence setup, before entering the read loop) → start health loop.

Shutdown (SIGTERM): stop collectors → drain queues through the pipeline →
flush final partial batches → await publisher acks (bounded, 5 s) → publish
a final `sys.agent.status = stopping` sample → exit. A crash loses at most
the unacked window plus one tick — acceptable, and measurable after the
fact from sequence/timestamp continuity.

## Failure modes

| Failure | Behaviour |
| --- | --- |
| Local NATS down at startup | Retry with backoff, capture-and-shed meanwhile; `sys.agent.publish_drops` counts the cost. Do not exit: a rebooting broker must not take the collectors' warm state with it |
| Local NATS dies while running | Same as above; publisher reconnects and resumes. JetStream dedupe (per-batch msg-id = `class:tick_epoch`) makes redelivery after reconnect idempotent |
| Collector thread dies (bus off, unplugged serial) | Supervisor in the health loop restarts it with backoff; restarts and current state are visible via `sys.agent.status` payload. The agent keeps running with the collectors it has |
| Serial device absent at startup | Collector retries open with backoff; not fatal — GPS being unplugged must not stop CAN logging |
| RTCM subject delivers while receiver busy | Driver write-back is best-effort fire-and-forget; corrections are stateless |
| Malformed catalog / DBC | Fatal at startup with a precise error (file, key, reason). Config errors are the one thing that *should* stop the agent |
| Clock steps backward (NTP/GPS at boot) | Immaterial: ordering uses monotonic time only; wall mapping slews |
| Sustained overload (queue drops nonzero) | Nothing breaks; drops are visible per collector. Operator response is a catalog edit (rate caps / RBE), not a restart |

## Configuration surface

Everything comes from the profile (`vehicle.yaml`, `catalog.yaml`) plus a
small env set (documented in `example.env`): NATS URL + creds path, vehicle
id override, tick length, queue/window sizes. No signal names, rates, or
policies in env — the profile is the single place a car is described.
