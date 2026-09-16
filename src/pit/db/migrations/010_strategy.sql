-- 010_strategy: the state of the race for this car, per evaluation (P7.9).
--
-- The strategy service reads the stable views (v_lap_fuel, v_stint_fuel_level,
-- v_pit_stops, v_race_plan, v_session_active), computes fuel remaining, burn
-- with bounds, the pit window, the stop plan, driver time and the refuel
-- clock, and writes one row here per evaluation -- on every completed lap,
-- on a timer while the car is in the pits, and whenever the race plan
-- changes. Written directly by the service in the pit-monitor pattern
-- (ADR 0011 decision 2): no registry, never crosses the radio.
--
-- Its warnings are watch_findings rows with `monitor` prefixed `strategy.`,
-- so they alert through the same generated `watch` rules as the anomaly
-- monitors. P7.5's migration (007, on a parallel branch) declares the same
-- table; whichever lands first creates it, so both use IF NOT EXISTS and the
-- shape here is the one docs/PIT_SCHEMA.md declared for both packages.
--
-- Numbered 010: 007 is the watch service's, 008 v_session_active, 009 the
-- race plans. Filename order applies them all.

CREATE TABLE IF NOT EXISTS watch_findings (
    finding_id  UUID PRIMARY KEY,
    vehicle_id  TEXT        NOT NULL,
    monitor     TEXT        NOT NULL,
    opened_at   TIMESTAMPTZ NOT NULL,
    closed_at   TIMESTAMPTZ NULL,
    severity    TEXT        NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    peak_score  DOUBLE PRECISION NOT NULL,
    summary     JSONB       NOT NULL
);
CREATE INDEX IF NOT EXISTS watch_findings_open_idx
    ON watch_findings (vehicle_id, severity) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS watch_findings_monitor_idx
    ON watch_findings (vehicle_id, monitor, opened_at DESC);

CREATE OR REPLACE VIEW v_watch_findings AS SELECT * FROM watch_findings;

-- One row per evaluation. Every projection carries its bounds: the lower
-- bound is the one that gets radioed, and a projection from three laps must
-- not look like one from a full stint.
CREATE TABLE strategy_state (
    time                     TIMESTAMPTZ NOT NULL,
    vehicle_id               TEXT        NOT NULL,
    -- NULL for the row written when a session ends: the dashboard's latest
    -- row then says "no session" rather than showing yesterday's numbers.
    session_id               TEXT        NULL,
    -- What prompted the evaluation: lap | pit | plan | timer | start | idle.
    trigger                  TEXT        NOT NULL,
    lap_number               INT         NULL,
    plan_revision            INT         NULL,
    -- Tank contents by P6.3's model: the last re-base less the counter
    -- deltas since. rebase_confidence says where the re-base came from:
    -- key_on (a stationary reading after a refuel stop), session_start,
    -- moving (the latest on-track level reading), plan (no reading at all,
    -- the plan's tank figure assumed), none.
    fuel_remaining_l         DOUBLE PRECISION NULL,
    fuel_remaining_lo_l      DOUBLE PRECISION NULL,
    fuel_remaining_hi_l      DOUBLE PRECISION NULL,
    rebase_confidence        TEXT        NOT NULL,
    rebase_level_l           DOUBLE PRECISION NULL,
    rebase_at                TIMESTAMPTZ NULL,
    fuel_added_l             DOUBLE PRECISION NULL,
    -- Rolling mean over the last N clean laps: in-laps, out-laps, counter
    -- resets and lap-time outliers (a full-course yellow) excluded.
    burn_l_per_lap           DOUBLE PRECISION NULL,
    burn_sd                  DOUBLE PRECISION NULL,
    burn_laps                INT         NOT NULL,
    lap_time_ref_s           DOUBLE PRECISION NULL,
    laps_to_dry_lo           DOUBLE PRECISION NULL,
    laps_to_dry_hi           DOUBLE PRECISION NULL,
    time_to_dry_s_lo         DOUBLE PRECISION NULL,
    time_to_dry_s_hi         DOUBLE PRECISION NULL,
    laps_remaining           INT         NULL,
    stops_needed             INT         NULL,
    window_open_lap          INT         NULL,
    window_close_lap         INT         NULL,
    target_lap_s             DOUBLE PRECISION NULL,
    driver                   TEXT        NULL,
    driver_time_remaining_s  DOUBLE PRECISION NULL,
    driver_total_remaining_s DOUBLE PRECISION NULL,
    refuel_elapsed_s         DOUBLE PRECISION NULL,
    refuel_remaining_s       DOUBLE PRECISION NULL,
    refuel_release_at        TIMESTAMPTZ NULL,
    -- [{"lap", "type", "driver_in", "reason", "planned_lap", "delta_laps"}]
    stop_plan                JSONB       NOT NULL DEFAULT '[]'::jsonb,
    -- {"planned", "computed", "max_abs_delta", "diverged"}
    plan_drift               JSONB       NOT NULL DEFAULT '{}'::jsonb
);

-- Daily chunks, as pit_metrics: a row per lap plus a few per minute in the
-- pits is thousands of rows a race, not millions.
SELECT create_hypertable('strategy_state', 'time', chunk_time_interval => INTERVAL '1 day');
CREATE INDEX strategy_state_vehicle_idx ON strategy_state (vehicle_id, time DESC);
CREATE INDEX strategy_state_session_idx ON strategy_state (session_id, time DESC);

-- The stable read surface (docs/PIT_SCHEMA.md): the latest evaluation per
-- vehicle for the stat panels and the pit wall, and every evaluation for the
-- projection bands.
CREATE VIEW v_strategy_latest AS
SELECT DISTINCT ON (vehicle_id) *
FROM strategy_state
ORDER BY vehicle_id, time DESC;

CREATE VIEW v_strategy_history AS
SELECT * FROM strategy_state;

DO $role$
DECLARE
    role_name TEXT := current_setting('openlaps.grafana_db_user', true);
BEGIN
    IF role_name IS NULL OR role_name = '' THEN
        RAISE EXCEPTION 'GRAFANA_DB_USER must be set while applying migration 010';
    END IF;
    EXECUTE format(
        'GRANT SELECT ON v_watch_findings, v_strategy_latest, v_strategy_history TO %I',
        role_name
    );
END
$role$;
