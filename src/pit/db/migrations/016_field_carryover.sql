-- 016_field_carryover: standings that span more than one timing session.
--
-- At the BEERS 36 Hour (Collie, 13-14 Nov 2026) Natsoft timed one race as
-- two sessions: "(S1)" from 04:32 Friday and "(S2)" from 18:35 Friday. The
-- live standings only ever show the session that is running, so a car's
-- race total is its laps in the earlier part plus its live laps.
--
-- field_carryover holds the earlier parts: one row per car per part, taken
-- from the published result. applies_to names the live session the rows
-- belong in front of, so they stop counting the moment a different session
-- is on the feed. v_field_combined is the read surface: latest live
-- standings plus the carry-over, ranked on the total within each class.

CREATE TABLE field_carryover (
    applies_to   TEXT        NOT NULL,
    part         TEXT        NOT NULL,
    car_number   TEXT        NOT NULL,
    team         TEXT        NULL,
    class        TEXT        NULL,
    laps         INT         NOT NULL CHECK (laps >= 0),
    race_time_s  DOUBLE PRECISION NULL,
    -- When the published result was printed: laps after this are not in it.
    as_of        TIMESTAMPTZ NULL,
    PRIMARY KEY (applies_to, part, car_number)
);

CREATE VIEW v_field_combined AS
WITH live_source AS (
    SELECT source FROM field_cars ORDER BY time DESC LIMIT 1
),
live_session AS (
    SELECT fs.session_name
    FROM field_session fs JOIN live_source ls ON ls.source = fs.source
    ORDER BY fs.time DESC LIMIT 1
),
live AS (
    SELECT s.* FROM v_field_standings s JOIN live_source ls ON ls.source = s.source
),
carry AS (
    SELECT c.car_number, sum(c.laps)::int AS laps, max(c.team) AS team,
           max(c.class) AS class, max(c.as_of) AS as_of
    FROM field_carryover c JOIN live_session s ON s.session_name = c.applies_to
    GROUP BY c.car_number
),
joined AS (
    SELECT coalesce(l.car_number, c.car_number) AS car_number,
           coalesce(l.driver, c.team) AS team,
           coalesce(l.class, c.class) AS class,
           coalesce(c.laps, 0) AS carry_laps,
           coalesce(l.laps, 0) AS live_laps,
           coalesce(c.laps, 0) + coalesce(l.laps, 0) AS total_laps,
           l.last_lap_s, l.best_lap_s, l.in_pit,
           l.time AS live_at, c.as_of AS carry_as_of
    FROM live l FULL JOIN carry c ON c.car_number = l.car_number
),
ours AS (
    SELECT rp.car_number
    FROM v_session_active sa JOIN v_race_plan rp ON rp.session_id = sa.session_id
    WHERE rp.car_number IS NOT NULL
    LIMIT 1
)
SELECT j.car_number, j.team, j.class,
       (rank() OVER (PARTITION BY j.class ORDER BY j.total_laps DESC))::int AS class_position,
       j.carry_laps, j.live_laps, j.total_laps,
       (max(j.total_laps) OVER (PARTITION BY j.class) - j.total_laps)::int AS laps_to_lead,
       (j.total_laps - (SELECT u.total_laps FROM joined u JOIN ours o ON o.car_number = u.car_number))::int
           AS laps_to_us,
       j.car_number = (SELECT car_number FROM ours) AS is_us,
       j.last_lap_s, j.best_lap_s, j.in_pit, j.live_at, j.carry_as_of
FROM joined j;

DO $role$
DECLARE
    role_name TEXT := current_setting('openlaps.grafana_db_user', true);
BEGIN
    IF role_name IS NULL OR role_name = '' THEN
        RETURN;
    END IF;
    EXECUTE format('GRANT SELECT ON field_carryover, v_field_combined TO %I', role_name);
END
$role$;
