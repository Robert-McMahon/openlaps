# Pit database schema

The pit runs one TimescaleDB instance (ADR 0003) and it holds everything the
pit keeps: decoded samples, the registry bookkeeping that makes those samples
resolvable to channel names, the ordinary relational tables for drivers,
sessions, stints and laps, and the pit's own health. This document is the
reference for that schema; the normative definition is
[`src/pit/db/migrations/`](../src/pit/db/migrations/), which this document
describes rather than duplicates.

There is no vehicle-side database — vehicle durability is the JetStream file
store (ADR 0002) — so nothing here is replicated or reconciled anywhere.

## Migrations

Plain `.sql` files applied in filename order by a small applier
(`src/pit/db/migrate.py`). No Alembic, no ORM: there is no ORM in this
codebase, the schema is small and mostly DDL, and Postgres has transactional
DDL, so "run the file inside one transaction that also records it in
`schema_migrations`" is the whole mechanism. A migration either lands
completely or not at all.

```bash
openlaps-migrate --dry-run    # list pending migrations
openlaps-migrate              # apply them
```

Connection details come from `TIMESCALE_*` in the environment
(`example.env`) — `TIMESCALE_DSN` alone, or host/port/db/user/password.
Nothing about the schema or the data model is configurable; only the wiring
is. Concurrent appliers (two pit services migrating on boot) serialise on a
Postgres advisory lock rather than racing each other's DDL.

Migration `002_trace_read_surface.sql` also consumes `GRAFANA_DB_USER` and
`GRAFANA_DB_PASSWORD` while it is pending. The applier passes them as
session-local Postgres settings rather than interpolating them into the SQL:
the role name is deployment wiring, the password is a secret, and neither
belongs in a committed migration. The default role name is `grafana_ro`; the
password has no default and migration fails closed when it is absent.

Adding a migration means adding `002_*.sql`; existing files are never edited
once applied anywhere.

Every pit service assumes the schema is already there — none of them
migrate on startup, which would have several containers racing the same DDL
on every restart. Applying migrations is a bring-up step that runs once,
after the database is healthy and before anything that writes to it.

## The registry-generation resolution rule

This is the rule the whole channel side of the schema exists to enforce.

`Channel.id` on the wire is only meaningful **within one `registry_seq`** —
a catalog reload renumbers ids (see `proto/telemetry.proto`, `Channel.id`,
and `docs/WIRE_FORMAT.md` → Registry lifecycle). So a wire id is not a stable
key, and it is **never stored in `samples`**. Instead:

1. Every `ChannelRegistry` generation the pit sees becomes a
   `channel_registry` row.
2. Every channel name becomes exactly one `channels` row per vehicle, for
   the life of the database — that row's `channel_key` is the stable
   identity.
3. Every `(registry_seq, wire_id)` pair becomes a `channel_map` row pointing
   at that `channel_key`, carrying what the channel meant *in that
   generation*: units, value type, and the `scale`/`offset` coefficients a
   decoder must apply.

The writer resolves `(vehicle_id, registry_seq, wire_id) → channel_key` at
ingest time and stores only the key. History then survives any number of
catalog edits: a channel renumbered from wire id 3 to wire id 91 is still one
`channel_key`, and one query answers across the rollover.

Consumers must never decode a batch against a different generation. If a
generation is unknown, the answer is to go find it in the stream, not to
guess — `tools/decode.py` is the reference implementation.

`registry_seq = 0` is **reserved for synthetic, non-wire channels**: the
historical importer registers imported channels under generation 0 so
imported and live data share one `channel_key` per canonical name. Live
registries start at 1, so the two can never collide. `channel_map` carries
a composite foreign key to `channel_registry`, so a `(vehicle_id, 0)` row
has to exist before any generation-0 mapping is inserted.

## Tables

### Channel identity

| Table | Key | Holds |
| --- | --- | --- |
| `channels` | `channel_key` (identity), unique `(vehicle_id, name)` | The stable identity of a channel: canonical name, units, `ValueType`, first-seen time |
| `channel_registry` | `(vehicle_id, registry_seq)` | One row per generation seen; `created` is the registry's own `created_unix_ms`, `received` is when the pit saw it |
| `channel_map` | `(vehicle_id, registry_seq, wire_id)` | What each wire id meant in each generation: `channel_key`, `source_ref`, `units`, `value_type`, `scale`, `"offset"` |

`"offset"` is a reserved word in SQL and is quoted everywhere it appears.

### Samples

