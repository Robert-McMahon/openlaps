# Wire format

The wire format is how the vehicle agent gets samples off the car and onto
the pit's TimescaleDB and live dashboards: NATS JetStream subjects carrying
protobuf messages defined in [`proto/telemetry.proto`](../proto/telemetry.proto).
This document is the prose companion to that schema — read them together.

## Subject hierarchy

All openlaps traffic lives under a small set of subject prefixes. `<vehicle>`
is a short, stable identifier for the car (e.g. `example-club-racer`); it is
the same string as `ChannelRegistry.vehicle_id`.

| Subject pattern | Direction | Transport | Purpose |
| --- | --- | --- | --- |
| `tele.<vehicle>.<source-class>` | vehicle → pit | JetStream (`TELE`) | Telemetry data: `ChannelRegistry` and `SampleBatch` messages |
| `cmd.<vehicle>.session` | pit → vehicle | JetStream (`CMD`) | Session/driver state, track selection |
| `rtcm.<vehicle>` | pit → vehicle | core NATS (no stream) | RTK correction bytes for the GPS receiver |

`<source-class>` is the name of the collector that produced the batch — for
example `can0`, `can1`, `serial0`, `host`, or `derived` for channels
synthesised by an in-agent app such as the timing engine (`lap.*`,
`timing.*`). Source-class is a routing concern only: it says which collector
published a batch, not which canonical channels are inside it — a single
`can0` batch can carry samples for `engine.*`, `pd16.*` and `chassis.*`
channels side by side, because the channel catalog assigns domain meaning
independently of the source.

Both `ChannelRegistry` and `SampleBatch` messages are published on the same
`tele.<vehicle>.<source-class>` subject for that source-class — there is no
separate registry subject. A JetStream consumer distinguishes the two by
message shape (a registry has no `samples` field; a batch always does) or,
more robustly, by trying to parse as `SampleBatch` first and falling back to
`ChannelRegistry` if that fails, since both are valid protobuf on the wire.
Implementations should tag published messages with a NATS header
(`Openlaps-Msg-Type: registry` / `batch`) so consumers never need to guess.

### `rtcm.<vehicle>` — core NATS only, by design

RTCM correction bytes are published on **core NATS**, not a JetStream
subject, and this is a deliberate choice, not an oversight. A stale RTCM
correction is worse than no correction — feeding old corrections to the GPS
receiver degrades the fix rather than improving it — so there is no value in
durability or redelivery here. Core NATS gives exactly the semantics wanted:
at-most-once, fire-and-forget, no backlog to catch up on after a dropout. If
the link is down when a correction is published, that correction is simply
lost, which is correct: by the time the link comes back, a fresher
correction has already superseded it.

## JetStream stream definitions

Two JetStream streams live on the vehicle's `nats-server`. The pit's
`nats-server` connects to the vehicle over a leafnode and sources `TELE`
from it, so the vehicle-side stream and the pit-side copy are (eventually)
identical — JetStream's resumable sourcing is what makes link dropouts a
non-event: the pit catches up from the last sequence number it has, on
whatever bandwidth is available, live or backlogged, off the same stream.

### `TELE`

| Setting | Value | Rationale |
| --- | --- | --- |
| Subjects | `tele.<vehicle>.>` | Everything the vehicle agent produces — registries and batches, every source-class, live and historical are the same stream |
| Storage | File | Must survive an agent restart; this is the vehicle's only durability until the pit has ingested it |
| Retention | Limits | Age cap ~72 h *and* a size cap (sized to the vehicle's disk budget), whichever is hit first, oldest messages dropped |
| Replicas | 1 (single vehicle node) | No HA requirement on the vehicle; the pit copy is the redundancy |

72 hours comfortably covers "car sits in the garage over a long weekend with
no pit wifi" without needing operator intervention; the size cap is the
backstop for unexpectedly high data rates (e.g. a misbehaving collector
publishing far above its configured rate).

### `CMD`

| Setting | Value | Rationale |
| --- | --- | --- |
| Subjects | `cmd.<vehicle>.>` | Session/driver state, track selection, other pit → vehicle control messages |
| Storage | File | Small volume; durability costs nothing |
| Retention | **Limits, with `max_msgs_per_subject = 1`** | Last-value semantics: a new message on `cmd.<vehicle>.session` supersedes the previous one. A vehicle agent (or timing engine) that (re)starts mid-session reads the single retained message on that subject to recover current session state, instead of replaying a command history — this replaces the MQTT `retained` flag from the old stack. |

`CMD` messages are small and infrequent (driver swaps, track changes), so
the volume argument for retention doesn't apply the way it does for `TELE`;
what matters is that exactly one current value exists per subject.

## Registry lifecycle

`ChannelRegistry` is the only place channel names, units, source refs and
types ever appear on the wire; every `SampleBatch.Sample` refers to a
channel purely by its integer `channel_id`. This keeps batches small (an
integer instead of a repeated string) and gives the catalog a single,
auditable place to change.

1. **Publish triggers.** The vehicle agent publishes a `ChannelRegistry` to
   `tele.<vehicle>.<source-class>` (using the source-class the registry's
   channels mostly belong to, or a dedicated `catalog` source-class if a
   registry spans classes) in two situations: on agent startup, and whenever
   the channel catalog changes (a config reload, a new device coming
   online). Each publish carries a `registry_seq` one higher than the last.
2. **Batches carry the seq they were built against.** Every `SampleBatch`
   has a `registry_seq` field. A consumer decoding a batch must already hold
   (or be able to fetch) the `ChannelRegistry` with that exact seq — decoding
   a batch against the wrong registry produces channel ids that resolve to
   the wrong name/units/type, so this is a hard equality check, not a
   best-effort lookup.
3. **Unknown seq: look back in the stream.** Because `TELE` is a durable,
   replayable JetStream stream, a consumer that sees a `registry_seq` it
   doesn't recognise (a late-joining dashboard, a pit consumer that missed
   the startup publish during a dropout, or a consumer that just came up)
   resolves it by reading backwards through the stream on the relevant
   subject until it finds the `ChannelRegistry` message with that seq. In
   practice this means keeping a small durable consumer (or replaying from
   the earliest retained message once) whose only job is registry recovery.
   Because every registry publish is a full snapshot of the catalog (not a
   delta — see the `Channel` message docs in the proto), finding any one
   `ChannelRegistry` with the right seq is sufficient; there is no need to
   replay and merge multiple registry messages.
