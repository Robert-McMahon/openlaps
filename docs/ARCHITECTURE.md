# openlaps architecture

openlaps is a racing telemetry platform split across two hosts: a **vehicle**
node that collects, times, and durably stores telemetry, and a **pit** node
that receives the stream over a constrained wireless link, lands it in a
time-series database, and serves live and historical dashboards. Everything
specific to one car — buses, DBC files, sensors, channel names, tracks — lives
in a configuration *profile* (see `CATALOG.md`); the platform code is generic.

Design goals, in priority order:

1. **Survive a bad link.** The vehicle↔pit radio (e.g. 802.11ah HaLow) is
   half-duplex, long-range, and sometimes awful. Data must never be lost to a
   dropout; the pit view must heal itself when the link returns.
2. **The car is config.** Adding a CAN bus, moving GPS from a serial receiver
   onto a bus, or renaming a sensor is a profile edit — no consumer (dashboard,
   timing, export) changes.
3. **Bandwidth is a budget, not a hope.** The wire format is binary, batched,
   and carries no repeated name strings; per-channel report-by-exception
   trims what crosses the link.
4. **Open source hygiene.** No credentials in the repo, ever; deployments
   inject secrets via environment. The example profile is documentation.

## System overview

```mermaid
flowchart LR
  subgraph vehicle [Vehicle node]
    CAN0[can collectors 0..n<br/>socketCAN + DBC decode] --> AGENT
    SER0[serial collectors 0..n<br/>decoder: nmea, driver: um980] --> AGENT
    HOST[host collector<br/>cpu, temps, disk] --> AGENT
    AGENT[vehicle agent<br/>catalog map / RBE / batcher / timing] --> VNATS[(vehicle nats-server<br/>JetStream: TELE, CMD)]
    VNATS -- "rtcm.vehicle, core NATS" --> SER0
  end
  VNATS <-- "leafnode over radio, TLS<br/>TELE sourced to pit, CMD back" --> PNATS[(pit nats-server<br/>JetStream)]
  subgraph pit [Pit node]
    PNATS --> WRITER[ingest-writer] --> TSDB[(TimescaleDB)]
    PNATS --> LIVE[live-decoder] --> MQ[mosquitto, websockets] --> GLIVE[Grafana live panels]
    TSDB --> GSQL[Grafana SQL dashboards]
    SC[session-control] --> PNATS
    NTRIP[ntrip-client] --> PNATS
  end
```

One durable stream (`TELE`) on the vehicle is the system's source of truth.
The pit's copy of `TELE` is a JetStream *sourced stream*: it pulls from the
vehicle's stream by sequence number over a NATS leafnode connection, so after
any interruption — a corner of the track with no RF, a dead radio, an
overnight power-down — it resumes exactly where it left off. There is no
separate synchronisation service and no diff-based backfill: catch-up *is*
the transport.

## Layered data model

| Layer | Owns | Knows about |
| --- | --- | --- |
| **Collectors** | Transport + decode: socketCAN frames → DBC signals, serial bytes → NMEA fields, host stats | Nothing about racing. A collector emits `Sample{source_ref, t, value}` where `source_ref` is `bus:device.MESSAGE.SIGNAL` |
| **Channel catalog** | Meaning: maps source refs to canonical channels (`engine.rpm`, `position.lat`, `chassis.accel_x`) with units, types, and link policies | The profile YAML. Integer channel IDs come from here (via the `ChannelRegistry`) |
| **Apps & consumers** | Lap timing, dashboards, exports | Canonical channel names only — never a bus, DBC, or wire detail |

This is the load-bearing indirection: the timing engine subscribes to
`position.*` and does not care whether fixes come from a serial GNSS receiver
or a CAN frame. Swapping hardware is a catalog edit (`CATALOG.md` has the
worked example).

Derived data re-enters the same pipeline: the timing engine's outputs (lap and
sector events, delta-vs-best, predicted lap, distance) are published as
`lap.*` / `timing.*` channels and flow to the pit like any sensor channel.

## Vehicle agent data flow

```mermaid
flowchart TB
  C1[can0..n threads] --> Q[bounded sample queues<br/>drop-oldest + drop counters]
  C2[serial0..n threads] --> Q
  C3[host thread] --> Q
  Q --> MAP[catalog mapper<br/>source_ref to channel_id]
  MAP --> TAP[timing engine tap<br/>pre-RBE position channels]
  TAP --> DERIVED[derived channels re-enter<br/>lap.* timing.*]
  DERIVED --> MAP
  MAP --> RBE[RBE / rate-cap filter<br/>deadband, min and max interval]
  RBE --> BATCH[batcher, 10 to 20 ms tick<br/>protobuf SampleBatch]
  BATCH --> JS[JetStream publish<br/>tele.vehicle.source-class]
```

Behaviours that matter (full spec in `AGENT_DESIGN.md`):

- **Timestamps** — each `SampleBatch` carries a wall-clock and a monotonic
  epoch; samples carry microsecond offsets. True capture time survives
  batching, and cross-host latency is measurable.
- **RBE is a link policy, not a data policy** — the timing engine taps the
  stream *before* report-by-exception filtering, so timing always sees
  full-rate position data. Every RBE channel has a `max_interval` heartbeat
  so downstream fill-forward is bounded.
- **Backpressure by shedding, visibly** — every queue is bounded and
  drop-oldest; drop counters are themselves channels (`sys.agent.*`), so
  overload shows up on a dashboard instead of in a log nobody reads.
- **Local durability** — the agent publishes to a NATS server on the same
  host. The only loss window is a hard crash between capture and local ack.

## Wire format

