-- v_pit_stops closed a stop only on a line crossing. A car that finishes its
-- session parked in the pits never crosses PitExit, so on 2026-11-12 the
-- stop entered at 18:01:20 was still open, and still counting against the
-- wall clock, hours after the practice session had been ended. The PitExit
-- that eventually answers it belongs to the next session and would have
-- turned it into an overnight stop.
--
-- A stop now ends no later than the session it began in. If the session that
-- was running at the entry has ended, and no exit of the stop's kind came
-- before that, the stop closes at the session's end with a NULL exit_line:
-- NULL says no line closed it. Reopening the session (ended back to NULL)
-- puts the stop back as the crossings alone describe it. An entry outside
-- any session is untouched.
--
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