4. **Registries are cheap to keep.** Registry messages are small (one entry
   per channel, published rarely) relative to the sample stream, so
   retaining every registry generation for the life of the stream's
   retention window is not a size concern.

## Value encoding

`Sample.value` is a oneof with six arms: `d` (double), `i` (sint64), `b`
(bool), `s` (string), `f` (float), `u` (uint64, varint). `d`/`i`/`b`/`s` map
1:1 to `ValueType.DOUBLE`/`INT64`/`BOOL`/`STRING`; `f` and `u` map to the
newer `ValueType.FLOAT` and `ValueType.UINT`. Exactly one arm is set per
`Sample`, selected by the `ValueType` of that sample's channel in the
`ChannelRegistry` it was encoded against — a decoder doesn't need to guess
which arm is populated, it looks up the channel's declared type first.

**Default stays `DOUBLE`.** Every channel the catalog doesn't explicitly
tune still round-trips through a plain 8-byte double, exactly as before this
change. `FLOAT` and `UINT` are opt-in, per-channel compact encodings —
"levers" a catalog author reaches for on channels where the extra headroom
of a double buys nothing:

- **`FLOAT`** (`Sample.f`) — single-precision, 4 bytes fixed width instead
  of double's 8. Good for real-valued channels whose physical precision
  never approaches single-float resolution (most analog sensor channels:
  temperatures, pressures, voltages).
- **`UINT`** (`Sample.u`) — an unsigned varint, as few as 1-2 bytes on the
  wire for small-magnitude values, instead of double's fixed 8. Good for
  naturally-integer channels (counters, RPM as a raw count) and, combined
  with the fixed-point convention below, for channels that are physically
  fractional but don't need double's dynamic range.

Producers choose the wire type per channel via the catalog's per-channel
`encode:` setting — see [`docs/CATALOG.md`](CATALOG.md) for the exact config
syntax; this document only describes the wire-level mechanics the catalog
lever controls: which `ValueType` a channel's `ChannelRegistry.Channel`
entry declares, and, for `UINT`, the accompanying `scale`/`offset`
coefficients.

### Fixed-point convention (`scale` / `offset`)

`Channel` carries two `double` fields, `scale` and `offset`, both defaulting
to `0`. A `scale` of `0` means "unscaled" — the wire value *is* the physical
value (used for plain `UINT`/`INT64` counters). A non-zero `scale` activates
a fixed-point convention that lets an integer wire type carry a fractional
physical quantity:

```
wire_value = round((physical - offset) / scale)
physical   = wire_value * scale + offset
```

`scale`/`offset` are only meaningful for integer wire types (`UINT`, and
`INT64` where a producer wants the same trick with signed deltas); they are
undefined for `DOUBLE`/`FLOAT`/`BOOL`/`STRING` channels and MUST be left at
`0` there. Decoders MUST apply a channel's `scale`/`offset` when it declares
them non-zero — a raw `wire_value` without that transform is not the
physical quantity.

**Worked example:** coolant temperature in Kelvin (roughly 250-400 K)
encoded with `scale: 0.1, offset: 0` becomes a wire value in the low
thousands (e.g. 300.0 K -> 3000) — comfortably inside a 1-2 byte varint —
instead of an 8-byte double, while still resolving to 0.1 K precision on
decode.

## format_version

