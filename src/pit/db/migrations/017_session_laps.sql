-- Two changes for the lap dashboards.
--
-- 1. v_pit_stops read lap.event through a plain join from samples to
--    channels. The planner hashed that join and decompressed every text
--    sample in every chunk (a million fix-quality rows per race hour) to keep
--    a few hundred events: 1.9 s over the Collie 36 Hour database. Driving
--    the read from the channel row instead lets each chunk be filtered on
--    its channel_key segment first: 0.1 s for the same rows. OFFSET 0 is what
--    stops the planner flattening the lateral back into the hash join.
--    Columns and their types are unchanged.
--
-- 2. v_session_laps: v_laps plus what a lap report needs and the car's own
--    lap record cannot supply.
--      session_lap   1..n in crossing order within the session. lap_number
--                    is the agent's in-memory counter and restarts at 1 with
--                    the agent (three times in the Collie race).
--      lap_in_stint  1..n in crossing order within the stint.
--      lap_start_at  crossed_at less the lap time, or the previous crossing
--                    of the session when the lap time is NULL.
--      lap_kind      'flying', or 'in' / 'out' / 'stop' when a pit entry, a
--                    pit exit or both fell inside the lap. pit_status only
--                    says where the car was at the crossing: at a circuit
--                    whose pit lane does not cross the timing line every
--                    pit lap reads 'track' (1 of 1,162 Collie race laps read
--                    'pit', against 30 stops).
--      clean         valid, flying and timed: the laps pace statistics rest on.
CREATE OR REPLACE VIEW v_pit_stops AS
WITH lap_events AS (
    SELECT
        s.time,
        c.vehicle_id,
        -- Guarded as in 012: the cast must never meet another channel's text.
        CASE WHEN c.name = 'lap.event' THEN s.value_text::jsonb END AS event
    FROM channels c
    CROSS JOIN LATERAL (
        SELECT sm.time, sm.value_text
        FROM samples sm
        WHERE sm.channel_key = c.channel_key
          AND sm.value_text IS NOT NULL
        OFFSET 0
    ) s
    WHERE c.name = 'lap.event'
), pit_events AS (
    SELECT
        time,
        vehicle_id,
        event ->> 'type' AS event_type,
        event ->> 'line' AS line,
        CASE
            WHEN lower(event ->> 'line') LIKE '%refuel%' THEN 'refuel'
            WHEN lower(event ->> 'line') LIKE '%service%' THEN 'service'
            ELSE 'unknown'
        END AS line_type
    FROM lap_events
    WHERE event ->> 'type' IN ('pit_entry', 'pit_exit')
), paired AS (
    SELECT
        time AS entry_at,
        vehicle_id,
        event_type,
        line AS entry_line,
        line_type AS stop_type,
        lead(time) OVER vehicle_events AS next_at,
        lead(event_type) OVER vehicle_events AS next_type,
        lead(line) OVER vehicle_events AS next_line
    FROM pit_events
    -- Per line type, not per vehicle: the next event *of this kind* is the
    -- one that answers an entry, whatever other kinds happen in between.
    WINDOW vehicle_events AS (PARTITION BY vehicle_id, line_type ORDER BY time)
), classified AS (
    SELECT
        paired.*,
        -- The end of the session running at the entry; NULL while it is
        -- still running or when the entry fell outside every session.
        session.ended AS session_ended,
        -- NULL-safe: false when there is no next event, and false when the
        -- answering exit came after the entry's session had ended.
        coalesce(
            next_type = 'pit_exit' AND (session.ended IS NULL OR next_at <= session.ended),
            false
        ) AS closes
    FROM paired
    LEFT JOIN LATERAL (
        SELECT se.ended
        FROM sessions se
        WHERE se.vehicle_id = paired.vehicle_id
          AND se.started <= paired.entry_at
          AND (se.ended IS NULL OR se.ended > paired.entry_at)
        ORDER BY se.started DESC
        LIMIT 1
    ) session ON true
    WHERE event_type = 'pit_entry'
)
SELECT
    vehicle_id,
    entry_at,
    CASE WHEN closes THEN next_at ELSE session_ended END AS exit_at,
    extract(epoch FROM (
        coalesce(CASE WHEN closes THEN next_at ELSE session_ended END, clock_timestamp())
        - entry_at
    )) AS duration_s,
    NOT closes AND session_ended IS NULL AS is_open,
    stop_type,
    entry_line,
    CASE WHEN closes THEN next_line END AS exit_line
FROM classified;

CREATE VIEW v_session_laps AS
WITH stops AS MATERIALIZED (
    SELECT vehicle_id, entry_at, exit_at, is_open
    FROM v_pit_stops
    -- The pit lane itself. A refuel or service bay sits inside a pit-lane
    -- stop and would only repeat it.
    WHERE stop_type = 'unknown'
), numbered AS (
    SELECT
        vl.*,
        row_number() OVER session_order AS session_lap,
        row_number() OVER (
            PARTITION BY vl.vehicle_id, vl.session_id, vl.stint_number
            ORDER BY vl.crossed_at
        ) AS lap_in_stint,
        coalesce(
            vl.crossed_at - vl.lap_time_s * interval '1 second',
            lag(vl.crossed_at) OVER session_order
        ) AS lap_start_at
    FROM v_laps vl
    WINDOW session_order AS (PARTITION BY vl.vehicle_id, vl.session_id ORDER BY vl.crossed_at)
), kinds AS (
    SELECT
        n.*,
        EXISTS (
            SELECT 1 FROM stops p
            WHERE p.vehicle_id = n.vehicle_id
              AND p.entry_at > n.lap_start_at AND p.entry_at <= n.crossed_at
        ) AS has_entry,
        EXISTS (
            SELECT 1 FROM stops p
            WHERE p.vehicle_id = n.vehicle_id
              AND NOT p.is_open
              AND p.exit_at > n.lap_start_at AND p.exit_at <= n.crossed_at
        ) AS has_exit
    FROM numbered n
)
SELECT
    lap_id, vehicle_id, track_name, lap_number, crossed_at, lap_time_s, valid,
    pit_status, direction, session_id, session_type, car, stint_number, driver,
    session_lap,
    lap_in_stint,
    lap_start_at,
    CASE
        WHEN has_entry AND has_exit THEN 'stop'
        WHEN has_entry THEN 'in'
        WHEN has_exit THEN 'out'
        WHEN pit_status = 'pit' THEN 'in'
        ELSE 'flying'
    END AS lap_kind,
    valid AND lap_time_s IS NOT NULL AND NOT has_entry AND NOT has_exit
        AND pit_status IS DISTINCT FROM 'pit' AS clean
FROM kinds;

DO $role$
DECLARE
    role_name TEXT := current_setting('openlaps.grafana_db_user', true);
BEGIN
    IF role_name IS NULL OR role_name = '' THEN
        RAISE EXCEPTION 'GRAFANA_DB_USER must be set while applying migration 017';
    END IF;
    EXECUTE format('GRANT SELECT ON v_session_laps TO %I', role_name);
END
$role$;
