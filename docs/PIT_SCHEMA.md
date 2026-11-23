# Pit database schema

The pit runs one TimescaleDB instance (ADR 0003). It holds everything the
pit keeps: decoded samples, the registry tables that resolve those samples
to channel names, the relational tables for drivers, sessions, stints and
laps, and the pit's own health. This document is the reference for that
schema. The normative definition is
[`src/pit/db/migrations/`](https://github.com/Robert-McMahon/openlaps/tree/main/src/pit/db/migrations); this document
describes it and does not duplicate it.

There is no vehicle-side database. Vehicle durability is the JetStream file
store (ADR 0002), so nothing here is replicated or reconciled anywhere.

## Migrations

Plain `.sql` files applied in filename order by a small applier
(`src/pit/db/migrate.py`). Alembic and an ORM are not used: the codebase
has no ORM, the schema is small and mostly DDL, and Postgres has
transactional DDL. The applier runs each file inside one transaction that
also records it in `schema_migrations`. A migration is applied completely
or not at all.

```bash
openlaps-migrate --dry-run    # list pending migrations
openlaps-migrate              # apply them
```

Connection details come from `TIMESCALE_*` in the environment
(`example.env`): `TIMESCALE_DSN` alone, or host/port/db/user/password.
Only the connection is configurable; the schema and the data model are not.
Concurrent appliers serialise on a Postgres advisory lock, so they cannot
run each other's DDL at the same time.

Migration `002_trace_read_surface.sql` also consumes `GRAFANA_DB_USER` and
`GRAFANA_DB_PASSWORD` while it is pending. The applier passes them as
session-local Postgres settings and does not interpolate them into the SQL.
The role name is deployment configuration and the password is a secret, so
neither is written in a committed migration. The default role name is
`grafana_ro`. The password has no default, and the migration fails when it
is absent.

To add a migration, add the next numbered `.sql` file. A file that has been
applied anywhere is never edited.

Every pit service assumes the schema already exists. No service migrates on
startup, because several containers would then run the same DDL on every
restart. Applying migrations is a bring-up step that runs once, after the
database is healthy and before anything writes to it.

## The registry-generation resolution rule

The channel tables exist to enforce this rule.

`Channel.id` on the wire is only meaningful **within one `registry_seq`**.
A catalog reload renumbers ids (see `proto/telemetry.proto`, `Channel.id`,
and `docs/WIRE_FORMAT.md` → Registry lifecycle). A wire id is therefore not
a stable key, and it is **never stored in `samples`**. Instead:

1. Every `ChannelRegistry` generation the pit sees becomes a
   `channel_registry` row.
2. Every channel name becomes exactly one `channels` row per vehicle, for
   the life of the database. That row's `channel_key` is the stable
   identity.
3. Every `(registry_seq, wire_id)` pair becomes a `channel_map` row pointing
   at that `channel_key`, carrying what the channel meant in that
   generation: units, value type, and the `scale`/`offset` coefficients a
   decoder must apply.

The writer resolves `(vehicle_id, registry_seq, wire_id) → channel_key` at
ingest time and stores only the key. History is then unaffected by catalog
edits: a channel renumbered from wire id 3 to wire id 91 is still one
`channel_key`, and one query covers both generations.

Consumers must never decode a batch against a different generation. If a
generation is unknown, the consumer must fetch it from the stream and must
not guess. `tools/decode.py` is the reference implementation.

`registry_seq = 0` is **reserved for synthetic, non-wire channels**: the
historical importer registers imported channels under generation 0 so
imported and live data share one `channel_key` per canonical name. Live
registries start at 1, so the two cannot collide. `channel_map` carries
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

- **`value`** carries numerics, including `BOOL` as `0`/`1` and `UINT`/
  `INT64` channels after `scale`/`offset` have been applied. The stored
  value is always the physical value, never the wire value.
- **`value_text`** carries the ~0.1% of rows from `STRING` channels
  (`lap.event`, `sys.agent.status`).

Exactly one of the two is non-NULL for any row. The alternatives were
rejected: a typed column per wire type would add seven mostly-NULL columns
for one rare case, and a separate events table would add a second write path
and a second query path for 0.1% of rows.

**`samples` has no foreign key on `channel_key`.** A per-row FK check costs
time on the `COPY` path at the LINK_BUDGET rate of ~4,000 samples/s. The
writer is responsible for referential integrity: it resolves `channel_key`
from `channel_map` before it builds a row.

**Chunks are 1 hour** (`chunk_time_interval => INTERVAL '1 hour'`), roughly
14 M rows per chunk at that rate. This keeps the number of chunks over a
season manageable, and a session-length range query touches one or two
chunks.

**Compression** is `segmentby = channel_key`, `orderby = time DESC`, with a
policy at 7 days. This table serves one query shape, a range query on one
channel. Segmenting by channel lets that query read one compressed batch per
channel instead of scanning everything.

**There is no retention policy.** Timescale is the archive. The buffer is
the pit stream in front of it. The `drop_chunks` /
`add_retention_policy` recipe is commented out in `001_init.sql`, so
enabling it requires an explicit, reviewable change.

The working index is `samples(channel_key, time DESC)`.

### One-second and one-minute traces

`samples_1s` is a Timescale continuous aggregate for session-length trace
panels. It contains one row per `(one-second bucket, channel_key)` with
`avg`, `min`, `max`, and `count`. Only rows whose numeric `value` is non-NULL
are included; text channels such as `lap.event` are excluded.

The refresh job runs every 30 seconds and materialises up to two seconds
behind the present. Real-time aggregation supplies those newest two seconds
from `samples`, so a current trace has no gap at its end.
The refresh has no fixed lookback (its start offset is unbounded): vehicle
backlog and historical imports can insert old samples, and Timescale's
invalidation log then refreshes the affected old buckets without recomputing
unchanged history.

`samples_1m` is the endurance-range aggregate. It cascades from `samples_1s`
on TimescaleDB 2.28+, reducing a 36-hour range from 129,600 buckets per
channel to 2,160. Its average is count-weighted (`sum(avg * count) /
sum(count)`) and not `avg(avg)`, because report-by-exception produces
unequal sample counts. Minima and maxima are taken directly. It has the same
numeric-only rule, real-time mode, unbounded refresh start, and named-view
shape as the one-second aggregate.

`min` and `max` are part of the contract. An average of a 100 Hz engine
channel can hide a one-sample pressure drop or knock spike. With the extrema
a dashboard can draw the average as a line and the range as a band without
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

A stint runs from the pit-exit line to the pit-entry line, so consecutive
stints need not meet: the gap between one stint's `ended` and the next
one's `started` is the stop, and belongs to neither driver. A lap crossed
in that gap (the in-lap finishing in the pit lane) stays with the outgoing
driver. Stints recorded with a single button press still meet exactly.

Two more tables carry what the operator knows and the car cannot report
(migration 013), written from the same session snapshot and replaced
wholesale on every write:

```
session_flags(flag_id, session_id -> sessions, kind, started, ended)
fuel_marks(mark_id, session_id -> sessions, at, level_l)
```

`session_flags` holds red-flag periods (`kind = 'red'`, the only kind so
far; `ended` NULL while the flag is out). The race clock runs through one;
the strategy service stops the driver's clock for it and leaves laps driven
under it out of the burn. A pit stop (`v_pit_stops`) pauses the driver's
clock the same way; the continuous limit counts drive time in the current
stint, so a new stint resets it, also when the same driver gets back in. `fuel_marks` holds "this many litres were in the
tank at this moment" as the crew knows it, which the counter fuel model
re-bases on.

### Laps

| Table | Key | Holds |
| --- | --- | --- |
| `laps` | `lap_id` (identity), unique `(vehicle_id, crossed_at)` | One row per completed lap: `session_id`, `stint_id`, `track_name`, `lap_number`, `lap_time_s`, `valid`, `pit_status`, `direction` |
| `lap_sectors` | `(lap_id, sector)` | `split_time_s`, `crossed_at` |

Materialised by the ingest-writer from `lap.event` samples
(`docs/WIRE_FORMAT.md` → `lap.event` payload schema). The same samples are
also stored in `samples` as raw JSON, so the lap tables can be rebuilt from
them.

**`lap_number` is a label and is not a key.** It is held in
`TimingEngine.state`, in memory, for one agent run and one track. It starts
at 1 on the first line crossing, increments per lap and is never persisted.
A session change does not reset it. Rebuilding the engine does reset it,
which happens on an agent restart or a track switch. An agent restart
mid-session therefore repeats lap numbers 1..k under the same `session_id`.

The unique key is therefore **`(vehicle_id, crossed_at)`**: one car cannot
complete two laps at the same instant. An upsert on that key behaves
correctly in these cases:

- writer redelivery after a crash presents the same `lap.event` payload,
  with the same crossing time, so `ON CONFLICT` updates the existing row;
- a re-run of the historical importer reads the same source line, with the
  same result;
- an agent restart, a track switch, or a second event on another day produce
  different crossing times, so nothing collides.

The key does not merge the same physical lap arriving through two different
pipelines (eg a historical import and a replay of the same event). Those are
two records from different sources, with crossing times computed by
different code. Both are kept, so neither overwrites the other.

`session_id` and `stint_id` are **nullable**: laps are recorded whether or
not a session is open. No synthetic session is created for a lap without
one. With `crossed_at` as the key, attribution is not needed for
correctness, and an invented session would appear in `v_laps` and in any
session picker built on it. An unattributed lap is found by time range and
track.

Writers must observe one ordering rule. `laps.session_id` is a foreign key
checked at insert time (deferring the check would not help, because the row
must exist at commit either way). A writer may hold a stamped `session_id`
that has no `sessions` row yet, eg when a session-control database write is
still queued behind an outage. It must then insert the lap with
`session_id` NULL so that the whole flush does not fail.

The unique constraint's index also serves newest-laps-first queries
(Postgres scans it backwards). `laps(session_id, lap_number)` serves the
session-scoped lookups that `v_laps` is used for.

### Pit health

```
pit_metrics(source TEXT, metric TEXT, time TIMESTAMPTZ, value DOUBLE PRECISION NULL, value_text TEXT NULL)
```

The pit's own chrony, host resources, ntrip-client and NATS server, written by
the pit-monitor (`src/pit/pit_monitor/`). Same numeric/text rule as `samples`:
exactly one of `value` and `value_text` is non-NULL.

**Pit health is not stored in `samples` under a synthetic `vehicle_id`.**
The sample tables and views are vehicle-scoped: `v_samples_named` answers
in `(vehicle_id, channel)` and every dashboard's Vehicle variable is
`SELECT DISTINCT vehicle_id FROM v_samples_named`. A `'pit'` row there would
appear in every Vehicle dropdown and select a car that does not exist. Pit
health also differs from telemetry: it never crosses the link, has no wire
registry, and is never renumbered, so the `channel_key` mapping that
`samples` needs has no use here. `(source, metric)` is the identity.

`source` is the probe: `host` (psutil), `chrony` (`chronyc tracking`),
`ntrip` (the ntrip-client's `/health`) and `nats` (the pit server's
`/varz`, `/jsz` and `/leafz`). Metric names under `host` and `chrony` are the
`collectors.host` names the vehicle already publishes, minus their `host:`
prefix, because the pit-monitor reuses that collector.
Under `nats`, names carrying a server, stream or consumer identity are
composed (`leaf.<remote>.rtt_s`, `consumer.<stream>.<consumer>.num_pending`),
so dashboard panels match them by pattern.

**Chunks are 1 day** (`samples` uses 1 hour). This table receives one poll's
rows every few seconds, four orders of magnitude fewer than the sample
path, so hourly chunks would be nearly empty.
Compression is `segmentby = source, metric` with a policy at 7 days. As with
`samples`, there is no retention policy.

The working index is `pit_metrics(source, metric, time DESC)`.

### Race plans

```
race_plans(plan_id, session_id -> sessions, revision, race_end_at, race_end_laps, end_authority,
           tank_l, usable_fuel_l, refuel_min_s, service_typical_s, driver_limits JSONB,
           planned_stops JSONB, car_number, updated_at, updated_by,
           fuel_used_scale)                                          UNIQUE (session_id, revision)
```

The facts about the event that the car cannot report (migration 009):
when the race ends (a time, a lap count, or both with one authoritative),
tank and usable fuel in litres, the regulated refuelling minimum and the
typical service stop in seconds, the driver-time rules in minutes, the
planned stops, and our car number as the timekeepers know it. Written by
session-control from its operator UI; read by the strategy service.

**Every save is a new revision.** An edit inserts a row and no row is ever
updated, so earlier versions of the plan can be reviewed later.
`v_race_plan` is the latest revision per session and is what strategy
computes from; `v_race_plan_history` is all of them. The database enforces
the same constraints as the form: usable fuel never exceeds the tank, and
the authoritative end condition must be present.

`fuel_used_scale` (migration 013, default 1.0) is the litres actually
burned per litre the ECU's fuel-used counter reports. It multiplies every
counter figure the strategy service uses. It is stored in the plan and not
in the environment because it is learned during the race and must be
correctable without a restart. The `strategy.fuel_calibration` finding
proposes a value after each full-to-surge-light run.

### The alert ledger

```
alert_events(time, rule_uid, alertname, status, severity, labels JSONB, annotations JSONB, fingerprint, started_at)
alert_acks(fingerprint, started_at, acked_at, acked_by, note)   PK (fingerprint, started_at)
```

Every firing and resolution Grafana sends the notifier (`src/pit/notifier/`,
Grafana's only contact point per ADR 0011), and every acknowledgement a
person makes on the annunciator. These are plain tables (a busy race
produces hundreds of rows, too few to need hypertables). `fingerprint` is
Grafana's identity for an alert instance and repeats when the same rule
fires again, so `started_at` is stored with it and an acknowledgement is
keyed on both. A later firing needs its own acknowledgement. `rule_uid` is
the alarm key in the profile's `alarms.yaml` (from Grafana's
`__alert_rule_uid__` label), NULL for a synthetic test alert raised from
the annunciator.

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
migration 010): on every completed lap, on a timer while the car is in
the pits, whenever the race plan changes, and once with a NULL `session_id`
when the session ends so the latest row does not show the previous
session's numbers. `trigger` says which. A daily-chunked hypertable in the `pit_metrics`
pattern, written directly by the service as the owner role, one transaction
per evaluation together with its findings.

