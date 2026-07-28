-- 001_init: the pit store's initial schema.
--
-- One TimescaleDB instance holds everything the pit keeps: the `samples`
-- hypertable, the registry-generation bookkeeping that makes samples
-- resolvable to channel names, and the ordinary relational tables for
-- drivers, sessions, stints and laps (ADR 0003).
--
-- The load-bearing rule this schema exists to enforce: `Channel.id` off the
-- wire is only meaningful within one `registry_seq` (proto/telemetry.proto,
-- Channel.id), so it is never stored in `samples`. The writer resolves
-- (vehicle_id, registry_seq, wire_id) -> channels.channel_key at ingest
-- time via `channel_map`, and history then survives any number of catalog
-- edits.
--
-- See docs/PIT_SCHEMA.md for the prose reference and the stable-view
-- contract (ADR 0007).

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- --------------------------------------------------------------------------
-- Channel identity and registry generations
-- --------------------------------------------------------------------------

-- The stable identity of a channel across every registry generation ever
-- seen. One row per (vehicle, canonical name) for the life of the database;
-- `channel_key` is what `samples` stores.
CREATE TABLE channels (
    channel_key BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vehicle_id  TEXT        NOT NULL,
    name        TEXT        NOT NULL,
    units       TEXT        NOT NULL DEFAULT '',
    -- openlaps.v1.ValueType enum number as most recently declared for this
    -- channel; the per-generation truth lives in channel_map.value_type.
    value_type  SMALLINT    NOT NULL,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (vehicle_id, name)
);

-- One row per ChannelRegistry generation observed on the wire. `created` is
-- the registry's own created_unix_ms; `received` is when the pit saw it.
-- registry_seq is a uint32 on the wire, stored wide. Generation 0 is
-- reserved for synthetic (imported, non-wire) channels — see P3.8 and
-- docs/PIT_SCHEMA.md.
CREATE TABLE channel_registry (
    vehicle_id   TEXT        NOT NULL,
    registry_seq BIGINT      NOT NULL,
    created      TIMESTAMPTZ,
    received     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (vehicle_id, registry_seq)
);

-- The registry history ADR 0003 asks for, and the writer's decode lookup:
-- what each wire id meant in each generation, including the fixed-point
-- coefficients a consumer must apply (docs/WIRE_FORMAT.md -> Fixed-point
-- convention). "offset" is quoted throughout: it is a reserved word.
CREATE TABLE channel_map (
    vehicle_id   TEXT             NOT NULL,
    registry_seq BIGINT           NOT NULL,
    wire_id      BIGINT           NOT NULL,
    channel_key  BIGINT           NOT NULL REFERENCES channels (channel_key),
    source_ref   TEXT             NOT NULL DEFAULT '',
    units        TEXT             NOT NULL DEFAULT '',
    value_type   SMALLINT         NOT NULL,
    scale        DOUBLE PRECISION NOT NULL DEFAULT 0,
    "offset"     DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (vehicle_id, registry_seq, wire_id),
    FOREIGN KEY (vehicle_id, registry_seq)
        REFERENCES channel_registry (vehicle_id, registry_seq) ON DELETE CASCADE
);

-- --------------------------------------------------------------------------
-- Samples
-- --------------------------------------------------------------------------

-- Every decoded sample, numeric or not. Numerics — including bool as 0/1
-- and scale/offset-applied integers — go in `value`; the ~0.1% of rows from
-- STRING channels (lap.event, sys.agent.status) go in `value_text`. One
-- hypertable, not a typed column per wire type and not a split events table.
--
-- Deliberately NO foreign key on channel_key: the per-row FK check is real
-- cost on the COPY path at ~4,000 samples/s (docs/LINK_BUDGET.md), and
-- referential integrity here is the writer's job — it resolves channel_key
-- from channel_map before it ever builds a row.
CREATE TABLE samples (
    time        TIMESTAMPTZ      NOT NULL,
    channel_key BIGINT           NOT NULL,
    value       DOUBLE PRECISION NULL,
    value_text  TEXT             NULL
);

-- 1 hour chunks: ~14 M rows per chunk at the LINK_BUDGET rate of ~4,000
-- samples/s, which keeps chunk count sane over a season while staying small
-- enough that a single session's range query touches one or two chunks.
SELECT create_hypertable('samples', 'time', chunk_time_interval => INTERVAL '1 hour');

-- The working index for every query shape this schema serves: one channel
-- (or a handful), newest-first, over a time range.
CREATE INDEX samples_channel_key_time_idx ON samples (channel_key, time DESC);

ALTER TABLE samples SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'channel_key',
    timescaledb.compress_orderby = 'time DESC'
);

SELECT add_compression_policy('samples', INTERVAL '7 days', if_not_exists => TRUE);

-- NO retention policy, by design: Timescale is the archive, not a buffer.
-- If a deployment ever does need to drop old data, this is the knob — left
-- commented out so that enabling it is an explicit, reviewable decision:
--
--   SELECT add_retention_policy('samples', INTERVAL '2 years');
--
-- and the one-shot equivalent:
--
--   SELECT drop_chunks('samples', older_than => INTERVAL '2 years');

-- --------------------------------------------------------------------------
-- Drivers, sessions, stints
-- --------------------------------------------------------------------------

