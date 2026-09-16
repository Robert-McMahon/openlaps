-- P7.5. 006 is reserved for the parallel notifier work package.
CREATE TABLE watch_scores (
    time TIMESTAMPTZ NOT NULL,
    vehicle_id TEXT NOT NULL,
    monitor TEXT NOT NULL,
    score DOUBLE PRECISION CHECK (score BETWEEN 0 AND 1),
    residual DOUBLE PRECISION,
    expected DOUBLE PRECISION,
    observed DOUBLE PRECISION,
    baseline_status TEXT NOT NULL,
    PRIMARY KEY (time, vehicle_id, monitor)
);
SELECT create_hypertable('watch_scores', 'time', chunk_time_interval => INTERVAL '1 day');
CREATE INDEX ON watch_scores (vehicle_id, monitor, time DESC);
ALTER TABLE watch_scores SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'vehicle_id, monitor',
    timescaledb.compress_orderby = 'time DESC'
);
SELECT add_compression_policy('watch_scores', INTERVAL '7 days', if_not_exists => TRUE);

CREATE TABLE watch_findings (
    finding_id UUID PRIMARY KEY,
    vehicle_id TEXT NOT NULL,
    monitor TEXT NOT NULL,
    opened_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ,
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    peak_score DOUBLE PRECISION NOT NULL,
    summary JSONB NOT NULL
);
CREATE INDEX ON watch_findings (vehicle_id, severity) WHERE closed_at IS NULL;
CREATE TABLE watch_baselines (
    vehicle_id TEXT NOT NULL,
    monitor TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    -- 0 means session-wide; positive numbers are reserved for per-stint models.
    stint_number INTEGER NOT NULL DEFAULT 0,
    learned_at TIMESTAMPTZ NOT NULL,
    model JSONB NOT NULL,
    PRIMARY KEY (vehicle_id, monitor, session_id, stint_number)
);
CREATE VIEW v_watch_scores AS SELECT * FROM watch_scores;
CREATE VIEW v_watch_findings AS SELECT * FROM watch_findings;
DO $role$
DECLARE
    role_name TEXT := current_setting('openlaps.grafana_db_user', true);
BEGIN
    IF role_name IS NULL OR role_name = '' THEN
        RAISE EXCEPTION 'GRAFANA_DB_USER must be set while applying migration 007';
    END IF;
    EXECUTE format('GRANT SELECT ON v_watch_scores, v_watch_findings TO %I', role_name);
END
$role$;
