# 0003: TimescaleDB as the time-series store

## Status

Accepted, 2026-07-26

## Context

The current system runs two InfluxDB instances — a vehicle-edge buffer and a
pit long-term store — kept consistent across HaLow dropouts by the
diff-based `data-sync-service` being removed under ADR 0002. Queries are
written in Flux. Laps, sessions, drivers, and stints are naturally
relational concepts (a lap belongs to a stint, a stint has a driver, a
session has many stints) but are currently modelled as tags on individual
points, which makes cross-cutting analysis (e.g. "all laps by driver X across
session type Y") require Flux's pivot operations rather than a join.

Two upstream facts about InfluxDB itself also matter to this decision: Flux
is deprecated with no further active development from InfluxData, and
InfluxDB 2.x OSS is in maintenance mode, with development effort moved to
the 3.x line's new architecture.

Separately, ADR 0002 already removes the vehicle-side database entirely —
vehicle-side durability is now the JetStream file store, not a local
InfluxDB — so this decision only concerns a **single, pit-side** store.

## Decision

Use **TimescaleDB** (PostgreSQL + hypertables) as the one time-series and
relational store, running only at the pit. Samples land in a `samples`
hypertable (`time, channel_id, value`) with a `channels` dimension table
carrying canonical names, units, source refs, and registry history.
Sessions, stints, drivers, and laps are ordinary relational tables with
foreign keys, written by the pit's ingest-writer from derived timing
channels. Because there is only ever one instance, and its counterpart on
the vehicle is JetStream's own file store rather than a second database,
this removes the two-database synchronization problem outright rather than
making it easier to manage.

## Alternatives considered

- **InfluxDB 3 Core.** Rejected: young project undergoing an architecture
  rewrite, with the OSS/Core edition's retention and compaction controls
  limited relative to the commercial/cloud tiers at this point in its
  maturity. It keeps the same tag-based (non-relational) modelling as 2.x
  for laps/sessions/drivers, so the join-via-pivot problem this decision is
  partly meant to solve would remain. Moving to it still costs the same
  dashboard-rewrite effort as moving to Timescale (new query language, new
  Grafana datasource) while delivering fewer of the relational-modelling
  benefits.
- **QuestDB.** Rejected: strong raw time-series ingest performance, but
  weaker relational modelling for the sessions/stints/drivers/laps side of
  the schema — no foreign-key/join ergonomics comparable to Postgres — and a
  smaller surrounding ecosystem (Grafana plugin maturity, ORM/tooling
  choices) for the pit-side services that need to read and write this data.

## Consequences

**Positive**

- One database to operate and back up, down from two InfluxDB instances plus
  a synchronization service.
- Laps, sessions, stints, and drivers are ordinary SQL tables with foreign
  keys — cross-cutting analysis is a join, not a Flux pivot.
- Standard PostgreSQL tooling (backup/restore, extensions, ORMs, migration
  tools) becomes available to pit services (`ingest-writer`,
  `session-control`) rather than InfluxDB-specific tooling.
- Continuous aggregates replace hand-rolled Flux windowing for long-range
  dashboard panels.

**Negative**

- All existing Grafana dashboards (eight in the current system) must be
  rewritten against SQL; there is no automatic Flux-to-SQL translation.
- The team has less operational experience with PostgreSQL/Timescale in
  this context than with InfluxDB — new failure modes to learn (vacuum
  behaviour, hypertable chunk sizing, compression-policy tuning) replace
  familiar InfluxDB ones.
- This decision does not unify the live and historical query paths: the
  MQTT-Live Grafana panels still depend on the separate `live-decoder` path
  introduced under ADR 0002, not on Timescale, so "live" and "historical"
  remain two different code paths in Grafana — accepted here, not solved.