```
samples(time TIMESTAMPTZ, channel_key BIGINT, value DOUBLE PRECISION NULL, value_text TEXT NULL)
```

One hypertable for every decoded sample, numeric or not:

- **`value`** carries numerics — including `BOOL` as `0`/`1` and `UINT`/
  `INT64` channels after `scale`/`offset` have been applied, so what is
  stored is always the physical value, never the wire value.
- **`value_text`** carries the ~0.1% of rows from `STRING` channels
  (`lap.event`, `sys.agent.status`).

Exactly one of the two is non-NULL for any row. This is deliberately not a
typed column per wire type (seven mostly-NULL columns to serve one rare case)
and not a separate events table (two write paths, two query paths, for 0.1%
of rows).

**`samples` has no foreign key on `channel_key`.** The per-row FK check is
real cost on the `COPY` path at the LINK_BUDGET rate of ~4,000 samples/s, and
referential integrity here is the writer's job — it resolves `channel_key`
from `channel_map` before it ever builds a row.

**Chunks are 1 hour** (`chunk_time_interval => INTERVAL '1 hour'`), roughly
14 M rows per chunk at that rate: large enough that chunk count stays sane
over a season, small enough that a session-length range query touches one or
two chunks.

**Compression** is `segmentby = channel_key`, `orderby = time DESC`, with a
policy at 7 days. Segmenting by channel is what makes the per-channel range
query — the only query shape this table serves — read a compressed batch per
channel instead of scanning everything.

**There is no retention policy, by design.** Timescale is the archive, not a
buffer; the pit stream in front of it is the buffer. The `drop_chunks` /
`add_retention_policy` recipe ships commented out in `001_init.sql` so that
turning it on is an explicit, reviewable decision.

The working index is `samples(channel_key, time DESC)`.

### One-second and one-minute traces

`samples_1s` is a Timescale continuous aggregate for session-length trace
panels. It contains one row per `(one-second bucket, channel_key)` with
`avg`, `min`, `max`, and `count`. Only rows whose numeric `value` is non-NULL
participate; text channels such as `lap.event` are deliberately absent.

The refresh job runs every 30 seconds, materialising through two seconds
behind the present. Real-time aggregation supplies that newest two-second
edge from `samples`, so a current trace does not acquire an artificial gap.
Its start offset is unbounded rather than a fixed lookback: vehicle backlog
and historical imports can insert old samples, and Timescale's invalidation
log then refreshes the affected old buckets without recomputing unchanged
history.

`samples_1m` is the endurance-range aggregate. It cascades from `samples_1s`
on TimescaleDB 2.28+, reducing a 36-hour range from 129,600 buckets per
channel to 2,160. Its average is count-weighted (`sum(avg * count) /
sum(count)`), not `avg(avg)`, because report-by-exception produces unequal
sample counts; minima and maxima compose directly. It has the same
numeric-only rule, real-time mode, unbounded refresh start, and named-view
shape as the one-second aggregate.

`min` and `max` are part of the contract, not optional decoration. An average
of a 100 Hz engine channel can hide a one-sample pressure drop or knock spike;
a dashboard can draw the average as a line and the extrema as a band without
returning the raw 100 rows.

### Sessions, stints, drivers

| Table | Key | Holds |
| --- | --- | --- |
| `drivers` | `driver_id` (identity), unique `name` | The driver roster |
| `sessions` | `session_id` (the id session-control minted) | `vehicle_id`, `session_type`, `track_name`, `car`, `started`, `ended`, `status` |
| `stints` | `stint_id` (identity), unique `(session_id, stint_number)` | Which driver drove which stint, and when |

`session_type` and `status` are CHECK-constrained to the values the
`cmd.<vehicle>.session` payload schema pins (`docs/WIRE_FORMAT.md`):
`practice|qualifying|race|test` and `none|active|ended`.

These three tables are **owned by session-control**; the ingest-writer only
reads them, to resolve lap foreign keys.

### Laps

| Table | Key | Holds |
| --- | --- | --- |
| `laps` | `lap_id` (identity), unique `(vehicle_id, crossed_at)` | One row per completed lap: `session_id`, `stint_id`, `track_name`, `lap_number`, `lap_time_s`, `valid`, `pit_status`, `direction` |
| `lap_sectors` | `(lap_id, sector)` | `split_time_s`, `crossed_at` |

Materialised by the ingest-writer from `lap.event` samples
(`docs/WIRE_FORMAT.md` → `lap.event` payload schema) — the same samples are
also stored in `samples` as raw JSON, so nothing is lost if the
materialisation ever needs redoing.

