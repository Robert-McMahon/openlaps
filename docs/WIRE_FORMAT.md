# Wire format

The wire format defines how samples travel from the vehicle agent to the
pit's TimescaleDB and live dashboards. It consists of NATS JetStream
subjects carrying protobuf messages defined in
[`proto/telemetry.proto`](https://github.com/Robert-McMahon/openlaps/blob/main/proto/telemetry.proto).
This document describes that schema in prose. Read the two together.

## Subject hierarchy

All openlaps traffic uses a small set of subject prefixes. `<vehicle>`
is a short, stable identifier for the car (eg `example-club-racer`). It is
the same string as `ChannelRegistry.vehicle_id`.

| Subject pattern | Direction | Transport | Purpose |
| --- | --- | --- | --- |
| `tele.<vehicle>.catalog` | vehicle → pit | JetStream (`TELE`) | Full `ChannelRegistry` snapshots |
| `tele.<vehicle>.<source-class>` | vehicle → pit | JetStream (`TELE`) | `SampleBatch` telemetry from one source class |
| `cmd.<vehicle>.session` | pit → vehicle | JetStream (`CMD`) | Session/driver state, track selection |
| `rtcm.<vehicle>` | pit → vehicle | core NATS (no stream) | RTK correction bytes for the GPS receiver |

`<source-class>` is the name of the collector that produced the batch, for
example `can0`, `can1`, `serial0`, `host`, or `derived` for channels
computed by an in-agent app such as the timing engine (`lap.*`,
`timing.*`). The source class is used for routing only. It says which
collector published a batch. It does not say which channels are in it: one
`can0` batch can carry samples for `car.rpm`, `car.oil_pressure` and other
`car.*` channels, because the channel catalog names channels
independently of the source.

Registries are published only on `tele.<vehicle>.catalog` with
`Openlaps-Msg-Type: registry`. Batches use their source-class subject with
`Openlaps-Msg-Type: batch`. Consumers read the header to tell the two
apart, because a protobuf payload may parse under more than one message
type.

### `rtcm.<vehicle>` — core NATS only, by design

RTCM correction bytes are published on **core NATS**, without a JetStream
stream. This is deliberate. An old RTCM correction degrades the GPS
receiver's fix, so storing or redelivering corrections has no value. Core
NATS delivers each message at most once and keeps no backlog. If the link
is down when a correction is published, that correction is lost. By the
time the link returns, a newer correction has replaced it.

## JetStream stream definitions

The vehicle's `nats-server` holds two JetStream streams and the pit's
holds one. The pit's `nats-server` connects to the vehicle over a leafnode
and sources `TELE` into its own `TELE_VEHICLE`, so the pit's copy becomes
identical to the vehicle's stream. Sourcing resumes by sequence number:
after a link dropout the pit continues from the last sequence it holds, at
whatever bandwidth is available. Live data and backlog come from the same
stream.

### `TELE`

| Setting | Value | Rationale |
| --- | --- | --- |
| Subjects | `tele.<vehicle>.>` | Everything the vehicle agent produces: registries and batches from every source class. Live and historical data are the same stream |
| Storage | File | Must survive an agent restart. It is the vehicle's only durable copy until the pit has ingested it |
| Retention | Limits | An age cap (72 h by default) and a size cap (set from the vehicle's disk space). Whichever is reached first applies, and the oldest messages are dropped |
| Replicas | 1 (single vehicle node) | The vehicle has no high-availability requirement. The pit copy is the redundancy |
| `duplicate_window` | `2m` | Set explicitly (see "Deduplication" below) instead of using the server default |

72 hours covers a car left in the garage over a long weekend with no link
to the pit, with no operator action. The size cap limits disk use if the
data rate is unexpectedly high (eg a faulty collector publishing far above
its configured rate).

#### Deduplication

Every published `TickBatch` carries a `Nats-Msg-Id` header of
`<source-class>:<batch_epoch_unix_ms>:<sequence>`, where `<sequence>` is a
per-process batch counter (`src/agent/pipeline.py` builds the id,
`src/agent/publisher.py` sets the header on publish). JetStream uses
this header to deduplicate messages published again within the stream's
`duplicate_window`: if the same id is published twice inside that window,
the second publish is accepted but not stored again. `TELE` sets
`duplicate_window` explicitly to **2 minutes**, so this behaviour does not
change if the server default changes.

**What this provides.** Republication after a reconnect is idempotent. If
the agent's NATS connection drops and reconnects, or a publish is retried
because its acknowledgement was lost, the same batch arriving twice within
the 2-minute window is stored once, so a consumer sees it once.

**What it does not provide.** Anything beyond that window. A consumer that
crashes, is redeployed, or falls behind for longer than `duplicate_window`
will see redelivery, because JetStream consumers are at-least-once. This
header does not prevent that. A consumer that needs each batch to take
effect exactly once (the pit ingest-writer) cannot rely on `Nats-Msg-Id`
alone. It must keep its own idempotency key, a committed stream sequence
cursor, which does not depend on this window.

### `CMD`

| Setting | Value | Rationale |
| --- | --- | --- |
| Subjects | `cmd.<vehicle>.>` | Session/driver state, track selection, other pit → vehicle control messages |
| Storage | File | The volume is small, so durable storage has no significant cost |
| Retention | **Limits, with `max_msgs_per_subject = 1`** | Only the last value is kept: a new message on `cmd.<vehicle>.session` replaces the previous one. A vehicle agent (or timing engine) that starts or restarts during a session reads the one retained message on that subject to recover the current session state. It does not replay a command history. |

`CMD` messages are small and infrequent (driver swaps, track changes), so
volume is not a concern as it is for `TELE`. The requirement is that
exactly one current value exists per subject.

### `TELE_VEHICLE`

The pit's copy of `TELE`. It is created at the pit by
`deploy/provision_pit_streams.py`. The agent does not create it and has no
knowledge of it. Every pit service consumes this stream, and each is given
its name as deploy-time configuration
(`OPENLAPS_INGEST_STREAM`, `OPENLAPS_LIVE_STREAM`, `OPENLAPS_NTRIP_STREAM`).

| Setting | Value | Rationale |
| --- | --- | --- |
| Subjects | **none** | See below. A sourced stream that also declares `tele.<vehicle>.>` captures every message twice |
| Sources | `TELE`, `external.api = "$JS.veh.API"` | Cross-domain sourcing over the leafnode. `veh` is the vehicle server's JetStream domain |
| Storage | File | Survives a pit reboot without re-sourcing from the start |
| Retention | Limits, age ≥ the vehicle's 72 h and a size cap from pit disk | The stream is a buffer in front of Timescale. The database is the archive (ADR 0003). The age cap still matches the vehicle's because nats-server resumes a source from the newest sourced message it holds: an empty pit stream restarts from the vehicle's oldest message and re-ingests data already archived. That produces duplicate `samples` rows, because the writer's idempotency is its stream-sequence cursor and re-sourced messages get new sequences. The provisioner warns when the pit's cap is the shorter |
| Replicas | 1 | Single pit node |

**Why it declares no subjects.** The leafnode propagates
`tele.<vehicle>.>` to the pit server as ordinary core NATS. A pit stream
that declared that subject would capture each message directly and again
through sourcing. This was measured at exactly 2×: ten messages published
once to the vehicle grew such a stream by twenty. The sourced stream must
therefore declare no subjects. Sourcing preserves each message's subject,
so pit consumers still filter on `tele.<vehicle>.>`, against a stream they
name.

**Consumers must name the stream.** `nats-py` resolves a stream from a
subject by calling `$JS.API.STREAM.NAMES` with a subject filter, and the
server matches that filter against a stream's declared subjects. A
stream with no subjects matches nothing, so a bare
`js.subscribe("tele.<vehicle>.>")` raises `NotFoundError` against the pit
while the `stream=`-qualified form succeeds. This is a property of the
deployed topology and not of the wire format, but it is a common mistake
in a new pit consumer. Every existing consumer passes `stream=` from
configuration for this reason.

**Dropout recovery.** Sourcing resumes by sequence, so after a link is cut
and restored the pit catches up with no gap: it continues from the last
sequence it holds, at whatever bandwidth is available. This is verified
over a leafnode pair that was cut and restored, with 200 messages
published while disconnected: zero missing, zero duplicated, order
preserved (`tests/test_deploy_topology.py`). The same behaviour has not
yet been tested over a real half-duplex HaLow radio link.

## Registry lifecycle

`ChannelRegistry` is the only place where channel names, units, source
refs and types appear on the wire. Every `SampleBatch.Sample` refers to a
channel only by its integer `channel_id`. This keeps batches small (an
integer instead of a repeated string) and means the catalog is the one
place where channels change.

1. **Publish triggers.** The vehicle agent publishes a `ChannelRegistry` to
   `tele.<vehicle>.catalog` on startup and periodically after that. The
   catalog is loaded at startup; runtime catalog reload is not implemented.
   Periodic republication keeps the same `registry_seq`. A changed catalog on
   a later startup receives a new generation.
2. **Batches carry the seq they were built against.** Every `SampleBatch`
   has a `registry_seq` field. A consumer decoding a batch must already hold
   (or be able to fetch) the `ChannelRegistry` with exactly that seq.
   Decoding a batch against the wrong registry resolves channel ids to the
   wrong name, units or type, so the seq must match exactly.
3. **Unknown seq: look back in the stream.** `TELE` is a durable stream
   that can be replayed. A consumer that sees a `registry_seq` it does not
   recognise (a dashboard that joined late, a pit consumer that missed the
   startup publish during a dropout, or a consumer that has just started)
   resolves it by reading backwards through the stream on the relevant
   subject until it finds the `ChannelRegistry` message with that seq. In
   practice this means keeping a small durable consumer used only for
   registry recovery, or replaying once from the earliest retained
   message. Every registry publish is a full snapshot of the catalog and
   not a delta (see the `Channel` message docs in the proto), so finding
   any one `ChannelRegistry` with the right seq is sufficient. There is
   no need to replay and merge several registry messages.
4. **Registries are small.** A registry message has one entry per channel
   and is published rarely, so it is small relative to the sample stream.
   Keeping every registry generation for the whole retention window does
   not use significant space.

## Value encoding

`Sample.value` is a oneof with six arms: `d` (double), `i` (sint64), `b`
(bool), `s` (string), `f` (float), `u` (uint64, varint). `d`/`i`/`b`/`s` map
1:1 to `ValueType.DOUBLE`/`INT64`/`BOOL`/`STRING`; `f` and `u` map to
`ValueType.FLOAT` and `ValueType.UINT`. Exactly one arm is set per
`Sample`, selected by the `ValueType` of that sample's channel in the
`ChannelRegistry` it was encoded against. A decoder looks up the channel's
declared type first and then reads that arm.

**The default is `DOUBLE`.** Every channel that the catalog does not
configure otherwise is sent as an 8-byte double. `FLOAT` and `UINT` are
compact encodings that a catalog author can select per channel, for
channels that do not need the range or precision of a double:

- **`FLOAT`** (`Sample.f`): single precision, 4 bytes fixed width instead
  of double's 8. Suitable for real-valued channels whose physical precision
  is well below single-float resolution (most analog sensor channels:
  temperatures, pressures, voltages).
- **`UINT`** (`Sample.u`): an unsigned varint, as few as 1-2 bytes on the
  wire for small values, instead of double's fixed 8. Suitable for
  channels that are integers by nature (counters, RPM as a raw count) and,
  combined with the fixed-point convention below, for channels that are
  physically fractional but do not need double's dynamic range.

Producers choose the wire type per channel with the catalog's per-channel
`encode:` setting. See [`docs/CATALOG.md`](CATALOG.md) for the config
syntax. This document describes only what that setting controls on the
wire: which `ValueType` a channel's `ChannelRegistry.Channel` entry
declares, and, for `UINT`, the accompanying `scale`/`offset`
coefficients.

### Fixed-point convention (`scale` / `offset`)

`Channel` carries two `double` fields, `scale` and `offset`, both defaulting
to `0`. A `scale` of `0` means unscaled: the wire value is the physical
value (used for plain `UINT`/`INT64` counters). A non-zero `scale` enables
a fixed-point convention that lets an integer wire type carry a fractional
physical quantity:

```
wire_value = round((physical - offset) / scale)
physical   = wire_value * scale + offset
```

`scale`/`offset` apply only to integer wire types (`UINT`, and `INT64`
where a producer wants the same convention with signed values). They are
undefined for `DOUBLE`/`FLOAT`/`BOOL`/`STRING` channels and MUST be left at
`0` there. Decoders MUST apply a channel's `scale`/`offset` when it declares
them non-zero. A raw `wire_value` without that transform is not the
physical quantity.

**Worked example:** coolant temperature in Kelvin (roughly 250-400 K)
encoded with `scale: 0.1, offset: 0` becomes a wire value in the low
thousands (eg 300.0 K -> 3000). That fits in a 1-2 byte varint instead of
an 8-byte double, and still decodes to 0.1 K precision.

## format_version

`SampleBatch.format_version` declares the shape of that batch's payload.
It is currently always `1`: the layout described above, with one `Sample`
message per value, each carrying its own `channel_id` and `t_offset_us`.
A producer that does not set the field is version 1 (proto3's zero-value
default), so producers need not set it.

**Consumers MUST reject a batch whose `format_version` they do not
understand.** They must log or alert and drop the batch, and must not
parse it as if it were version 1. A new version means the shape of the
payload changed (see below), not only that fields were added to the
existing shape. Reading an unknown version's bytes as version 1 could
decode invalid data as valid samples.

## Future: columnar payload (format_version 2)

`SampleBatch` reserves field numbers `10` to `15` (see the proto) for a
future `format_version 2`: a **columnar** payload, with packed arrays of
values and offsets per channel, instead of version 1's one submessage per
sample. This is not implemented. The reservation allows the schema to add
it later without reusing a field number.

**Why it is not needed now.** At the 10-20 ms batching ticks this system
runs (see "Batching rules" below), most channels contribute at most one
sample per batch. A per-channel array saves nothing when it holds one
sample, and the per-sample framing (`Sample`'s tag and length plus its own
`channel_id`/`t_offset_us`) is a small cost at one sample per channel per
tick.

**Why it would help at longer ticks.** The wire format allows longer
batching ticks on a degraded link (100+ ms, where a collector keeps
sampling at its native rate but publishing is slowed). At that tick
length one channel can contribute many samples to one batch, and version 1
repeats the submessage tag, length and `channel_id` for each of them. A
columnar `format_version 2` writes `channel_id` once per channel, followed
by one packed array of values and one packed array of offsets, which
removes the repeated per-sample framing. Estimated saving: **~40%** on top
of the `float32`/`UINT` encodings, at the tick lengths where it would be
used ([`docs/LINK_BUDGET.md`](LINK_BUDGET.md), section 6, has the
reasoning behind this estimate).

## Timestamp scheme

Every sample's true capture time is:

```
capture_time_ms = SampleBatch.batch_epoch_unix_ms + Sample.t_offset_us / 1000.0
```

- **`batch_epoch_unix_ms`** is the wall-clock epoch (UTC, milliseconds) at
  the start of the batching tick. Wall-clock time is the vehicle's system
  clock, which chrony disciplines from GPS when a fix is available. The
  vehicle has no other guaranteed time source, so without a fix the clock
  may be wrong. A consumer cannot tell from the wire format whether the
  clock was disciplined, so statements about latency or precision should
  be qualified by GPS fix status where it matters.
- **`t_offset_us`** is the offset, in microseconds, from `batch_epoch_unix_ms`
  to the instant the sample was captured by its collector (not when it
  was batched or published). This preserves the capture time of each
  sample even though samples from different instants are sent together in
  one batch per tick.
- **`batch_epoch_mono_ns`** is a monotonic clock reading (nanoseconds,
  arbitrary per-process epoch) taken at the same instant as
  `batch_epoch_unix_ms`. It allows a consumer to measure latency from
  differences between monotonic readings, which are not affected by
  wall-clock steps (NTP corrections, a GPS fix acquired or lost). It is
  not comparable between hosts in absolute terms, because monotonic clocks
  have no shared epoch. Only differences between one host's own readings
  are meaningful. For example, the ingest-writer takes its own monotonic
  clock at receipt minus the batch's reading, and reports how much that
  difference has grown from the smallest value seen recently
  (`src/pit/ingest_writer/health.py`).

## Batching rules

- One `SampleBatch` is published per source class per batching tick.
- Tick length is **10-20 ms** (`OPENLAPS_TICK_MS`, default 20; the agent
  accepts 10 to 50). It is configured per agent, not per channel. These batches are the only data
  on the stream: `live-decoder` reads the same batches when its source is
  `pit` or `vehicle`. Live frames (see "Live link") are a separate UDP
  path that carries a subset of the same samples.
- **Empty ticks are not published.** If a source class produced no samples
  during a tick (eg a slow sensor between updates, or a quiet bus), no
  `SampleBatch` is sent for that tick. The wire format has no empty-batch
  heartbeat. Whether a channel is still alive is handled per channel by
  the catalog's RBE `max_interval` (see `AGENT_DESIGN.md`).
- A batch contains only samples captured within its own tick window.
  Samples are never held back to a later tick, so `t_offset_us` values in
  a batch are bounded by the tick length and latency is bounded by one
  tick plus publish time.
- All samples in one batch share a `registry_seq`. The catalog is loaded
  once at startup and is not reloaded while the agent runs, so one
  `SampleBatch` never mixes samples from two registry generations.

## Live link

The stream above is the record. It is ordered, acknowledged and has no
gaps, so it is late whenever the radio is slower than the data. Gauges
need current values more than complete ones, so they do not use the
stream. Beside the stream the car sends **live
frames**: UDP datagrams holding the newest value of each channel the pit's
live view selects, sent only when that value changes. Nothing is queued or
retried. A lost or late frame is replaced by the next one. Implementation:
`core/live_wire.py` (formats), `agent/live.py` (car), `pit/live_decoder/direct.py`
(pit, `OPENLAPS_LIVE_SOURCE=direct`).

Measured on the bench with a replayed race CAN capture (2026-11-22): 97
selected channels cost about 20 kbit/s, against roughly 430 kbit/s for the
stream, and with the car's link throttled to 100 kbit/s the gauges stayed a
median 80 ms old while the stream ran 7-19 s late.

**Who talks first.** The pit. About once a second the live-decoder sends a
`HELLO` to every address in `OPENLAPS_LIVE_CAR_ADDRS` (UDP port 7423 by
default). The car sends frames back to the address each valid HELLO came
from, for as long as HELLOs keep arriving (5 s timeout). As a result:

- the car needs no pit address and no channel selection of its own, and
  sends nothing to a network where no pit is listening;
- the link uses whichever path reaches the car. If both the tunnel address
  and the plain radio-network address are listed, the car answers on
  both, the pit drops the duplicates, and the loss of either path loses
  no frames. A plain local network with no tunnel uses one address;
- one pit is served at a time: a second listener is ignored until the first
  has been silent for the timeout.

**Datagram layout.** Network byte order. Every datagram starts with the
4-byte prefix `"OL"`, type (`1` HELLO, `2` FRAME), version (`1`).

| Datagram | After the prefix |
| --- | --- |
| HELLO | `session` u64 (random per live-decoder process), `counter` u32 (rises every cycle; an older one is refused), `registry_seq` u32, `selection_version` u32, `refresh_ms` u32, `part` u8, `parts` u8, then pairs of varints: `channel_id`, `interval_ms` |
| FRAME | `boot` u32 (random per agent process), `seq` u32 (rises per frame), then one serialized `SampleBatch` |

A selection too big for one datagram is split across `parts` HELLOs sharing
a `selection_version`. The car applies it once it holds them all. A HELLO
whose `registry_seq` is not the agent's own is ignored, because its channel
ids would refer to different channels.

**What a frame carries.** A `SampleBatch` exactly as on the stream
(`format_version` 1, the agent's `registry_seq`), so the pit decodes it with
the same code. Each sample keeps its capture time (`batch_epoch_unix_ms`
is the oldest sample in the frame). The agent builds frames every
`OPENLAPS_LIVE_TICK_MS` (100 ms, ie gauges update at 10 Hz). A selected
channel is included when

- its newest value differs from the one last sent **and** at least
  `interval_ms` has passed since (the live view's `max_hz`; 0 = every frame), or
- it has not been sent for `refresh_ms` (the live view's `resend_s`), which
  repeats an unchanged value. The repeat replaces a lost datagram and
  gives a value to a gauge that has just subscribed. A steady value is
  therefore sent once a second instead of ten times.

The agent samples batches after the catalog's report-by-exception filter,
so "differs" means a change the catalog already treats as significant
for that channel.

**Only what is watched.** The live view config lists the channels that
may be shown. The selection in the HELLO is the subset that a dashboard
is showing now. The pit's MQTT broker has that information, and the
live-decoder gets it in two ways (`pit/live_decoder/demand.py`):

- It publishes one message per channel every two seconds at QoS 1 over
  MQTT 5. The broker's acknowledgement says when a channel has no
  subscriber. After two such acknowledgements in a row the channel leaves
  the selection.
- The broker publishes every SUBSCRIBE it receives (`log_dest topic`,
  `log_type subscribe` in `deploy/mosquitto/mosquitto.conf`). This returns
  a channel to the selection as soon as a gauge opens, and a HELLO is sent
  immediately instead of on the next second.

With no dashboard open the selection is empty and the car sends only one
empty frame a second, so the pit can still distinguish a car with nothing
to send from a car it cannot reach. A live-decoder that has just started
or reconnected to the broker requests every channel and removes the
unwatched ones within seconds. If the broker does not publish its
SUBSCRIBE log, the live-decoder never removes channels.

**Ordering and duplicates.** The pit accepts a frame only if its `seq` is
higher than the last one taken from the same `boot`. A repeat (from the
second path) or a frame that arrives after a newer one is dropped. A new
`boot` means the agent restarted and resets the comparison.

**Signing.** With `OPENLAPS_LIVE_KEY` set on both ends (the same secret),
every datagram ends in a 16-byte keyed BLAKE2b tag over everything before
it, and a datagram with no tag or a wrong tag is dropped. This provides
authenticity only: it prevents another host on the network from
redirecting or faking the gauges. It does not hide the values. A tunnel
does, when one is in use. Without a key the link works unsigned and both
ends log a warning.

**Fallback.** In `direct` mode the live-decoder still reads the pit's
sourced copy. Registries arrive only that way. When the car has sent no
frame for `OPENLAPS_LIVE_FALLBACK_S` (30 s), the copy's batches supply the
gauges, as in `pit` mode, until frames return.

**Sharing the radio with the stream.** Sent as a separate flow, a live
frame waits in the radio's queue behind the stream data already there. A
stream that is catching up can put several seconds of data in that queue.
The link tunnel below prevents this. Without it, a live frame is delayed
by the length of the radio's queue.

**Preferred path and backups.** The first address in
`OPENLAPS_LIVE_CAR_ADDRS` is the preferred path (the tunnel, when one is
used). The others are asked only while the preferred one has delivered
nothing for 3 s, so the car does not send every frame once per path.

## Link tunnel

`openlaps-link` (`rust/link`) puts the leafnode stream and the live link
inside **one QUIC connection**, so that one sender decides what is sent
first. It is optional and runs at both ends. NATS and the live link need
no knowledge of it: each connects to a local port.

```
pit                                                        car
NATS leaf ---tcp 7422--> [link pit] ==== QUIC ==== [link car] --tcp--> NATS :7422
live-decoder -udp 7423->      "     UDP 7424 (datagrams)   "  --udp--> agent :7423
```

- **Live frames** (HELLOs one way, frames the other) cross as QUIC
  datagrams. They are unreliable, sent ahead of stream data, and dropped
  instead of queued when the link cannot take them (the send buffer holds
  about four frames).
- **The leafnode connection** crosses as a QUIC stream, byte for byte, with
  its own TLS intact. It uses the capacity that the live frames leave.
- **The queue stays short.** Sending live frames first is not enough if the
  stream has already filled the radio's queue, which any loss-based
  congestion control does. The tunnel uses a delay-based controller
  (`rust/link/src/pacing.rs`, the same approach as LEDBAT): the lowest
  recent round-trip time is taken as the empty-queue delay, and the stream
  is held to the rate that keeps the delay about `--target-delay-ms` (150)
  above it. After every 20 s of saturation it pauses briefly to measure
  the empty-queue delay again.
- **Who dials, and trust.** The pit dials UDP 7424 on every address in
  `OPENLAPS_LINK_CAR_ADDRS` at once and uses the first to answer. Both ends
  authenticate with the leafnode certificates (mutual TLS against the
  deployment CA; the car's certificate name is `OPENLAPS_LINK_SERVER_NAME`).
  It works over any IP path: a tunnel address, or a plain radio network.
- **No tunnel, no stream.** With the `link` profile on, the tunnel is the
  leafnode's only route to the car. While there is no QUIC connection (eg
  the car's service is stopped, or the path drops UDP) the pit end refuses
  the leafnode connection. NATS retries every second and is carried again
  within a second of the tunnel returning. Nothing is lost: the car keeps
  the stream and the pit catches up. HELLOs are dropped meanwhile and the
  live-decoder uses its backup address. There is deliberately no second
  route for the stream. A tunnel that does not work is a fault to be
  fixed.

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
  of its own, and the pit adds it to the URLs it reconnects over. By
  default that is the car's LAN address, which would route the leafnode
  outside the tunnel on every second reconnect.
- **`NATS_PING_MAX=30` on the pit.** A PONG travels in order behind the
  car's backlog, so at the default of 2 the pit closes a connection that
  is slow but working, and a leafnode cannot be re-established while a
  round trip exceeds a second. The tunnel detects a dead path itself (8 s)
  and closes the connection.

Deployment: car, `deploy/systemd/openlaps-link.service` (the binary is
installed by `tools/deploy_vehicle.sh`); pit, the `link` profile of
`deploy/pit-compose.yaml`, whose comment lists the settings.

**Still to do: drop the leafnode's own TLS.** Inside the tunnel the stream
is encrypted twice, once by QUIC and once by the leafnode, and both ends
are authenticated twice with the same certificates. The inner layer costs
about 22 bytes per NATS write and a second handshake on every reconnect,
and the link has little capacity for either. It stays until the tunnel
has been proven on a real radio, because removing it makes the tunnel
mandatory: the car's leafnode listener must then bind to loopback only
(`listen: 127.0.0.1:7422`, no `tls` block, and none on the pit's remote),
so a pit without the tunnel can no longer connect at all.

## `lap.event` payload schema

The timing engine (`src/agent/timing_app.py`, which runs inside the agent,
see `docs/AGENT_DESIGN.md`) emits timing events on an ordinary
`STRING`-typed channel, `lap.event`. The event enters the pipeline like
any other derived sample: it is published on `tele.<vehicle>.derived`
inside a normal `SampleBatch`, with `Sample.value` holding the JSON payload
below in its `s` arm. There is **no** separate subject or message type.
The pit ingest-writer turns these events into `laps` / `lap_sectors` rows
using the same decode path as every other sample.

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

The payload also carries these keys whenever the active session
(`src/agent/session.py`, taken from `cmd.<vehicle>.session`, see below)
has them: `session_id`, `driver`, `stint_number`, `session_type`,
`track_name`. Each is added only when it is not `None` in the current
session state, so a lap recorded with no session open carries none of
them.

**`lap_number` does not identify a lap.** It is held in the timing
engine's memory (`src/timing/timing_core.py`: set to 1 on the first line
crossing, incremented per lap), is never persisted, and is not reset by a
session change. It is reset whenever the engine is rebuilt, which
`src/agent/timing_app.py` does on a track switch and which also happens on
an agent restart. Lap numbers therefore repeat within one session across a
restart. A consumer storing laps must use a different key (the pit
schema uses the crossing instant, see `docs/PIT_SCHEMA.md`).

**Consumers must tolerate unknown keys.** This schema is expected to gain
keys (new session keys, new event metadata) without a change of
wire-format version, because it travels inside a versioned
`SampleBatch.Sample` and is not a typed message of its own.

## `cmd.<vehicle>.session` payload schema

Published by session-control to `cmd.<vehicle>.session` on `CMD`
(`max_msgs_per_subject = 1`, so only the latest state is retained, see
"CMD" above). The vehicle agent (`src/agent/session.py`) does not
interpret this payload. It copies five of these keys onto derived channels
(`session_id`, `driver`, `stint_number`, `session_type`, `track_name`) and
ignores the rest. The payload is a JSON object with these keys:

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

**Consumers must preserve unknown keys**, so that session-control can add
keys later. On a payload that is not valid JSON or lacks the required
structure, a consumer must **keep the previous session state** and not
clear it, so that a malformed publish does not remove the session from a
lap in progress. `src/agent/session.py` does this: it logs and discards
the bad payload and keeps the last good state.