`SampleBatch.format_version` declares the shape of that batch's payload.
Today it is always `1` — the per-sample repeated-submessage layout described
above (one `Sample` message per value, each carrying its own `channel_id`
and `t_offset_us`). A producer that doesn't set the field is implicitly
version 1 (proto3's zero-value default), so existing producers need no
change.

**Consumers MUST reject a batch whose `format_version` they don't
understand, loudly** — log/alert and drop the batch, not best-effort parse
it as if it were version 1. A version bump is a signal that the payload
shape itself changed (see below), not just that new fields were added to
the existing shape; silently misinterpreting an unknown version's bytes as
version 1 risks decoding garbage as valid samples.

## Future: columnar payload (format_version 2)

`SampleBatch` reserves field numbers `10` to `15` (see the proto) for a
future `format_version 2`: a **columnar** payload — per-channel packed
arrays of values and offsets — instead of today's `format_version 1`
per-sample repeated submessages. This is not implemented yet; the reservation
exists so the schema can grow into it later without a breaking field-number
reuse.

**Why it isn't worth doing today.** At the 10-20 ms batching ticks this
system actually runs (see "Batching rules" below), most channels contribute
at most one sample per batch — there is nothing to amortize a per-channel
array format against; the per-sample submessage framing (`Sample`'s
tag+length plus its own `channel_id`/`t_offset_us`) barely matters when
there's usually only one sample per channel per tick anyway.

**Why it would pay off at longer ticks.** The wire format is designed to
tolerate longer batching ticks under degraded-link conditions (100+ ms,
where a source-class's collector keeps sampling at its native rate but
publishing is throttled back). At that tick length, a single channel can
contribute many samples to one batch, and today's format pays the full
per-sample submessage tag+length plus a repeated `channel_id` for every one
of them. A columnar `format_version 2` — one packed array of values and one
packed array of offsets per channel, `channel_id` written once — removes
that repeated per-sample framing entirely. Estimated savings: **~40%**
further reduction on top of the `float32`/`UINT` lever, at the tick lengths
where it would actually be used (see the "levers" section of
[`docs/LINK_BUDGET.md`](LINK_BUDGET.md) for the reasoning this estimate
carries forward from).

## Timestamp scheme

Every sample's true capture time is:

```
capture_time_ms = SampleBatch.batch_epoch_unix_ms + Sample.t_offset_us / 1000.0
```

- **`batch_epoch_unix_ms`** is the wall-clock epoch (UTC, milliseconds) at
  the start of the batching tick. Wall-clock time is sourced from the
  GPS-disciplined clock when a GPS fix is available (the vehicle has no
  reliable RTC/NTP guarantee on its own), falling back to system time
  otherwise; consumers cannot distinguish which source was used from the
  wire format alone, so downstream latency/precision claims should be
  qualified by GPS fix status where it matters.
- **`t_offset_us`** is the offset, in microseconds, from `batch_epoch_unix_ms`
  to the instant that specific sample was actually captured by its
  collector — not when it was batched or published. This preserves true
  per-sample timing even though many samples from different source
  instants are bundled into one batch on a fixed tick.
- **`batch_epoch_mono_ns`** is a monotonic clock reading (nanoseconds,
  arbitrary per-process epoch) taken at the same instant as
  `batch_epoch_unix_ms`. It exists so a downstream consumer can measure
  cross-host latency using mono-to-mono deltas, which are immune to wall
  clock steps (NTP corrections, GPS fix acquired/lost) that would otherwise
  corrupt a latency measurement based on wall-clock timestamps alone. It is
  *not* comparable across hosts in absolute terms (monotonic clocks have no
  shared epoch) — only deltas of the same host's own readings are
  meaningful, e.g. "how much mono time elapsed between this batch's capture
  and the ingest-writer's receipt", using the ingest-writer's own
  synchronised sense of elapsed wall time as the cross-host bridge.

## Batching rules

- One `SampleBatch` is published per source-class per batching tick.
- Tick length is **10-20 ms**, configured per-agent (not per-channel); this
  is the live-feed granularity — there is no separate "live" vs. "batch"
  path, the 10-20 ms batches published to `TELE` *are* the live feed that
  `live-decoder` republishes to the pit's MQTT-Live bridge.
- **Empty ticks are not published.** If a source-class produced no samples
  during a tick (e.g. a slow sensor between updates, or a quiet bus), no
  `SampleBatch` is sent for that tick — there is no empty-batch heartbeat at
  the wire-format level. (Channel-level liveness is a catalog/RBE concern —
  see the agent design spec's `max_interval` heartbeat semantics — not a
  batching-tick concern.)
- A batch only ever contains samples captured within its own tick window;
  samples are never held back to a later tick to be combined with others,
  so `t_offset_us` values in a batch are bounded by the tick length and
  latency is bounded by one tick plus publish time.
- All samples in one batch share a `registry_seq`; if the catalog changes
  mid-tick (a config reload lands between two collector reads), the agent
  either finishes the in-flight tick against the old registry and starts
  the next tick fresh against the new one, or splits the tick — either way
  a single `SampleBatch` never mixes samples meant for two different
  registry generations.