Defined in `WIRE_FORMAT.md` + `proto/telemetry.proto`. In one paragraph:
channels are registered once in a `ChannelRegistry` message (id → name, units,
type, source ref); data rides in `SampleBatch` messages — one per 10–20 ms
tick per source class — containing `{channel_id, t_offset, value}` triples.
No topic-per-signal, no repeated JSON keys, no name strings on the hot path.
Against the predecessor system's per-signal JSON-over-MQTT this is roughly an
order of magnitude less wire traffic at identical data rates, which is what
lets full-rate telemetry (including 100 Hz IMU) fit a 2 MHz HaLow channel
with headroom for video.

## Pit data flow

```mermaid
flowchart LR
  SRC[(pit TELE<br/>sourced stream)] --> W[ingest-writer<br/>durable consumer]
  W -- "batched COPY" --> DB[(TimescaleDB<br/>samples hypertable +<br/>sessions, stints, laps)]
  SRC --> L[live-decoder<br/>registry-aware]
  L -- "selected channels as JSON" --> M[mosquitto ws] --> G1[Grafana live gauges]
  DB --> G2[Grafana SQL dashboards]
  NC[ntrip-client] -- "rtcm.vehicle, core NATS" --> LEAF[leafnode to vehicle]
  S[session-control] -- "cmd.vehicle.session" --> LEAF
```

- **ingest-writer** is a durable JetStream consumer: decode batch → resolve
  registry → batched `COPY` into the `samples` hypertable; lap/sector events
  additionally materialise rows in the relational `laps` table. Target
  source-to-row latency: under 500 ms when the link is healthy.
- **live-decoder** republishes a configured subset of channels as plain JSON
  over a small local MQTT broker (websockets) for Grafana's live gauge
  panels. This is the only MQTT left in the system, it never crosses the
  radio, and it is disposable — replacing it with Grafana Live push would
  touch nothing else.
- **session-control** publishes driver/stint/session state on `cmd.<vehicle>.*`
  (replicated to the vehicle over the same leafnode); the agent stamps active
  session identity onto derived channels so every lap is attributable.
- **ntrip-client** runs at the pit because that's where the internet is:
  RTK correction bytes flow vehicle-ward on core NATS (`rtcm.<vehicle>`),
  deliberately fire-and-forget — a stale correction is a useless correction.
  GNSS credentials never exist on the vehicle.

## Link dropout and recovery

```mermaid
sequenceDiagram
  participant V as vehicle TELE (JetStream)
  participant L as leafnode link
  participant P as pit TELE (sourced stream)
  participant W as ingest-writer
  Note over V,W: healthy: batches flow at tick rate, pit lag ≈ 0
  V->>P: batch seq 1041..1042
  P->>W: deliver, COPY to DB
  Note over L: link drops (RF shadow, crash, overnight)
  V->>V: keeps appending seq 1043..8800<br/>(file store, retention-capped)
  Note over P: live gauges go stale — visibly, via sys.link lag channel
  Note over L: link returns (or car rolls into garage wifi)
  P->>V: resume sourcing from seq 1043
  V-->>P: replay 1043..8800 at link speed
  P->>W: writer drains in order, DB heals — no gaps, no duplicates
  Note over V,W: no operator action, no sync service, no diff scan
```

Overload behaves the same way as outage: if offered load exceeds link
capacity on a bad-RF day, the pit stream lags rather than dropping. Live
dashboards fall behind (and say so); history is complete once the stream
catches up. The manual relief valve is per-channel `rate_cap`/`rbe` in the
catalog — a config push, not a code change.

## What replaced what

The predecessor system (same author, private repo) accreted seven always-on
data services; openlaps needs four. For the curious:

| Before | After | Why it could be deleted |
| --- | --- | --- |
| 3 × mosquitto (vehicle, pit, bridge) | 2 × nats-server + 1 small pit-local mosquitto | The bridge's topic-forwarding job *is* leafnode + sourced stream |
| 2 × telegraf (topic→measurement shaping) | ingest-writer | The catalog already names everything; no per-topic parsing config to maintain in duplicate |
| 2 × InfluxDB kept consistent by a sync service | 1 × TimescaleDB | Vehicle durability is the JetStream file store; there is no second database to reconcile |
| data-sync-service (diff-based gap backfill) | — | Sourced streams resume by sequence number; recovery is transport-native |
| per-signal JSON topics (~850 msg/s) | protobuf SampleBatch ticks | ~10× wire reduction; name strings live in the registry, not the hot path |
| GPS "processor" (serial + NTRIP + NMEA + timing + publish in one class) | serial collector + catalog + timing app | Each concern is a layer; GPS-over-CAN becomes a config edit |
| psmqtt host monitoring | host collector | One fewer sidecar and broker dependency; host metrics are ordinary channels |

## Deployment

Two compose stacks (`deploy/vehicle-compose.yaml`, `deploy/pit-compose.yaml`).
The pit initiates the leafnode connection (works behind NAT/WSL2; the vehicle
never needs inbound reachability from the pit LAN). All credentials — NATS
creds files, TLS material, database and dashboard passwords, NTRIP account —
arrive via environment/mounted files documented in `example.env`; none exist
in this repository.

## Document map

- `WIRE_FORMAT.md` — subjects, streams, protobuf schema, registry lifecycle, timestamps
- `AGENT_DESIGN.md` — vehicle agent threading, queues, RBE semantics, failure modes
- `CATALOG.md` — profile schema (`vehicle.yaml`, `catalog.yaml`), naming convention, worked examples
- `LINK_BUDGET.md` — measured bandwidth model vs. radio capacity
- `adr/` — decision records with alternatives considered
