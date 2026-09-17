# 0002: NATS JetStream as the transport backbone

## Status

Accepted, 2026-07-26

## Context

The current system forwards per-signal JSON over MQTT: three mosquitto
brokers, two Telegraf instances, and two InfluxDB instances (vehicle-edge and
pit long-term) kept consistent across HaLow link dropouts by a diff-based
`data-sync-service` that compares coverage between the two databases and
backfills gaps. Most of this sprawl exists to solve one problem — "what data
did the pit miss while the link was down, and how do we get it there without
duplicating what it already has" — with application-level reconciliation
logic.

The legacy system's link-budget analysis measured this concretely rather than
estimating it:

- The default configuration offers roughly **850 MQTT messages/s**, which
  does not fit a 2 MHz MCS4 HaLow channel (3.90 Mbps PHY, ~1.95 Mbit/s
  realistic goodput at 50% airtime efficiency) — a several-times reduction
  is required, not a marginal trim.
- Of the bytes actually on the wire for a per-signal CAN message
  (`{"name":…,"value":…,"units":…,"timestamp":…,"message_name":…}` on a
  ~47-byte topic string), only about **5% is the actual signal value** — the
  remaining ~95% is repeated JSON keys, topic strings, and MQTT/TCP framing.
  Put the other way: today's JSON spends roughly **20× the actual
  signal-value bytes** on framing and names.
- A byte-accurate protobuf sizing of the same CAN stream (§7 of the
  notebook, using Sparkplug B's field layout as the reference encoding)
  shows this collapses by **roughly an order of magnitude** once the
  payload is binary, aliased (integer IDs instead of repeated name
  strings), and bundled per-frame instead of per-signal.

## Decision

Adopt **NATS JetStream** as the transport backbone, with protobuf payloads
(`ChannelRegistry` + `SampleBatch`, see the wire-format spec). The vehicle
runs a `nats-server` with a durable, file-backed `TELE` stream as the single
source of truth for everything the vehicle produces. The pit `nats-server`
connects to the vehicle as a **leafnode over a TLS connection** and sources
(mirrors) the vehicle's `TELE` stream; a durable consumer on the pit side
resumes automatically from its last acknowledged sequence number after any
disconnection.

The deciding argument is structural, not just bandwidth: with a
sourced/mirrored durable stream, link-dropout recovery *is* "the consumer
resumes from the last sequence" — a JetStream feature, not application code.
This eliminates the `mqtt-bridge`, the diff-based `data-sync-service` (and
its reconciliation scripts), and most of the broker sprawl, because there is
no longer a second independent database on the vehicle whose coverage needs
comparing against the pit's.

## Alternatives considered

- **MQTT + Sparkplug B.** Bundles the same three levers (batching, integer
  aliases, binary encoding) that the notebook's byte-accurate model credits
  with the ~10× reduction above, and adds a BIRTH/DEATH session model. It
  was rejected as the transport backbone because it does not touch the
  problem NATS JetStream solves structurally: Sparkplug is a wire encoding
  over MQTT, and the vehicle/pit split still needs two databases kept in
  sync — the diff-based reconciliation problem remains, only the payloads
  on the wire get smaller. It also breaks the existing Grafana MQTT-Live
  datasource exactly as much as the chosen design does (both need a
  pit-side decoder to turn protobuf back into plain topics for that
  datasource to keep working — this is not a Sparkplug-specific cost).
  Finally, Sparkplug's report-by-exception is baked into the protocol
  (points recorded on change, reads fill-forward), which is a bigger and
  less controllable semantic shift than opting into RBE per channel,
  on our own terms, in the catalog (see ADR 0005).
- **MQTT5 + a custom protobuf payload**, keeping the existing
  broker/bridge topology and only changing the encoding (plus v5 topic
  aliases). This was the smallest implementation delta from today, and the
  notebook's scenario table (§6) shows that batching + minimal payload alone
  already fits the link without changing transport at all. It was rejected
  because it leaves the entire broker/bridge/sync-service sprawl in place —
  it fixes the bandwidth problem but not the dropout-recovery/consistency
  problem that motivates most of the current system's complexity — and it
  produces a bespoke wire format with no standard or ecosystem behind it.

## Consequences

**Positive**

- Dropout recovery becomes a JetStream property (sourced stream + durable
  consumer with sequence tracking) instead of hand-written reconciliation
  logic.
- Deletes `mqtt-bridge`, `data-sync-service` and its backfill scripts, the
  vehicle-side mosquitto broker, and folds `psmqtt`'s role into a generic
  host-metrics collector.
- Roughly an order-of-magnitude bandwidth reduction versus today's per-signal
  JSON, per the byte-accurate protobuf sizing model — leaving headroom on
  the same HaLow radio to also carry video.
- The vehicle's JetStream file store is simultaneously the durability layer
  and the transport buffer — there is no separate vehicle-side database or
  write-ahead mechanism to keep consistent with anything else.

**Negative**

- NATS/JetStream is new operational surface the project has not run in
  production before (nkeys/creds management, leafnode TLS, stream and
  consumer provisioning), compared with the mature, well-understood
  mosquitto setup it replaces.
- Grafana's MQTT-Live datasource still cannot consume protobuf directly, so
  a pit-side `live-decoder` republishing selected channels as plain JSON to
  a (single, pit-only) mosquitto instance is still required — this is
  unavoidable in every design considered here, but it means "no bridge
  needed anywhere" was never actually on the table.
- The vehicle-side JetStream file store needs explicit retention/capacity
  planning (target ~72h / N GB) that did not exist as a concept before.
- Leafnode behaviour over a lossy, half-duplex link under real RF conditions
  is the single biggest unvalidated assumption in the whole rewrite; it is
  explicitly called out as the gate (garage bench test) before any track use.
