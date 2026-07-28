# Pit database schema

The pit runs one TimescaleDB instance (ADR 0003) and it holds everything the
pit keeps: decoded samples, the registry bookkeeping that makes those samples
resolvable to channel names, and the ordinary relational tables for drivers,
sessions, stints and laps. This document is the reference for that schema;
the normative definition is
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

### Ingest bookkeeping

`ingest_cursor(consumer, stream, stream_seq, updated)` is the ingest-writer's
idempotency anchor: the highest JetStream sequence whose rows are committed,
advanced in the same transaction as the rows themselves. On restart the
writer skips any delivered message at or below the cursor, which makes
redelivery a no-op regardless of dedupe windows or outage length. See the
ingest-writer's own documentation for why `Nats-Msg-Id` dedupe alone is not
enough.

## The stable read surface

ADR 0007 puts the private companion repo downstream of this database, with no
automated check spanning both repositories. So the contract has to be cheap
to keep stable, and it is these two views:

| View | Columns |
| --- | --- |
| `v_samples_named` | `time, vehicle_id, channel, units, value, value_text` |
| `v_laps` | `lap_id, vehicle_id, track_name, lap_number, crossed_at, lap_time_s, valid, pit_status, direction, session_id, session_type, car, stint_number, driver` |

**The views are the stable surface. The base tables are not.** Anything
reading this database from outside the pit services — the companion repo,
ad-hoc analysis, a future dashboard — should read the views. A later
migration may reshape the base tables; when it does, the views are updated to
keep presenting the same columns, and downstream readers are unaffected.

`v_samples_named` joins `samples` to `channels`, so it answers in canonical
channel names and never exposes a `channel_key` or a wire id. `v_laps`
flattens `laps` with its session, stint and driver names, so the common
question ("every lap by driver X in session type Y") is a `WHERE` clause
rather than a four-table join.

## Related documents

- `ARCHITECTURE.md` — where this database sits in the pit data flow
- `WIRE_FORMAT.md` — registry lifecycle, `lap.event` and session payload schemas
- `CATALOG.md` — canonical channel names and the `encode:` lever behind `scale`/`offset`
- `adr/0003-timescaledb-storage.md`, `adr/0007-private-companion-repo-for-proprietary-exports.md`
