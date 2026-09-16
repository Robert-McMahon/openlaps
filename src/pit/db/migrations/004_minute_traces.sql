-- 004_minute_traces: hierarchical one-minute traces for endurance ranges.
--
-- TimescaleDB 2.28 supports continuous aggregates over continuous aggregates.
-- Compose the one-second extrema directly and weight each second's average by
-- its raw count: avg(avg) is wrong when report-by-exception makes counts differ.

CREATE MATERIALIZED VIEW samples_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(INTERVAL '1 minute', time) AS time,
    channel_key,
    sum(avg * count)::double precision / sum(count) AS avg,
    min(min) AS min,
    max(max) AS max,
    sum(count)::bigint AS count
FROM samples_1s
GROUP BY time_bucket(INTERVAL '1 minute', time), channel_key
WITH NO DATA;

ALTER MATERIALIZED VIEW samples_1m SET (timescaledb.materialized_only = false);

SELECT add_continuous_aggregate_policy(
    'samples_1m',
    start_offset => NULL,
    end_offset => INTERVAL '2 minutes',
    schedule_interval => INTERVAL '1 minute'
);

CREATE VIEW v_samples_1m_named AS
SELECT
    s.time,
    c.vehicle_id,
    c.name AS channel,
    c.units,
    s.avg,
    s.min,
    s.max,
    s.count
FROM samples_1m s
JOIN channels c ON c.channel_key = s.channel_key;

DO $role$
DECLARE
    role_name TEXT := current_setting('openlaps.grafana_db_user', true);
BEGIN
    IF role_name IS NULL OR role_name = '' THEN
        RAISE EXCEPTION 'GRAFANA_DB_USER must be set while applying migration 004';
    END IF;
    EXECUTE format('GRANT SELECT ON v_samples_1m_named TO %I', role_name);
END
$role$;
