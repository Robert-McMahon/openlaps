# 0011: Pit-derived data — namespaces, storage, and the alert path

## Status

Accepted, 2026-09-16.

## Context

Phase 7 (`docs/plan/PHASE7.md`) adds four pit services that *derive* data
rather than carry it: `watch` scores anomaly monitors against telemetry,
`strategy` computes fuel windows and stop plans, `timing-feed` ingests the
other cars from a timing provider, and `notifier` turns Grafana alerts into
something a person acknowledges. None of that existed when the earlier
decisions were taken, and three of those decisions now need extending
rather than reinterpreting.

**Namespaces.** `docs/CATALOG.md` names four namespaces outside `car.*` —
`position.*`, `sys.*`, `lap.*` and `timing.*` — and says they are the only
ones. Phase 6's locked decision 8 already bent that: the pit-side timing
extrapolator publishes `timing.lap_elapsed_pit` and
`timing.sector_elapsed_pit`, suffixed precisely so a pit-derived value can
never be mistaken for a received one. Phase 7 derives far more than two
values, and a suffix convention inside a vehicle namespace does not scale
to it.

**Storage.** The ingest-writer is the one path from the wire into
`samples` (ADR 0003, `docs/PIT_SCHEMA.md`). It consumes JetStream, resolves
registries, and writes with `COPY`. Pit-derived data has no registry, never
crosses the radio, and arrives at whatever cadence the deriving service
chooses. `pit-monitor` faced this first and set the pattern: it writes its
own table (`pit_metrics`) directly, with sync psycopg, one transaction per
poll, as the owner role — and `PIT_SCHEMA.md` explains at length why that
table is *not* `samples` under a synthetic vehicle id.

**Alerting.** Phase 6's eight rules evaluate in Grafana and post to a
webhook on `session-control` that writes a log line. Grafana owns rule
state, `for` durations, silences and history well; it has no concept of
acknowledgement, no audible annunciation, and no retry queue for a pit
whose internet comes and goes. The owner's constraint on the delivery path
is latency: as shipped, a threshold crossing takes up to fifty seconds to
reach anyone, and an endurance pit call cannot wait that long.

**Dependencies.** Nothing in `src/` needs more than the standard library
plus the protocol, database and messaging clients already listed. The
whole-car anomaly monitor and the Monte Carlo race forecast want linear
algebra. Timing71 publishes open-source libraries for live timing analysis
that overlap with `timing-feed` and the forecast.

## Decision

**1. Pit-derived values live in pit-owned namespaces: `watch.*`,
`strategy.*` and `field.*`.** They never appear in a catalog, never carry a
`from:`, and never cross the radio. A pit service may read any vehicle
channel; it may publish only under a pit-owned namespace. The two
`timing.*_pit` channels are grandfathered and are the only vehicle-namespace
names a pit service will ever emit. `docs/CATALOG.md` records the
namespaces alongside the vehicle ones.

**2. A pit service that derives a table writes that table itself, in the
`pit-monitor` pattern.** Direct psycopg writes as the owner role, one
transaction per evaluation, a hypertable where the cadence warrants it, a
view over it in `docs/PIT_SCHEMA.md`'s stable read surface, and a
`grafana_ro` grant in the migration that creates it. The ingest-writer is
not taught a second input; the JetStream sourced stream stays the only
thing it consumes.

**3. Grafana is the alert manager; `notifier` is its only contact point.**
Rules stay provisioned Grafana rules, rendered from `profiles/<car>/alarms.yaml`
by a generator so the limits are car configuration in physical units and
the Kelvin conversion happens in one tested place. Grafana's native
delivery integrations are not used. Every notification goes to `notifier`,
which records it, annunciates it, fans it out by severity with a retry
queue, and holds the acknowledgement state. The latency budget is a
requirement of this decision, not a tuning afterthought: **under ten
seconds from threshold crossing to phone for a critical alarm**, achieved
by lowering Grafana's evaluation floor and `group_wait` and choosing each
alarm's `for` from the physics. If a bench measurement shows that budget
cannot be met inside Grafana, the fallback is sample-rate evaluation of
critical limits in `watch`, posting to `notifier` directly, with Grafana
kept as the record — and that fallback is built only on that evidence.

**4. `numpy` is a dependency of the pit services.** For the whole-car
monitor's least-squares fits and residual covariance, and for the race
forecast's simulation. `scikit-learn`, `pandas` and anything carrying a
compiled BLAS beyond what `numpy` wheels bundle are not, unless a later
ADR says why a binned lookup table or a least-squares fit could not do the
job.

**5. Timing71 is adopted as a format, not as a dependency.** Its Common
Timing Data state shape and column vocabulary inform the `field_*` schema,
and its standalone WebSocket message protocol is an accepted ingest format.
Its libraries are not imported or vendored.

## Alternatives considered

- **Suffix conventions inside vehicle namespaces** (`car.oil_pressure_expected`).
  Rejected: it puts pit opinions in the namespace that means "what the car
  said", and a dashboard glob on `car.*` would pick them up.
- **Routing derived data back through JetStream and the ingest-writer**, so
  every row in the database has one writer. Rejected: it would require a
  registry for data that has no wire form, a pit-local stream the sourced
  stream does not know about, and a second consumer in a service whose
  single job is currently easy to state. `pit-monitor` already declined
  this for the same reasons.
- **A custom alarm engine replacing Grafana alerting.** Sub-second latency
  for free, but it duplicates rule state, silences, history and a UI that
  already exist and are tested, and it makes the alert path a large new
  surface. Kept as the fallback in decision 3, gated on a measurement.
- **Grafana's native Discord/Telegram/ntfy contact points.** Zero code,
  but no acknowledgement, no offline queue, and delivery policy in
  provisioning YAML where it cannot be unit-tested.
- **Timing71's libraries as dependencies.** JavaScript on mobx-state-tree
  under AGPL-3.0, in a Python repository under Apache-2.0 whose frontend
  rule is "hand-written and buildless". A combined work served over a
  network would carry AGPL obligations, and the part that would save real
  work — the provider plugins — is a private package.

## Consequences

**Five tables and their views are declared before they exist.**
`docs/PIT_SCHEMA.md` carries a "declared for Phase 7" section so that the
three packages building against them share one shape; each package's
migration is expected to match the declaration or amend it in the same
commit.

**Four new pit services, each with the full plumbing.** `/health`, a
compose entry, an `example.env` block, a deploy-topology row. Phase 6
accepted that cost for one service; this ADR accepts it for four, and
says so rather than absorbing them into services whose single job is
currently easy to describe.

**The alert path acquires a heartbeat.** Because `notifier` and not
Grafana owns the annunciator, a quiet night and a broken pipe look the
same unless something says otherwise. One always-firing rule on a short
interval, and an indicator that goes red when it stops arriving, is part
of decision 3 rather than an optional extra.

**`timing.*_pit` is a closed set.** No further vehicle-namespace names are
emitted from the pit. If a future service wants to extend the pit clock it
publishes under `strategy.*` or a new pit-owned namespace recorded here.
