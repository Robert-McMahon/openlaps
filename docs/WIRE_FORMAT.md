# Wire format

The wire format is how the vehicle agent gets samples off the car and onto
the pit's TimescaleDB and live dashboards: NATS JetStream subjects carrying
protobuf messages defined in
[`proto/telemetry.proto`](https://github.com/Robert-McMahon/openlaps/blob/main/proto/telemetry.proto).
This document is the prose companion to that schema — read them together.

## Subject hierarchy

All openlaps traffic lives under a small set of subject prefixes. `<vehicle>`
is a short, stable identifier for the car (e.g. `example-club-racer`); it is
the same string as `ChannelRegistry.vehicle_id`.

| Subject pattern | Direction | Transport | Purpose |
| --- | --- | --- | --- |
| `tele.<vehicle>.catalog` | vehicle → pit | JetStream (`TELE`) | Full `ChannelRegistry` snapshots |
| `tele.<vehicle>.<source-class>` | vehicle → pit | JetStream (`TELE`) | `SampleBatch` telemetry from one source class |
| `cmd.<vehicle>.session` | pit → vehicle | JetStream (`CMD`) | Session/driver state, track selection |
| `rtcm.<vehicle>` | pit → vehicle | core NATS (no stream) | RTK correction bytes for the GPS receiver |

`<source-class>` is the name of the collector that produced the batch — for
example `can0`, `can1`, `serial0`, `host`, or `derived` for channels
synthesised by an in-agent app such as the timing engine (`lap.*`,
`timing.*`). Source-class is a routing concern only: it says which collector
published a batch, not which canonical channels are inside it — a single
`can0` batch can carry samples for `car.rpm`, `car.oil_pressure` and other `car.*`
channels side by side, because the channel catalog assigns domain meaning
independently of the source.

Registries are published only on `tele.<vehicle>.catalog` with
`Openlaps-Msg-Type: registry`. Batches use their source-class subject with
`Openlaps-Msg-Type: batch`. Consumers use the header and never guess from a
protobuf payload that may parse under more than one message type.

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

Two JetStream streams live on the vehicle's `nats-server`, and one at the
pit. The pit's `nats-server` connects to the vehicle over a leafnode and
sources `TELE` into its own `TELE_VEHICLE`, so the vehicle-side stream and
the pit-side copy are (eventually) identical — JetStream's resumable
sourcing is what makes link dropouts a non-event: the pit catches up from
the last sequence number it has, on whatever bandwidth is available, live
or backlogged, off the same stream.

### `TELE`

| Setting | Value | Rationale |
| --- | --- | --- |
| Subjects | `tele.<vehicle>.>` | Everything the vehicle agent produces — registries and batches, every source-class, live and historical are the same stream |
| Storage | File | Must survive an agent restart; this is the vehicle's only durability until the pit has ingested it |
| Retention | Limits | Age cap ~72 h *and* a size cap (sized to the vehicle's disk budget), whichever is hit first, oldest messages dropped |
| Replicas | 1 (single vehicle node) | No HA requirement on the vehicle; the pit copy is the redundancy |
| `duplicate_window` | `2m` | Set explicitly (see "Deduplication" below) rather than left on the server default |

72 hours comfortably covers "car sits in the garage over a long weekend with
no pit wifi" without needing operator intervention; the size cap is the
backstop for unexpectedly high data rates (e.g. a misbehaving collector
publishing far above its configured rate).

#### Deduplication

Every published `TickBatch` carries a `Nats-Msg-Id` header of
`<source-class>:<batch_epoch_unix_ms>` (`src/agent/pipeline.py` builds the
id, `src/agent/publisher.py` sets the header on publish). JetStream uses
this header to deduplicate messages published again within the stream's
`duplicate_window`: if the same `source-class:epoch_ms` pair is published
twice inside that window, the second publish is accepted but not stored
again. `TELE` sets `duplicate_window` explicitly to **2 minutes** rather
than relying on the server default, so this behaviour doesn't silently
shift if the default changes.

**What this buys, precisely:** it makes *reconnect-scale* republication
idempotent — if the agent's NATS connection drops and reconnects, or a
publish is retried because the ack was lost in flight, the same batch
arriving twice within the 2-minute window is deduplicated at the stream
level, so a consumer only ever sees it once. **What it does not buy:**
anything beyond that window. A consumer that crashes, is redeployed, or
falls behind for longer than `duplicate_window` will see redelivery (from
JetStream's normal at-least-once consumer semantics) that this header does
nothing to prevent. Consumers that need exact-once effects at the row/write
level — the pit ingest-writer (P3.2) — cannot rely on `Nats-Msg-Id` dedupe
alone and must carry their own idempotency key (a committed stream
sequence cursor) independent of this window.

### `CMD`

| Setting | Value | Rationale |
| --- | --- | --- |
| Subjects | `cmd.<vehicle>.>` | Session/driver state, track selection, other pit → vehicle control messages |
| Storage | File | Small volume; durability costs nothing |
| Retention | **Limits, with `max_msgs_per_subject = 1`** | Last-value semantics: a new message on `cmd.<vehicle>.session` supersedes the previous one. A vehicle agent (or timing engine) that (re)starts mid-session reads the single retained message on that subject to recover current session state, instead of replaying a command history — this replaces the MQTT `retained` flag from the old stack. |

`CMD` messages are small and infrequent (driver swaps, track changes), so
the volume argument for retention doesn't apply the way it does for `TELE`;
what matters is that exactly one current value exists per subject.

### `TELE_VEHICLE`

The pit's copy of `TELE`, created at the pit by
`deploy/provision_pit_streams.py` — not by the agent, which knows nothing
about it. It is the stream every pit service actually consumes, and its
name is deploy-time configuration on each of them
(`OPENLAPS_INGEST_STREAM`, `OPENLAPS_LIVE_STREAM`, `OPENLAPS_NTRIP_STREAM`).

| Setting | Value | Rationale |
| --- | --- | --- |
| Subjects | **none** | See below — a sourced stream that also declares `tele.<vehicle>.>` captures every message twice |
| Sources | `TELE`, `external.api = "$JS.veh.API"` | Cross-domain sourcing over the leafnode; `veh` is the vehicle server's JetStream domain |
| Storage | File | Survives a pit reboot without re-sourcing from the start |
| Retention | Limits, age ≥ the vehicle's 72 h and a size cap from pit disk | A buffer in front of Timescale, not an archive — the database is the archive (ADR 0003). The age cap still matches the vehicle's because nats-server resumes a source from the newest sourced message it holds: an empty pit stream restarts from the vehicle's oldest message and re-ingests data already archived (duplicate `samples` rows — the writer's idempotency is its stream-sequence cursor, and re-sourced messages get new sequences). The provisioner warns when the pit's cap is the shorter |
| Replicas | 1 | Single pit node |

**Why it declares no subjects.** The leafnode propagates
`tele.<vehicle>.>` to the pit server as ordinary core NATS. A pit stream
that declared that subject would capture each message directly *and* again
through sourcing: measured at exactly 2× — ten messages published once to
the vehicle grew such a stream by twenty. The sourced stream must therefore
stay subject-less. Subjects are preserved *through* sourcing, so pit
consumers still filter on `tele.<vehicle>.>`; they simply do it against a
stream they name.

**Consumers must name the stream.** `nats-py` resolves a stream from a
subject by calling `$JS.API.STREAM.NAMES` with a subject filter, and the
server matches that filter against a stream's *declared* subjects. A
subject-less stream matches nothing, so a bare
`js.subscribe("tele.<vehicle>.>")` raises `NotFoundError` against the pit
while the `stream=`-qualified form succeeds. This is a property of the
deployed topology, not of the wire format, but it is the first thing a new
pit consumer gets wrong — every existing one passes `stream=` from
configuration for exactly this reason.

**Dropout recovery.** Sourcing resumes by sequence, so a severed and
restored link is a catch-up, not a gap: the pit continues from the last
sequence it holds, at whatever bandwidth is available. Verified over a
severed-and-restored leafnode pair with 200 messages published while
disconnected — zero missing, zero duplicated, order preserved
(`tests/test_deploy_topology.py`). What remains unproven is the same
behaviour over real half-duplex HaLow RF, which is Phase 4's garage bench
test.

## Registry lifecycle

`ChannelRegistry` is the only place channel names, units, source refs and
types ever appear on the wire; every `SampleBatch.Sample` refers to a
channel purely by its integer `channel_id`. This keeps batches small (an
integer instead of a repeated string) and gives the catalog a single,
auditable place to change.

1. **Publish triggers.** The vehicle agent publishes a `ChannelRegistry` to
   `tele.<vehicle>.catalog` on startup and periodically after that. The
   catalog is loaded at startup; runtime catalog reload is not implemented.
   Periodic republication keeps the same `registry_seq`. A changed catalog on
   a later startup receives a new generation.
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

## Live link

The stream above is the record: ordered, acknowledged, gap-free, and for
those reasons exactly as late as the radio is slow. Gauges want the opposite
trade, so they do not ride it. Beside the stream the car sends **live
frames**: UDP datagrams holding the newest value of each channel the pit's
live view selects, sent only when that value changes. Nothing is queued or
retried; a lost or late frame is superseded by the next one. Implementation:
`core/live_wire.py` (formats), `agent/live.py` (car), `pit/live_decoder/direct.py`
(pit, `OPENLAPS_LIVE_SOURCE=direct`).

Measured on the bench with a replayed race CAN capture (2026-11-22): 97
selected channels cost about 20 kbit/s, against roughly 430 kbit/s for the
stream, and with the car's link throttled to 100 kbit/s the gauges stayed a
median 80 ms old while the stream ran 7-19 s late.

**Who talks first.** The pit. About once a second the live-decoder sends a
`HELLO` to every address in `OPENLAPS_LIVE_CAR_ADDRS` (UDP port 7423 by
default). The car streams frames back to the address each valid HELLO came
from, for as long as HELLOs keep arriving (5 s timeout), so:

- the car needs no pit address and no channel selection of its own, and
  sends nothing to a network where no pit is listening;
- the link follows whatever path reaches the car. List the tunnel address
  and the plain radio-network address and the car answers on both; the pit
  drops the duplicates, and losing either path costs nothing. A plain local
  network with no tunnel at all is just the one-address case;
- one pit is served at a time: a second listener is ignored until the first
  has been silent for the timeout.

**Datagram layout.** Network byte order. Every datagram starts with the
4-byte prefix `"OL"`, type (`1` HELLO, `2` FRAME), version (`1`).

| Datagram | After the prefix |
| --- | --- |
| HELLO | `session` u64 (random per live-decoder process), `counter` u32 (rises every cycle; an older one is refused), `registry_seq` u32, `selection_version` u32, `refresh_ms` u32, `part` u8, `parts` u8, then pairs of varints: `channel_id`, `interval_ms` |
| FRAME | `boot` u32 (random per agent process), `seq` u32 (rises per frame), then one serialized `SampleBatch` |

A selection too big for one datagram is split across `parts` HELLOs sharing
a `selection_version`; the car applies it once it holds them all. A HELLO
whose `registry_seq` is not the agent's own is ignored, because its channel
ids would mean something else.

**What a frame carries.** A `SampleBatch` exactly as on the stream
(`format_version` 1, the agent's `registry_seq`), so the pit decodes it with
the same code. Each sample keeps its true capture time (`batch_epoch_unix_ms`
is the oldest sample in the frame). The agent builds frames every
`OPENLAPS_LIVE_TICK_MS` (100 ms, i.e. 10 Hz gauges). A selected channel is
included when

- its newest value differs from the one last sent **and** at least
  `interval_ms` has passed since (the live view's `max_hz`; 0 = every frame), or
- it has not been sent for `refresh_ms` (the live view's `resend_s`), which
  repeats an unchanged value. This is what heals a lost datagram and fills a
  gauge that has only just subscribed. A steady value therefore crosses once
  a second, not ten times.

The agent samples batches *after* the catalog's report-by-exception filter,
so "differs" means what the catalog already says is a significant change
for that channel.

**Only what is watched.** The live view config says which channels *may*
be shown; the selection in the HELLO is the part of it somebody is looking
at. The pit's MQTT broker knows that, and the live-decoder learns it two
ways (`pit/live_decoder/demand.py`): one publish per channel every two
seconds goes out at QoS 1 over MQTT 5, and the broker's acknowledgement
says when a channel has no subscriber (twice in a row and it leaves the
selection); and the broker publishes every SUBSCRIBE it receives
(`log_dest topic`, `log_type subscribe` in `deploy/mosquitto/mosquitto.conf`),
which puts a channel back the moment a gauge opens, with a HELLO sent at
once rather than on the next second. With no dashboard open the selection is
empty and the car sends only a one-per-second empty frame, so the pit can
still tell a quiet car from a lost one. A live-decoder that has just started
or reconnected to the broker asks for everything and prunes again within
seconds; a broker that does not publish its SUBSCRIBE log is never pruned
against at all.

**Ordering and duplicates.** The pit accepts a frame only if its `seq` is
higher than the last one taken from the same `boot`; a repeat (the second
path) or an overtaken frame is dropped. A new `boot` means the agent
restarted and resets the comparison.

**Signing.** With `OPENLAPS_LIVE_KEY` set on both ends (the same secret),
every datagram ends in a 16-byte keyed BLAKE2b tag over everything before
it, and anything untagged or mistagged is dropped. This is authenticity
only: it stops a stranger on the network redirecting or faking the gauges.
It does not hide the values; a tunnel does, when one is in use. Without a
key the link works unsigned and both ends log a warning.

**Fallback.** In `direct` mode the live-decoder still reads the pit's
sourced copy: registries only arrive that way, and when the car has sent no
frame for `OPENLAPS_LIVE_FALLBACK_S` (30 s) the copy's batches drive the
gauges instead, as in `pit` mode, until frames return.

**Sharing the radio with the stream.** Sent as a flow of its own, a live
frame queues at the radio behind whatever the stream has already put there,
and a stream that is catching up puts seconds there. The link tunnel below
exists to prevent that; without it the live link is only as fresh as the
radio's queue is short.

**Preferred path and backups.** The first address in
`OPENLAPS_LIVE_CAR_ADDRS` is the preferred path (the tunnel, when one is
used). The others are asked only while the preferred one has delivered
nothing for 3 s, so the car does not send every frame once per path.

## Link tunnel

`openlaps-link` (`rust/link`) puts the leafnode stream and the live link
inside **one QUIC connection**, so that one sender decides what goes first.
It is optional, runs at both ends, and neither NATS nor the live link knows
it is there: each talks to a local port.

```
pit                                                        car
NATS leaf ---tcp 7422--> [link pit] ==== QUIC ==== [link car] --tcp--> NATS :7422
live-decoder -udp 7423->      "     UDP 7424 (datagrams)   "  --udp--> agent :7423
```

- **Live frames** (HELLOs one way, frames the other) cross as QUIC
  datagrams: unreliable, sent ahead of stream data, and dropped rather than
  queued when the link cannot take them (the send buffer holds about four
  frames).
- **The leafnode connection** crosses as a QUIC stream, byte for byte, with
  its own TLS intact. It takes whatever capacity the live frames leave.
- **The queue stays short.** Sending live frames first is not enough if the
  stream has already filled the radio's queue, which any loss-based
  congestion control does. The tunnel uses a delay-based controller
  (`rust/link/src/pacing.rs`, the LEDBAT idea): the lowest recent round-trip
  time is taken as the empty-queue delay, and the stream is held to the rate
  that keeps the delay about `--target-delay-ms` (150) above it. Every 20 s
  of saturation it pauses for a moment to see the empty queue again.
- **Who dials, and trust.** The pit dials UDP 7424 on every address in
  `OPENLAPS_LINK_CAR_ADDRS` at once and uses the first to answer. Both ends
  authenticate with the leafnode certificates (mutual TLS against the
  deployment CA; the car's certificate name is `OPENLAPS_LINK_SERVER_NAME`).
  It works over any IP path: a tunnel address, or a plain radio network.
- **No tunnel, no harm.** While there is no QUIC connection (a car without
  the service, a path that drops UDP), the pit end passes the leafnode
  connection straight to `OPENLAPS_LINK_DIRECT_ADDRS`, and moves it into
  the tunnel 5 s after one comes up. HELLOs are dropped meanwhile and the
  live-decoder turns to its backup address.

Measured on the bench (2026-11-22), replayed race CAN load, all 97 live
channels subscribed, the tunnel's single connection throttled at the car:

| Link | Gauges (median age) | Stream |
| --- | --- | --- |
| unthrottled | 0.1 s | current |
| 250 kbit/s | 0.3 s | about a third of its rate, falling behind |
| 100 kbit/s | 0.45 s, rpm at 8 Hz | about a tenth of its rate |
| 30 kbit/s | 1.1 to 1.4 s, at a reduced rate | a trickle |

The leafnode stayed connected throughout, and after the throttle was
released the gauges were current within 15 s and the stream within 35 s.
For comparison, without the tunnel a small datagram flow sharing a
100 kbit/s link with a saturating TCP flow arrived 1.5 to 8 s late and half
of it was lost.

Two NATS settings belong with the tunnel:

- **The car announces `127.0.0.1:7422`** (`advertise` in
  `deploy/nats/vehicle.conf`). A leafnode server tells the pit one address
  of its own, and the pit adds it to the URLs it reconnects over; left
  alone that is the car's LAN address, which takes the leafnode around the
  tunnel on every other reconnect.
- **`NATS_PING_MAX=30` on the pit.** A PONG travels in order behind the
  car's backlog, so at the default of 2 the pit hangs up on a link that is
  slow but working, and a leafnode cannot be re-established while a round
  trip exceeds a second. The tunnel detects a dead path itself (8 s) and
  closes the connection.

Deployment: car, `deploy/systemd/openlaps-link.service` (the binary is
installed by `tools/deploy_vehicle.sh`); pit, the `link` profile of
`deploy/pit-compose.yaml`, whose comment lists the five settings.

## `lap.event` payload schema

The timing engine (`src/agent/timing_app.py`, hosted in-agent per
`docs/AGENT_DESIGN.md`) emits timing events as an ordinary `STRING`-typed
channel, `lap.event`, re-entering the pipeline like any other derived
sample — it is published on `tele.<vehicle>.derived` as part of a normal
`SampleBatch`, `Sample.value` holding the JSON payload below as its `s`
arm. This is **not** a separate subject or message shape; it exists so the
pit ingest-writer (P3.2) can materialise it into relational `laps` /
`lap_sectors` rows without inventing a second decode path.

The payload is a compact JSON object (`json.dumps(..., sort_keys=True,
separators=(",", ":"))`) with these keys:

| Key | Type | Meaning |
| --- | --- | --- |
| `type` | string | One of `timing.timing_core.EventType`'s values: `sector_completed`, `lap_completed`, `pit_entry`, `pit_exit` |
| `time` | number | Interpolated crossing time, epoch seconds |
| `line` | string | Timing line name that produced the event |
| `lap_number` | int | Lap number the event pertains to |
| `sector` | int | Sector number the event pertains to (0 when not sector-specific) |
| `split_time` | number | Completed sector split, seconds (0.0 unless `type` is `sector_completed`) |
| `lap_time` | number | Completed lap time, seconds (0.0 unless `type` is `lap_completed`) |
| `valid` | bool | Whether the lap/sector is eligible for best-time comparison |
| `direction` | string | Crossing direction |
| `pit_status` | string | `"track"` or `"pit"` at the time of the event |
| `lat` | number | Interpolated latitude at the crossing |
| `lon` | number | Interpolated longitude at the crossing |

Plus, whenever the active session (`src/agent/session.py`, stamped from
`cmd.<vehicle>.session` — see below) has them: `session_id`, `driver`,
`stint_number`, `session_type`, `track_name`. These five are added only
when non-`None` in the current session state, so a lap recorded with no
session open carries none of them.

**`lap_number` is not a key.** It lives in the timing engine's in-memory
state (`src/timing/timing_core.py` — set to 1 on the first line crossing,
incremented per lap), is never persisted, and is not reset by a session
change. It *is* reset whenever the engine is rebuilt, which
`src/agent/timing_app.py` does on a track switch, and which an agent restart
does implicitly. So lap numbers repeat within a single session across a
restart; a consumer storing laps must key them on something else (the pit
schema uses the crossing instant — see `docs/PIT_SCHEMA.md`).

**Consumers must tolerate unknown keys** — this schema is expected to grow
(new session-stamp keys, new event metadata) without a wire-format version
bump, since it travels inside an already-versioned `SampleBatch.Sample`
rather than being its own typed message.

## `cmd.<vehicle>.session` payload schema

Published by session-control (P3.4) to `cmd.<vehicle>.session` on `CMD`
(`max_msgs_per_subject = 1`, so only the latest state is ever retained —
see "CMD" above). The vehicle agent (`src/agent/session.py`) consumes this
payload **opaquely**: it stamps five of these keys onto derived channels
(`session_id`, `driver`, `stint_number`, `session_type`, `track_name`) and
otherwise does not interpret the message. The payload is a JSON object
with these keys:

| Key | Type | Meaning |
| --- | --- | --- |
| `session_id` | string | Stable identifier for the current session |
| `session_type` | string | One of `practice`, `qualifying`, `race`, `test` |
| `driver` | string | Current driver identifier |
| `track_name` | string | Track the session is running at |
| `car` | string | Car/vehicle identifier for the session |
| `stint_number` | int | Current stint number within the session |
| `session_start` | number | Session start time, unix ms |
| `stint_start` | number | Current stint start time, unix ms |
| `status` | string | One of `none`, `active`, `ended` |
| `timestamp` | number | Time this payload was produced, unix ms |

**Consumers must preserve unknown keys** they don't understand (forward
compatibility for session-control additions) and, on a payload that fails
to parse as valid JSON or is missing required structure, **leave prior
session state in force** rather than clearing it — a malformed publish
must not un-stamp an in-progress session (`src/agent/session.py` already
implements this: it logs and discards the bad payload, keeping the last
good state).
