-- v_pit_stops (003) parsed `value_text::jsonb` in the same WHERE as
-- `c.name = 'lap.event'`. SQL gives no evaluation order between those two
-- predicates, and once other STRING channels carried non-JSON text (a chrony
-- reference id on sys.host.clock_source, a fix-quality letter) the planner was
-- free to run the cast on their rows first: every query on the view then
-- failed with "invalid input syntax for type json", which stopped the
-- strategy evaluator outright.
--
-- CASE is the one construct whose evaluation order Postgres does guarantee,
-- so the cast is only reached for lap.event rows whatever plan is chosen.
-- Columns and their types are unchanged, so CREATE OR REPLACE keeps the
-- grants and the views that read this one.
CREATE OR REPLACE VIEW v_pit_stops AS
WITH lap_events AS (
    SELECT
        s.time,
        c.vehicle_id,
        CASE WHEN c.name = 'lap.event' THEN s.value_text::jsonb END AS event
    FROM samples s
    JOIN channels c ON c.channel_key = s.channel_key
    WHERE c.name = 'lap.event'
      AND s.value_text IS NOT NULL
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
        lead(line) OVER vehicle_events AS next_line,
        lead(line_type) OVER vehicle_events AS next_line_type
    FROM pit_events
    WINDOW vehicle_events AS (PARTITION BY vehicle_id ORDER BY time)
), classified AS (
    SELECT
        paired.*,
        -- NULL when there is no next event at all; every CASE below falls
        -- through to its open-stop branch on NULL exactly as it does on false.
        next_type = 'pit_exit' AND next_line_type = stop_type AS closes
    FROM paired
    WHERE event_type = 'pit_entry'
)
SELECT
    vehicle_id,
    entry_at,
    CASE WHEN closes THEN next_at END AS exit_at,
    extract(epoch FROM (
        CASE WHEN closes THEN next_at ELSE clock_timestamp() END - entry_at
    )) AS duration_s,
    NOT coalesce(closes, false) AS is_open,
    stop_type,
    entry_line,
    CASE WHEN closes THEN next_line END AS exit_line
FROM classified;
