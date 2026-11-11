-- What the operator knows about a session that the car cannot report, added
-- for the first 36-hour race: red-flag periods and fuel marks, both owned by
-- session-control and written from its session snapshot (as `stints` is),
-- plus one more race-plan figure.
--
-- Nothing here changes how a stint is stored, but what a stint *means* is
-- sharper from this migration on: a stint ends when the car crosses the
-- pit-entry line and the next begins at pit exit, so consecutive stints may
-- have a gap between them and the stop belongs to neither driver.

-- A period during which the race is stopped. The race clock keeps running
-- through it; a driver's continuous-drive clock does not, and laps driven
-- under it say nothing about racing fuel burn. `ended` is NULL while the
-- flag is still out. `kind` is a column rather than an assumption so a
-- full-course yellow can be recorded the same way later.
CREATE TABLE session_flags (
    flag_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id TEXT        NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    kind       TEXT        NOT NULL CHECK (kind IN ('red')),
    started    TIMESTAMPTZ NOT NULL,
    ended      TIMESTAMPTZ NULL,
    CHECK (ended IS NULL OR ended >= started)
);
CREATE INDEX session_flags_session_idx ON session_flags (session_id, started);

-- "There were this many litres in the tank at this moment", as the crew
-- knows it: a fill to the brim, or so many jerry cans into a known level.
-- The strategy service re-bases its fuel model on the latest one, which is
-- what keeps the model usable when the level sender is not.
CREATE TABLE fuel_marks (
    mark_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id TEXT        NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    at         TIMESTAMPTZ NOT NULL,
    level_l    DOUBLE PRECISION NOT NULL CHECK (level_l >= 0)
);
CREATE INDEX fuel_marks_session_idx ON fuel_marks (session_id, at);

-- Litres actually burned per litre the ECU's fuel-used counter reports. The
-- counter is computed from injector duty and has not been verified against
-- the tank on this car; 1.0 until a full-to-surge-light interval says
-- otherwise. A plan figure, not an environment variable, because it is
-- learned during the race and must be correctable without a restart.
ALTER TABLE race_plans
    ADD COLUMN fuel_used_scale DOUBLE PRECISION NOT NULL DEFAULT 1.0
        CHECK (fuel_used_scale > 0);

-- New columns go last, so CREATE OR REPLACE keeps the grants and every
-- reader of the existing columns.
CREATE OR REPLACE VIEW v_race_plan AS
SELECT DISTINCT ON (session_id)
    session_id, revision, race_end_at, race_end_laps, end_authority,
    tank_l, usable_fuel_l, refuel_min_s, service_typical_s,
    driver_limits, planned_stops, car_number, updated_at, updated_by,
    fuel_used_scale
FROM race_plans
ORDER BY session_id, revision DESC;

CREATE OR REPLACE VIEW v_race_plan_history AS
SELECT
    session_id, revision, race_end_at, race_end_laps, end_authority,
    tank_l, usable_fuel_l, refuel_min_s, service_typical_s,
    driver_limits, planned_stops, car_number, updated_at, updated_by,
    fuel_used_scale
FROM race_plans;

CREATE VIEW v_session_flags AS
SELECT f.session_id, se.vehicle_id, f.kind, f.started, f.ended
FROM session_flags f
JOIN sessions se ON se.session_id = f.session_id;

CREATE VIEW v_fuel_marks AS
SELECT m.session_id, se.vehicle_id, m.at, m.level_l
FROM fuel_marks m
JOIN sessions se ON se.session_id = m.session_id;

DO $role$
DECLARE
    role_name TEXT := current_setting('openlaps.grafana_db_user', true);
BEGIN
    IF role_name IS NULL OR role_name = '' THEN
        RAISE EXCEPTION 'GRAFANA_DB_USER must be set while applying migration 013';
    END IF;
    EXECUTE format('GRANT SELECT ON v_session_flags, v_fuel_marks TO %I', role_name);
END
$role$;