CREATE TABLE drivers (
    driver_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name      TEXT        NOT NULL UNIQUE,
    created   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Owned by session-control (P3.4); the ingest-writer only reads these to
-- resolve lap FKs. session_id is the id session-control minted and stamped
-- onto derived channels, so it is the natural key here too.
-- session_type and status track the pinned cmd.<vehicle>.session payload
-- schema (docs/WIRE_FORMAT.md).
CREATE TABLE sessions (
    session_id   TEXT PRIMARY KEY,
    vehicle_id   TEXT        NOT NULL,
    session_type TEXT        NOT NULL CHECK (
        session_type IN ('practice', 'qualifying', 'race', 'test')
    ),
    track_name   TEXT,
    car          TEXT,
    started      TIMESTAMPTZ NOT NULL,
    ended        TIMESTAMPTZ,
    status       TEXT        NOT NULL CHECK (status IN ('none', 'active', 'ended'))
);

CREATE TABLE stints (
    stint_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id   TEXT        NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    stint_number INT         NOT NULL,
    driver_id    BIGINT      NOT NULL REFERENCES drivers (driver_id),
    started      TIMESTAMPTZ NOT NULL,
    ended        TIMESTAMPTZ,
    UNIQUE (session_id, stint_number)
);

-- --------------------------------------------------------------------------
-- Laps
-- --------------------------------------------------------------------------

-- Materialised by the ingest-writer from `lap.event` samples
-- (docs/WIRE_FORMAT.md -> lap.event payload schema). session_id/stint_id are
-- nullable because laps are recorded whether or not a session is open — a
-- lap with no session is still a lap.
--
-- The unique key is (vehicle_id, crossed_at), NOT (…, lap_number): one car
-- cannot complete two laps at the same instant, whereas `lap_number` is not
-- unique for anything. It lives in TimingEngine.state, in memory, scoped to
-- one agent run and one track (src/timing/timing_core.py sets it to 1 on the
-- first crossing; src/agent/timing_app.py rebuilds the engine on a track
-- switch), and it is never reset by a session change. So an agent restart
-- mid-session replays lap numbers 1..k under the *same* session_id — keying
-- on it would silently overwrite the earlier laps.
--
-- Keying on the crossing instant instead means upserts converge exactly
-- where they should — writer redelivery and importer re-runs present the
-- same crossing time — and never merge laps that only happen to share a
-- number.
CREATE TABLE laps (
    lap_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vehicle_id TEXT        NOT NULL,
    session_id TEXT        REFERENCES sessions (session_id) ON DELETE SET NULL,
    stint_id   BIGINT      REFERENCES stints (stint_id) ON DELETE SET NULL,
    track_name TEXT,
    lap_number INT         NOT NULL,
    crossed_at TIMESTAMPTZ NOT NULL,
    lap_time_s DOUBLE PRECISION,
    valid      BOOLEAN     NOT NULL DEFAULT TRUE,
    pit_status TEXT,
    direction  TEXT,
    UNIQUE (vehicle_id, crossed_at)
);

-- The unique constraint's index is (vehicle_id, crossed_at), which Postgres
-- scans backwards for free, so it already serves the newest-laps-first query
-- and no separate laps(vehicle_id, crossed_at DESC) index is warranted. What
-- it doesn't serve is the session-scoped lookup v_laps is built for.
CREATE INDEX laps_session_idx ON laps (session_id, lap_number);

CREATE TABLE lap_sectors (
    lap_id       BIGINT      NOT NULL REFERENCES laps (lap_id) ON DELETE CASCADE,
    sector       INT         NOT NULL,
    split_time_s DOUBLE PRECISION,
    crossed_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (lap_id, sector)
);

-- --------------------------------------------------------------------------
-- Ingest bookkeeping
-- --------------------------------------------------------------------------

-- The ingest-writer's idempotency anchor (P3.2): the highest JetStream
-- sequence whose rows are committed. Advanced in the same transaction as the
-- rows themselves, so a redelivered message is recognised and skipped
-- regardless of dedupe windows or outage length.
CREATE TABLE ingest_cursor (
    consumer   TEXT PRIMARY KEY,
    stream     TEXT        NOT NULL,
    stream_seq BIGINT      NOT NULL,
    updated    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------
-- The stable read surface (ADR 0007)
-- --------------------------------------------------------------------------
--
-- These views are the contract for downstream readers, including the private
-- companion repo. The base tables are NOT the contract and may change shape
-- in a later migration; the views are kept stable across such changes.

CREATE VIEW v_samples_named AS
SELECT
    s.time,
    c.vehicle_id,
    c.name AS channel,
    c.units,
    s.value,
    s.value_text
FROM samples s
JOIN channels c ON c.channel_key = s.channel_key;

CREATE VIEW v_laps AS
SELECT
    l.lap_id,
    l.vehicle_id,
    l.track_name,
    l.lap_number,
    l.crossed_at,
    l.lap_time_s,
    l.valid,
    l.pit_status,
    l.direction,
    l.session_id,
    se.session_type,
    se.car,
    st.stint_number,
    d.name AS driver
FROM laps l
LEFT JOIN sessions se ON se.session_id = l.session_id
LEFT JOIN stints st ON st.stint_id = l.stint_id
LEFT JOIN drivers d ON d.driver_id = st.driver_id;