**`lap_number` is a label, not a key.** It lives in `TimingEngine.state`, in
memory, scoped to one agent run and one track: it starts at 1 on the first
line crossing, increments per lap, is never persisted, and is **not** reset
by a session change — but *is* reset when the engine is rebuilt, which
happens on an agent restart or a track switch. So an agent restart
mid-session replays lap numbers 1..k under the same `session_id`.

The unique key is therefore **`(vehicle_id, crossed_at)`**: one car cannot
complete two laps at the same instant. That key upserts correctly in exactly
the cases that matter —

- writer redelivery after a crash presents the same `lap.event` payload, so
  the same crossing time, so `ON CONFLICT` converges;
- a re-run of the historical importer reads the same source line, likewise;
- an agent restart, a track switch, or a second event on another day produce
  different crossing times, so nothing collides.

It deliberately does not merge the same physical lap arriving through two
different pipelines (a legacy import and a Phase 4 replay of the same event):
those are two records with different provenance and crossing times computed
by different code, and keeping both visible is more useful than silently
overwriting one with the other.

`session_id` and `stint_id` are **nullable**: laps are recorded whether or
not a session is open, and a lap with no session is still a lap. Nothing
mints a synthetic session to fill the gap — with `crossed_at` as the key,
attribution is not needed for correctness, and an invented session would
show up as noise in `v_laps` and in any session picker built on it. An
unattributed lap is found by time range and track, which is how anyone would
look for it.

One ordering obligation for writers: `laps.session_id` is a real foreign key,
checked at insert time (there is no deferral that would help — the row must
exist at commit either way). A writer holding a stamped `session_id` with no
`sessions` row yet — a session-control DB write still queued behind an
outage — must insert the lap with `session_id` NULL rather than let the whole
flush fail.

The unique constraint's index also serves newest-laps-first queries (Postgres
scans it backwards for free); `laps(session_id, lap_number)` serves the
session-scoped lookups `v_laps` exists for.

### Pit health

```
pit_metrics(source TEXT, metric TEXT, time TIMESTAMPTZ, value DOUBLE PRECISION NULL, value_text TEXT NULL)
```

The pit's own chrony, host resources, ntrip-client and NATS server, written by
the pit-monitor (`src/pit/pit_monitor/`). Same numeric/text rule as `samples`:
exactly one of `value` and `value_text` is non-NULL.

**This is deliberately not `samples` under a synthetic `vehicle_id`.** The
whole sample side of the schema is vehicle-scoped — `v_samples_named` answers
in `(vehicle_id, channel)` and every dashboard's Vehicle variable is
`SELECT DISTINCT vehicle_id FROM v_samples_named`. A `'pit'` row there would
appear in six dropdowns and select a car that does not exist. Pit health is
also not telemetry in the sense the rest of this schema means: it never
crosses the radio, has no wire registry behind it, and nothing renumbers it,
so the `channel_key` indirection `samples` needs would buy nothing here.
`(source, metric)` is the identity.

`source` is the probe: `host` (psutil), `chrony` (`chronyc tracking`),
`ntrip` (the ntrip-client's `/health`) and `nats` (the pit server's
`/varz`, `/jsz` and `/leafz`). Metric names under `host` and `chrony` are the
`collectors.host` names the vehicle already publishes, minus their `host:`
prefix — the pit-monitor reuses that collector rather than reimplementing it.
Under `nats`, names carrying a server, stream or consumer identity are
composed (`leaf.<remote>.rtt_s`, `consumer.<stream>.<consumer>.num_pending`),
so dashboard panels match them by pattern rather than by literal.

**Chunks are 1 day**, not the hour `samples` uses: this table accrues a poll's
worth of rows every few seconds, four orders of magnitude below the sample
path, and hourly chunking would only produce a great many nearly-empty ones.
Compression is `segmentby = source, metric` with a policy at 7 days. As with
`samples`, there is no retention policy.

The working index is `pit_metrics(source, metric, time DESC)`.

### Race plans

```
race_plans(plan_id, session_id -> sessions, revision, race_end_at, race_end_laps, end_authority,
           tank_l, usable_fuel_l, refuel_min_s, service_typical_s, driver_limits JSONB,
           planned_stops JSONB, car_number, updated_at, updated_by)   UNIQUE (session_id, revision)
```