Every projection carries its bounds. For example, `laps_to_dry_lo` is
usable fuel at its low end over burn at its high end (the rolling mean plus
two standard deviations). The lower bound is the figure given to the driver.
The fuel figure is `rebase_level_l` at `rebase_at` less the
clean per-lap counter deltas since. An unmeasured lap (a counter reset,
no samples) is substituted at the rolling mean and widens the bounds; it is
not averaged into the burn. `rebase_confidence` says where the re-base came
from: `key_on` (the first accepted level reading of a stint that began
during a refuelling stop, with the car stationary and the engine not yet
turning),
`session_start`, `moving` (the latest on-track level reading, when the
current stint spans the stop and so has no stationary reading after it),
`plan` (no reading at all; the plan's tank figure assumed) or `none`.

That is the level model (`OPENLAPS_STRATEGY_FUEL_MODEL=level`), for a car
whose level sender reads litres. The default **counter model** is for one
whose sender does not. It starts from the latest moment at which the tank's
contents were known and subtracts the fuel-used counter since, summed
directly from the `car.fuel_total_used` samples (restarts from zero
included) and multiplied by the plan's `fuel_used_scale`; it falls back to
the per-lap deltas only when the counter has no samples since the anchor.
`rebase_confidence` then names the anchor: `plan` (the session start, a
full tank assumed), `full` (the end of a refuelling stop at which the
sender read full), `assumed_full` (a refuelling stop nobody and nothing
confirmed), `operator` (a `fuel_marks` row; one made during a stop
replaces that stop's own anchor) or `surge` (the surge-tank switch stayed
closed for `OPENLAPS_STRATEGY_SURGE_HOLD_S`: the main tank is empty and
`OPENLAPS_STRATEGY_SURGE_RESERVE_L` usable litres were left). The sender's
"full" reading and the switch's "closed" reading are a channel, a
threshold and a side each, set in the environment; with no threshold set
neither is looked for. `fuel_added_l` is NULL under this model.
`burn_laps` is how many clean laps the mean rests on; in-laps, out-laps,
counter resets and lap-time outliers are excluded. `stop_plan` is the
remaining stops the numbers now say (`lap`, `type`, `driver_in`, `reason`,
`planned_lap`, `delta_laps`), and `plan_drift` its diff against the
operator's plan. The service reads views only and reimplements none of
their semantics.

`v_strategy_latest` is the latest row per vehicle; `v_strategy_history` is
every row. The strategy service's warnings (driver time within its margin
or over a limit, the pit window closing or closed, the computed plan
drifting from the operator's, a short fill) are `watch_findings` rows with
`monitor` prefixed `strategy.`, so they alert through the generated `watch`
rules in the same way as watch findings.

### Watch findings

```
watch_findings(finding_id UUID, vehicle_id, monitor, opened_at, closed_at, severity, peak_score, summary JSONB)
```

One row per finding, open while `closed_at` is NULL: a monitor's judgement
that something is wrong, with `summary` carrying enough (expected, observed,
a message) for the crew to deduce the cause. Written by the strategy service
for the `strategy.*` monitors and by the watch service for the rest.
Migrations 007 and 010 both create the table with `IF NOT EXISTS`, in the
same shape. `v_watch_findings` is the read
surface, and the generated alert rules count its open rows by severity and
monitor prefix. `summary` always carries `expected`, `observed` and
`baseline`, whichever kind of monitor wrote it (`docs/WATCH.md`), plus
`kind` and a `message`; the watch service's `watch_baselines` checkpoints
carry `stint_number` 0 for a session-wide baseline and the stint number
for a `stint_start` one.

### Field timing

```
field_session(time, source, session_name, event_type, flag_state, sub_status,
              time_remaining_s, laps_remaining, time_elapsed_s, track_temp)
field_cars(time, source, epoch, car_number, competitor_id, class, position, class_position, laps,
           last_lap_s, best_lap_s, gap_lead_s, gap_next_s, sec1_s, sec2_s, sec3_s,
           pit_count, in_pit, pit_flag, driver, state)
field_laps(time, source, car_number, competitor_id, lap_number, lap_time_s, position, class_position,
           gap_lead_s, gap_next_s, pit_count, sec1_s, sec2_s, sec3_s, flag_state, sub_status)
field_passings(time, source, competitor_id, line, passing_type, active, tod, car_number)
```

The other cars, from a timing provider (`src/pit/timing_feed/`, migration
011): four daily-chunked hypertables in the `pit_metrics` pattern,
written directly by the timing-feed service as the owner role, one
transaction per document. `source` names where the rows came from:
`natsoft` (the TCP feed, public or the timekeepers' local one), `replay` (a
capture file), `relay` (the browser relay's snapshots) or `t71` (a Timing71
service). The column vocabulary is a superset of Timing71's Common
Timing Data columns (ADR 0011 decision 5), so every source is stored in
one shape.

Rows are written **on change**, and not on every update. A `field_cars` row
is written
when anything the timing screen shows for that car changes; a
`field_session` row when the flag, the sub-status, the clock or the track
temperature changes. `epoch` on `field_cars` is when the set of cars last
changed (the first full leaderboard, one after a reconnect that names
different cars, a different session). "The latest snapshot" is therefore the
latest row per car within the latest epoch, and a car that left the
standings is not in it. `field_laps` is derived: a row each time a car's
lap count increments, carrying the lap it completed and the standings as
they stood at the crossing, with the flag it was driven under; a service
started mid-race adopts the counts it finds and derives nothing from them.
`field_passings` is every transponder crossing the feed reports, with
`line` naming the loop (`main`, `pit_main`, `pit_entry`, `pit_exit`,
`int1`, `int2`) and `tod` the timekeepers' own stamp when the feed carries
one; `time` is always when the document reached the pit.

`flag_state` and `sub_status` use Timing71's words whatever the source
said: `none`, `green`, `yellow`, `red`, `chequered`, `ended`, and a
sub-status of `sc`, `fcy`, `vsc`, `code_60`, `caution` or `slow_zone` when
the yellow is a safety-car condition, NULL otherwise. `state` is `RUN`,
`PIT`, `OUT`, or the feed's own `DNS` / `DNF` / `DSQ`.

**Our car, reconciled.** The race plan's `car_number` identifies us
in the feed. Every few seconds the service compares the feed's lap count
for that car with the count of `v_laps` rows for the open session. A
difference of more than a lap is a `field.lap_count` finding in
`watch_findings`. Its summary says whose count is higher and therefore
which fault is likely: if the feed is ahead, the vehicle missed a line
crossing; if the vehicle is ahead, the transponder was not seen. The
finding closes when the counts agree again. The `field-warning` and
`field-critical` rules in the profile's `alarms.yaml` alert on it by
severity, as the `strategy-*` rules do for the strategy findings. Our
main-line passings give a second comparison: the offset between the timekeepers' `tod` and the
nearest `v_laps.crossed_at` is the vehicle clock measured against theirs,
written as `clock_offset_s` under source `timing-feed` in `pit_metrics`,
with `feed_latency_s` (arrival minus `tod`) beside it.

The schema differs from the shape declared before the service was built in
these points: `epoch`, `tod` and `car_number` above, `competitor_id` as TEXT
(a passing's ID may be `Safety` or `Course`), and `field_laps` itself.

`field_carryover(applies_to, part, car_number, team, class, laps,
race_time_s, as_of)` (migration 016) holds the earlier parts of a race the
timekeepers run as more than one session, one row per car per part, entered
by hand from the published result. `applies_to` is the `session_name` of the
live session the rows count in front of, so they stop counting when a
different session is on the feed. Nothing writes it automatically.

### Ingest bookkeeping

`ingest_cursor(consumer, stream, stream_seq, updated)` is the ingest-writer's
idempotency anchor: the highest JetStream sequence whose rows are committed,
advanced in the same transaction as the rows themselves. On restart the
writer skips any delivered message at or below the cursor, so redelivery
has no effect whatever the dedupe window or the length of the outage. See the
ingest-writer's own documentation for why `Nats-Msg-Id` dedupe alone is not
enough.

### Planned schema

Watch, strategy and field-timing storage is in migrations 007–011 and is
described above. Race forecasting is not implemented: `race_forecasts` and
`v_race_forecast_latest` do not exist in the current migrations and cannot
be queried.

## The stable read surface

ADR 0007 puts the private companion repo downstream of this database, with no
automated check spanning both repositories. The contract between them is
this set of views:

| View | Columns |
| --- | --- |
| `v_samples_named` | `time, vehicle_id, channel, units, value, value_text` |
| `v_samples_1s_named` | `time, vehicle_id, channel, units, avg, min, max, count` |
| `v_samples_1m_named` | `time, vehicle_id, channel, units, avg, min, max, count` |
| `v_laps` | `lap_id, vehicle_id, track_name, lap_number, crossed_at, lap_time_s, valid, pit_status, direction, session_id, session_type, car, stint_number, driver` |
| `v_lap_sectors` | The `v_laps` context, plus `sector`, `split_time_s`, and the sector's own `crossed_at` (`lap_crossed_at` retains the lap boundary) |
| `v_session_laps` | The `v_laps` columns, plus `session_lap` and `lap_in_stint` (crossing order; `lap_number` restarts with the agent), `lap_start_at`, `lap_kind` (`flying`, `in`, `out`, or `stop` when a pit-lane entry, exit or both fell inside the lap) and `clean` (valid, timed and flying) |
| `v_track_sections` | `track_name, seq, name, kind` (`corner` or `straight`), `start_m, end_m, lap_length_m` as the track map measured them, and `start_fraction, end_fraction` of a lap. Loaded by `tools/track_sections.py`; a section spanning the start line ends before it begins |
| `v_pit_stops` | `vehicle_id, entry_at, exit_at, duration_s, is_open, stop_type, entry_line, exit_line` |
| `v_lap_fuel` | The `v_laps` context, counter endpoints, `fuel_used_cc`, `fuel_used_l`, and `measurement_status` |
| `v_stint_fuel_level` | Stint/session/driver context, accepted level sample count, start/end/used litres, and the level trend in L/hour |
| `v_pit_metrics` | `time, source, metric, value, value_text` |
| `v_alert_events` | `time, rule_uid, alertname, status, severity, fingerprint, started_at, acked_at, acked_by, note, labels, annotations` |
| `v_session_active` | `vehicle_id, session_id, session_type, track_name, started`. One row per open session |
| `v_race_plan` | The latest `race_plans` revision per session |
| `v_race_plan_history` | Every `race_plans` revision |
| `v_session_flags` | `session_id, vehicle_id, kind, started, ended`. Red-flag periods, `ended` NULL while the flag is out |
| `v_fuel_marks` | `session_id, vehicle_id, at, level_l`. What the crew recorded was in the tank |
| `v_watch_findings` | `finding_id, vehicle_id, monitor, opened_at, closed_at, severity, peak_score, summary` |
| `v_strategy_latest` | The latest `strategy_state` row per vehicle |
| `v_strategy_history` | Every `strategy_state` row |
| `v_field_standings` | The latest `field_cars` row per car within the latest `epoch` per source: the standings as the timing screen shows them now |
| `v_field_combined` | `v_field_standings` plus `field_carryover` for the live session: carried, live and total laps per car, ranked on the total within each class, with laps to the class leader and to our car |
| `v_field_laps` | Every `field_laps` row |
| `v_field_passings` | Every `field_passings` row |
| `v_field_flags` | `source, flag_state, sub_status, started_at, ended_at, duration_s`. One row per flag or sub-status change, `ended_at` NULL while current |
| `v_field_gaps` | `time, source, session_id, our_car, lap_number, car_number, position, class, laps, gap_lead_s, gap_to_us_s, laps_to_us`. Every other car's standing at each of our laps, joined on the race plan's car number |

**The views are the stable surface. The base tables are not.** Anything
reading this database from outside the pit services (the companion repo,
ad-hoc analysis, a future dashboard) should read the views. A later
migration may reshape the base tables; when it does, the views are updated to
keep presenting the same columns, and downstream readers are unaffected.

`v_samples_named` joins `samples` to `channels`, so it answers in canonical
channel names and never exposes a `channel_key` or a wire id.
The two named aggregate views give their continuous aggregates the same time,
vehicle, channel and units columns, then expose the four statistics. They
serve session- and endurance-length numeric traces. They do not serve
event/text queries and do not preserve sub-second detail. `v_laps` joins
`laps` with its session, stint and driver names, so a common question (eg
every lap by driver X in session type Y) needs a `WHERE` clause and not a
four-table join.

`v_lap_sectors` emits one row per recorded sector and emits no
NULL row for a lap with no sectors. `v_pit_stops` is the one stable view that
parses raw `lap.event` JSON. It pairs each entry with the next pit event of
its own line type (refuel, service, or unknown) for that vehicle, and only
when that event is an exit. An entry followed by another entry of its kind
means a crossing was missed between them, and pairing across it would
record a stop that did not happen. Pairing within a line type lets stops nest: at Collie a
fuel stop crosses PitEntry, PitEntryRefuel, PitExitRefuel, PitExit and is two
rows, the pit-lane stop and the refuel stop inside it, so anything totalling
time in the pits must merge overlaps. It discards orphan exits, keeps an
entry with no exit of its kind as `is_open`, and evaluates an open duration
against the current clock. A stop ends no later than the session it began in:
once that session has ended, a stop with no exit before then closes at the
session's end with a NULL `exit_line` (the car finished parked in the pits),
and a later exit, which belongs to the next session, does not stretch it.
The JSON and window processing make it more expensive than the other views.
It can be materialised if a race-length query proves too slow.

`v_lap_fuel` samples `car.fuel_total_used` inside each irregular lap window.
It returns NULL with `measurement_status = 'missing'` when no samples exist,
and NULL with `measurement_status = 'counter_reset'` if any adjacent counter
pair decreases. A reset is never reported as a fuel-burn figure. Clean rows expose both cc and litres. `v_stint_fuel_level` remains
separate because level regression has different failure modes: it rejects
readings paired with battery voltage below 12 V to avoid cranking transients,
then exposes the independent stint-scale level trend used to cross-check the
counter model. Fuel-temperature correction is not part of this first model.

`v_strategy_latest` is what the fuel dashboard's stat panels and the session
UI read; `v_strategy_history` draws the projection bands. Both expose every
column of the base table, so a later reshaping keeps presenting them.
`v_watch_findings` is the alert rules' surface: open rows by severity and
monitor prefix.

The `v_field_*` views are the race dashboard's surface.
`v_field_standings` is a `DISTINCT ON` per car within the latest
epoch, so it is one row per car however many changes were written.
`v_field_flags` converts `field_session` into intervals: one row each time
the flag or the sub-status changed, ended by the next change. This is the
form a safety-car model would be fitted on. `v_field_gaps` needs a session with a
race plan that names our car: it takes each of our laps in `field_laps` and
joins every other car's latest `field_cars` row at that instant, so
`gap_to_us_s` is positive for a car behind us on the road and `laps_to_us`
for one behind us on distance. Without a named car it is empty. The two
`LATERAL` lookups make it the most expensive view after `v_pit_stops`. It
can be materialised if a race-length query proves too slow.

`v_alert_events` joins each alert event to the acknowledgement for that
firing, so one query shows what fired and who acknowledged it, and a
dashboard can annotate a trace with both.

`v_session_active` is one row per session whose status is `active`. The
alert rules use it: every car-channel rule checks it before evaluating.
A car parked overnight whose last lap event still says "track" therefore
raises no alert, and ending the session on the session UI ends the alerts
(migration 008).

`v_pit_metrics` is the pit-health surface. It is a plain projection of
`pit_metrics` with no join, because there is nothing to resolve. It exists
so that pit health is also read through a view: the `system-status`
dashboard's four pit rows read it, and a later reshaping of the base table
does not affect them.

Grafana's database role has `CONNECT` on this database, `USAGE` on the public
schema, and `SELECT` on exactly these views. It has no privilege on `samples`,
`pit_metrics` or the other base tables. Postgres therefore enforces the
read-surface boundary, and it does not depend on dashboard authors.

## Related documents

- `ARCHITECTURE.md`: where this database sits in the pit data flow
- `WIRE_FORMAT.md`: registry lifecycle, `lap.event` and session payload schemas
- `CATALOG.md`: canonical channel names and the `encode:` setting behind `scale`/`offset`
- `adr/0003-timescaledb-storage.md`, `adr/0007-private-companion-repo-for-proprietary-exports.md`
