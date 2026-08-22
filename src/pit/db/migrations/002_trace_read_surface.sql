-- 002_trace_read_surface: one-second traces and Grafana's read-only role.
--
-- GRAFANA_DB_USER and GRAFANA_DB_PASSWORD are deployment wiring, not schema
-- constants, so the migration applier copies them into session-local
-- `openlaps.grafana_db_*` settings before executing this file. Dynamic SQL is
-- confined to the role DDL below; the password is never written to disk or
-- interpolated into a logged SQL string.
--
-- The refresh policy starts at the beginning of history deliberately. Samples
-- can arrive late after a vehicle/pit outage or from the historical importer;
-- a fixed lookback would silently leave those rows out forever. Timescale's
-- invalidation log limits each run to changed buckets, so this does not
-- recompute the archive every 30 seconds. The newest two seconds are read from
-- the raw hypertable through real-time aggregation while their buckets settle.

CREATE MATERIALIZED VIEW samples_1s
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(INTERVAL '1 second', time) AS time,
    channel_key,
    avg(value) AS avg,
    min(value) AS min,
    max(value) AS max,
    count(*) AS count
FROM samples
WHERE value IS NOT NULL
GROUP BY time_bucket(INTERVAL '1 second', time), channel_key
WITH NO DATA;

ALTER MATERIALIZED VIEW samples_1s SET (timescaledb.materialized_only = false);

SELECT add_continuous_aggregate_policy(
    'samples_1s',
    start_offset => NULL,
    end_offset => INTERVAL '2 seconds',
    schedule_interval => INTERVAL '30 seconds'
);

CREATE VIEW v_samples_1s_named AS
SELECT
    s.time,
    c.vehicle_id,
    c.name AS channel,
    c.units,
    s.avg,
    s.min,
    s.max,
    s.count
FROM samples_1s s
JOIN channels c ON c.channel_key = s.channel_key;

DO $role$
DECLARE
    role_name TEXT := current_setting('openlaps.grafana_db_user', true);
    role_password TEXT := current_setting('openlaps.grafana_db_password', true);
BEGIN
    IF role_name IS NULL OR role_name = '' THEN
        RAISE EXCEPTION 'GRAFANA_DB_USER must be set while applying migration 002';
    END IF;
    IF role_password IS NULL OR role_password = '' THEN
        RAISE EXCEPTION 'GRAFANA_DB_PASSWORD must be set while applying migration 002';
    END IF;

    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = role_name) THEN
        EXECUTE format(
            'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD %L',
            role_name,
            role_password
        );
    ELSE
        EXECUTE format(
            'ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD %L',
            role_name,
            role_password
        );
    END IF;

    EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), role_name);
    EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', role_name);
    EXECUTE format('REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM %I', role_name);
    EXECUTE format('REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM %I', role_name);
    EXECUTE format(
        'GRANT SELECT ON v_samples_named, v_samples_1s_named, v_laps TO %I',
        role_name
    );
END
$role$;
