-- 009_race_plans: the facts about the event that strategy needs (P7.8).
--
-- Race end, tank, usable fuel, the regulated stop durations, the driver-time
-- rules and the planned stops. Written by session-control from its operator
-- UI; read by the strategy service (P7.9) and its dashboard. One row per
-- *revision*: a plan changed at 2 am is a plan somebody will ask about at
-- 9 am, so an edit inserts rather than updates, and v_race_plan shows the
-- latest revision per session while v_race_plan_history keeps them all.
--
-- Numbered 009: 007 is the watch service's migration on a parallel branch
-- and 008 is v_session_active. Filename order applies them all.

CREATE TABLE race_plans (
    plan_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id        TEXT        NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    revision          INT         NOT NULL CHECK (revision >= 1),
    -- When the race ends: a wall-clock time, a lap count, or both, with one
    -- of them authoritative for the strategy arithmetic.
    race_end_at       TIMESTAMPTZ NULL,
    race_end_laps     INT         NULL CHECK (race_end_laps IS NULL OR race_end_laps > 0),
    end_authority     TEXT        NOT NULL CHECK (end_authority IN ('time', 'laps')),
    tank_l            DOUBLE PRECISION NOT NULL CHECK (tank_l > 0),
    usable_fuel_l     DOUBLE PRECISION NOT NULL CHECK (usable_fuel_l > 0 AND usable_fuel_l <= tank_l),
    -- Regulated minimum for a refuelling stop and the typical length of a
    -- service stop, in seconds. Event rules, not car facts: they live here,
    -- not in the profile.
    refuel_min_s      INT         NOT NULL CHECK (refuel_min_s >= 0),
    service_typical_s INT         NOT NULL CHECK (service_typical_s >= 0),
    -- {"max_continuous_min": n, "max_total_min": n, "min_rest_min": n}, any
    -- subset. Minutes, because that is how regulations state them.
    driver_limits     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- [{"at_lap": n | "at_ms": epoch-ms, "type": "refuel"|"service",
    --   "driver_in": name?}, ...] in race order.
    planned_stops     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    -- Our car number as the timekeepers know it, for P7.10 to find us in
    -- the field timing feed.
    car_number        TEXT        NULL,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by        TEXT        NULL,
    UNIQUE (session_id, revision),
    CHECK (race_end_at IS NOT NULL OR race_end_laps IS NOT NULL),
    CHECK (end_authority <> 'time' OR race_end_at IS NOT NULL),
    CHECK (end_authority <> 'laps' OR race_end_laps IS NOT NULL)
);

-- The latest revision per session: what strategy computes from.
CREATE VIEW v_race_plan AS
SELECT DISTINCT ON (session_id)
    session_id, revision, race_end_at, race_end_laps, end_authority,
    tank_l, usable_fuel_l, refuel_min_s, service_typical_s,
    driver_limits, planned_stops, car_number, updated_at, updated_by
FROM race_plans
ORDER BY session_id, revision DESC;

-- Every revision, for the 9 am question.
CREATE VIEW v_race_plan_history AS
SELECT
    session_id, revision, race_end_at, race_end_laps, end_authority,
    tank_l, usable_fuel_l, refuel_min_s, service_typical_s,
    driver_limits, planned_stops, car_number, updated_at, updated_by
FROM race_plans;

DO $role$
DECLARE
    role_name TEXT := current_setting('openlaps.grafana_db_user', true);
BEGIN
    IF role_name IS NULL OR role_name = '' THEN
        RAISE EXCEPTION 'GRAFANA_DB_USER must be set while applying migration 009';
    END IF;
    EXECUTE format('GRANT SELECT ON v_race_plan, v_race_plan_history TO %I', role_name);
END
$role$;