The facts about the event the car cannot report (migration 009, P7.8):
when the race ends (a time, a lap count, or both with one authoritative),
tank and usable fuel in litres, the regulated refuelling minimum and the
typical service stop in seconds, the driver-time rules in minutes, the
planned stops, and our car number as the timekeepers know it. Written by
session-control from its operator UI; read by the strategy service.

**Every save is a new revision.** A plan changed at 2 am is a plan somebody
will ask about at 9 am, so an edit inserts and nothing is ever updated.
`v_race_plan` is the latest revision per session and is what strategy
computes from; `v_race_plan_history` is all of them. Constraints hold the
shape honest in the database, not only in the form: usable fuel never
exceeds the tank, and the authoritative end condition must be present.

### The alert ledger

```
alert_events(time, rule_uid, alertname, status, severity, labels JSONB, annotations JSONB, fingerprint, started_at)
alert_acks(fingerprint, started_at, acked_at, acked_by, note)   PK (fingerprint, started_at)
```

Every firing and resolution Grafana sends the notifier (`src/pit/notifier/`,
Grafana's only contact point per ADR 0011), and every acknowledgement a
person makes on the annunciator. Plain tables, not hypertables: a busy race
produces hundreds of rows. `fingerprint` is Grafana's identity for an alert
instance and repeats when the same rule fires again, so `started_at` is
carried alongside it and an acknowledgement is keyed on both -- a later
firing needs its own. `rule_uid` is the alarm key in the profile's
`alarms.yaml` (from Grafana's `__alert_rule_uid__` label), NULL for a
synthetic test alert raised from the annunciator.

Written directly by the notifier, one transaction per notification, in the
`pit_metrics` pattern. A ledger failure is counted on `/health` and never
stops the alert reaching the page.

### Strategy state

```
strategy_state(time, vehicle_id, session_id, trigger, lap_number, plan_revision,
               fuel_remaining_l, fuel_remaining_lo_l, fuel_remaining_hi_l,
               rebase_confidence, rebase_level_l, rebase_at, fuel_added_l,
               burn_l_per_lap, burn_sd, burn_laps, lap_time_ref_s,
               laps_to_dry_lo, laps_to_dry_hi, time_to_dry_s_lo, time_to_dry_s_hi,
               laps_remaining, stops_needed, window_open_lap, window_close_lap, target_lap_s,
               driver, driver_time_remaining_s, driver_total_remaining_s,
               refuel_elapsed_s, refuel_remaining_s, refuel_release_at,
               stop_plan JSONB, plan_drift JSONB)
```

One row per evaluation by the strategy service (`src/pit/strategy/`,
migration 010, P7.9): on every completed lap, on a timer while the car is in
the pits, whenever the race plan changes, and once with a NULL `session_id`
when the session ends so the latest row never shows yesterday's numbers.
`trigger` says which. A daily-chunked hypertable in the `pit_metrics`
pattern, written directly by the service as the owner role, one transaction
per evaluation together with its findings.

Every projection carries its bounds -- `laps_to_dry_lo` is usable fuel at
its low end over burn at its high end (the rolling mean plus two standard
deviations), and the lower bound is the one that gets radioed. The fuel
figure is P6.3's model in code: `rebase_level_l` at `rebase_at` less the
clean per-lap counter deltas since, with an unmeasured lap (a counter reset,
no samples) substituted at the rolling mean and widening the bounds rather
than averaged into the burn. `rebase_confidence` says where the re-base came
from: `key_on` (the first accepted level reading of a stint that began
during a refuelling stop -- the car stationary, the engine not yet turning),
`session_start`, `moving` (the latest on-track level reading, when the
current stint spans the stop and so has no stationary reading after it),
`plan` (no reading at all; the plan's tank figure assumed) or `none`.
`burn_laps` is how many clean laps the mean rests on; in-laps, out-laps,
counter resets and lap-time outliers are excluded. `stop_plan` is the
remaining stops the numbers now say (`lap`, `type`, `driver_in`, `reason`,
`planned_lap`, `delta_laps`), and `plan_drift` its diff against the
operator's plan. The service reads views only and reimplements none of
their semantics.

`v_strategy_latest` is the latest row per vehicle; `v_strategy_history` is
every row. The strategy service's warnings -- driver time within its margin
or over a limit, the pit window closing or closed, the computed plan
drifting from the operator's, a short fill -- are `watch_findings` rows with
`monitor` prefixed `strategy.`, so they alert through the generated `watch`
rules like everything else.

### Watch findings

```
watch_findings(finding_id UUID, vehicle_id, monitor, opened_at, closed_at, severity, peak_score, summary JSONB)
```

One row per finding, open while `closed_at` is NULL: a monitor's judgement
that something is wrong, with `summary` carrying enough (expected, observed,
a message) for the crew to deduce the cause. Written by the strategy service
for the `strategy.*` monitors and by the watch service for the rest (P7.5);
the table is created with `IF NOT EXISTS` by whichever migration lands
first, in the shape declared below for both. `v_watch_findings` is the read
surface, and the generated alert rules count its open rows by severity and
monitor prefix.

### Ingest bookkeeping

`ingest_cursor(consumer, stream, stream_seq, updated)` is the ingest-writer's
idempotency anchor: the highest JetStream sequence whose rows are committed,
advanced in the same transaction as the rows themselves. On restart the
writer skips any delivered message at or below the cursor, which makes
redelivery a no-op regardless of dedupe windows or outage length. See the
ingest-writer's own documentation for why `Nats-Msg-Id` dedupe alone is not
enough.

### Declared for Phase 7, not yet migrated

ADR 0011 has pit services that derive data write their own tables, in the
`pit_metrics` pattern above. `docs/plan/PHASE7.md` builds several of them in
packages that can run in parallel, so their shapes are fixed here first and
each package's migration is expected to match this section or amend it in
the same commit. **None of these tables exist until the migration named
beside them lands**; a dashboard or test that reads one before then is
reading a plan.

| Table | Written by | Columns |
| --- | --- | --- |
| `watch_scores` (hypertable) | `watch` (P7.5) | `time, vehicle_id, monitor, score, residual, expected, observed, baseline_status` |
| `watch_findings` | `watch` (P7.5), `strategy` (P7.9) | Landed in migration 010 (see *Watch findings* above); P7.5's migration must create it with `IF NOT EXISTS` in the same shape |
| `watch_baselines` | `watch` (P7.5) | `vehicle_id, monitor, session_id, stint_number, learned_at, model JSONB` |
| `strategy_state` (hypertable) | `strategy` (P7.9) | Landed in migration 010 (see *Strategy state* above), with `vehicle_id`, the fuel bounds, the re-base, the refuel clock and `plan_drift` added to the declared shape |
| `field_session` (hypertable) | `timing-feed` (P7.10) | `time, source, session_name, event_type, flag_state, sub_status, time_remaining_s, laps_remaining, time_elapsed_s, track_temp` |
| `field_cars` (hypertable) | `timing-feed` (P7.10) | `time, source, car_number, competitor_id, class, position, class_position, laps, last_lap_s, best_lap_s, gap_lead_s, gap_next_s, sec1_s, sec2_s, sec3_s, pit_count, in_pit, pit_flag, driver, state` |
| `field_passings` (hypertable) | `timing-feed` (P7.10) | `time, source, competitor_id, line, passing_type, active` |
| `race_forecasts` | `strategy` (P7.11) | `time, session_id, scenario, car_number, p_position JSONB, expected_position, expected_gap_ahead_s, expected_gap_behind_s, runs` |

Their views — `v_watch_scores`,
`v_field_standings`, `v_field_laps`, `v_field_passings`, `v_field_gaps`,
`v_field_flags`, `v_race_forecast_latest` — join the stable read surface
below as each lands, with the `grafana_ro` grant in the same migration.

## The stable read surface

ADR 0007 puts the private companion repo downstream of this database, with no
automated check spanning both repositories. So the contract has to be cheap
to keep stable, and it is these views:

| View | Columns |
| --- | --- |
| `v_samples_named` | `time, vehicle_id, channel, units, value, value_text` |
| `v_samples_1s_named` | `time, vehicle_id, channel, units, avg, min, max, count` |
| `v_samples_1m_named` | `time, vehicle_id, channel, units, avg, min, max, count` |
| `v_laps` | `lap_id, vehicle_id, track_name, lap_number, crossed_at, lap_time_s, valid, pit_status, direction, session_id, session_type, car, stint_number, driver` |
| `v_lap_sectors` | The `v_laps` context, plus `sector`, `split_time_s`, and the sector's own `crossed_at` (`lap_crossed_at` retains the lap boundary) |
| `v_pit_stops` | `vehicle_id, entry_at, exit_at, duration_s, is_open, stop_type, entry_line, exit_line` |
| `v_lap_fuel` | The `v_laps` context, counter endpoints, `fuel_used_cc`, `fuel_used_l`, and `measurement_status` |
| `v_stint_fuel_level` | Stint/session/driver context, accepted level sample count, start/end/used litres, and the level trend in L/hour |
| `v_pit_metrics` | `time, source, metric, value, value_text` |
| `v_alert_events` | `time, rule_uid, alertname, status, severity, fingerprint, started_at, acked_at, acked_by, note, labels, annotations` |
| `v_session_active` | `vehicle_id, session_id, session_type, track_name, started` — one row per open session |
| `v_race_plan` | The latest `race_plans` revision per session |
| `v_race_plan_history` | Every `race_plans` revision |
| `v_watch_findings` | `finding_id, vehicle_id, monitor, opened_at, closed_at, severity, peak_score, summary` |
| `v_strategy_latest` | The latest `strategy_state` row per vehicle |
| `v_strategy_history` | Every `strategy_state` row |

**The views are the stable surface. The base tables are not.** Anything
reading this database from outside the pit services — the companion repo,
ad-hoc analysis, a future dashboard — should read the views. A later
migration may reshape the base tables; when it does, the views are updated to
keep presenting the same columns, and downstream readers are unaffected.

`v_samples_named` joins `samples` to `channels`, so it answers in canonical
channel names and never exposes a `channel_key` or a wire id.
The two named aggregate views give their continuous aggregates the same time,
vehicle, channel and units columns, then expose the four statistics. They
answer session- and endurance-length numeric traces; they deliberately do not
answer event/text queries or preserve sub-second sample shape. `v_laps` flattens `laps` with
its session, stint and driver names, so the common question ("every lap by
driver X in session type Y") is a `WHERE` clause rather than a four-table
join.

`v_lap_sectors` emits one row per recorded sector and deliberately emits no
NULL row for a lap with no sectors. `v_pit_stops` is the one stable view that
parses raw `lap.event` JSON. It pairs each entry only with the immediately
following exit for that vehicle, and only when that exit's line type (refuel,
service, or unknown) matches the entry's — a mismatch means a crossing was
missed somewhere between them, and closing across it would fabricate a stop.
It discards orphan exits, keeps an entry with no exit (or a mismatched one) as
`is_open`, and evaluates an open duration against the current clock.
The JSON/window work makes it more expensive than the other views; materialise
it later if a race-length query proves too slow.

`v_lap_fuel` samples `car.fuel_total_used` inside each irregular lap window.
It returns NULL with `measurement_status = 'missing'` when no samples exist,
and NULL with `measurement_status = 'counter_reset'` if any adjacent counter
pair decreases. A reset is never presented as negative or plausible fuel
burn. Clean rows expose both cc and litres. `v_stint_fuel_level` remains
separate because level regression has different failure modes: it rejects
readings paired with battery voltage below 12 V to avoid cranking transients,
then exposes the independent stint-scale level trend used to cross-check the
counter model. Fuel-temperature correction is not part of this first model.

`v_strategy_latest` is what the fuel dashboard's stat panels and the session
UI read; `v_strategy_history` draws the projection bands. Both expose every
column of the base table, so a later reshaping keeps presenting them.
`v_watch_findings` is the alert rules' surface: open rows by severity and
monitor prefix.

`v_alert_events` joins each alert event to the acknowledgement for that
firing, so "what fired overnight and who saw it" is one query and a dashboard
can annotate a trace with both.

`v_session_active` is one row per session whose status is `active`, and it
exists for the alert rules: every car-channel rule asks it before judging,
so a car parked overnight with its last lap event still saying "track" does
not page anyone, and ending the session on the session UI is what ends the
alerts (migration 008).

`v_pit_metrics` is the pit-health surface, and it is a plain projection of
`pit_metrics` rather than a join: there is nothing to resolve. It exists so
that the read-surface rule holds without exception — the `system-status`
dashboard's four pit rows read it, and a later reshaping of the base table
leaves them alone.

Grafana's database role has `CONNECT` on this database, `USAGE` on the public
schema, and `SELECT` on exactly these views. It has no privilege on `samples`,
`pit_metrics` or the other base tables. This makes the read-surface boundary enforceable in
Postgres rather than relying on dashboard authors to remember it.

## Related documents

- `ARCHITECTURE.md` — where this database sits in the pit data flow
- `WIRE_FORMAT.md` — registry lifecycle, `lap.event` and session payload schemas
- `CATALOG.md` — canonical channel names and the `encode:` lever behind `scale`/`offset`
- `adr/0003-timescaledb-storage.md`, `adr/0007-private-companion-repo-for-proprietary-exports.md`
