-- 005_pit_metrics: the pit's own health, stored beside the vehicle's telemetry.
--
-- Everything the `system-status` dashboard knows about the vehicle arrives via
-- the catalog -> NATS -> ingest-writer path, and that is deliberate: the pit
-- dials the car, the car needs no inbound reachability, and JetStream backfills
-- a dropout. None of that applies to the pit's own chrony, host resources,
-- ntrip-client and NATS server -- that data never crosses the radio and has no
-- business in the vehicle's catalog.
--
-- So it lands in its own table rather than under a synthetic vehicle_id in
-- `samples`. `v_samples_named` is vehicle-scoped by construction, and every
-- dashboard's Vehicle variable is `SELECT DISTINCT vehicle_id FROM
-- v_samples_named` -- a 'pit' row there would appear in six dropdowns and
-- select an empty car.
--
-- (source, metric) is the identity here, not a channel_key: there is no wire
-- registry behind these numbers and nothing renumbers them, so the indirection
-- `samples` needs would buy nothing.

CREATE TABLE pit_metrics (
    -- 'host' (psutil), 'chrony' (chronyc tracking), 'ntrip' (/health),
    -- 'nats' (:8222/varz, /jsz, /leafz). The collector's probe names.
    source     TEXT             NOT NULL,
    metric     TEXT             NOT NULL,
    time       TIMESTAMPTZ      NOT NULL,
    -- Same rule as `samples`: exactly one of the two is non-NULL. Text carries
    -- the handful of string metrics (chrony's selected source, the ntrip
    -- mountpoint, a leafnode's remote name).
    value      DOUBLE PRECISION NULL,
    value_text TEXT             NULL
);

-- 1 day chunks. The whole table accrues at roughly one poll's worth of rows
-- every few seconds -- four orders of magnitude below `samples` -- so the
-- one-hour chunking that suits a 4,000 sample/s hypertable would only produce
-- a great many nearly-empty chunks here.
SELECT create_hypertable('pit_metrics', 'time', chunk_time_interval => INTERVAL '1 day');

-- The only query shape the dashboard issues: one metric, newest first.
CREATE INDEX pit_metrics_source_metric_time_idx ON pit_metrics (source, metric, time DESC);

ALTER TABLE pit_metrics SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'source, metric',
    timescaledb.compress_orderby = 'time DESC'
);

SELECT add_compression_policy('pit_metrics', INTERVAL '7 days', if_not_exists => TRUE);

-- No retention policy, for the same reason `samples` has none (001_init):
-- Timescale is the archive. Turning one on stays an explicit decision.

-- The stable read surface (docs/PIT_SCHEMA.md, ADR 0007). Grafana holds no
-- privilege on the base table, only on this.
CREATE VIEW v_pit_metrics AS
SELECT
    p.time,
    p.source,
    p.metric,
    p.value,
    p.value_text
FROM pit_metrics p;

DO $role$
DECLARE
    role_name TEXT := current_setting('openlaps.grafana_db_user', true);
BEGIN
    IF role_name IS NULL OR role_name = '' THEN
        RAISE EXCEPTION 'GRAFANA_DB_USER must be set while applying migration 005';
    END IF;
    EXECUTE format('GRANT SELECT ON v_pit_metrics TO %I', role_name);
END
$role$;
