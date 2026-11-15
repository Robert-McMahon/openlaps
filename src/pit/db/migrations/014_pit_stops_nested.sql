-- v_pit_stops paired an entry only with the very next pit event. Collie
-- nests its lines: a fuel stop crosses PitEntry, PitEntryRefuel,
-- PitExitRefuel, PitExit. On 2026-11-12 the refuel stop closed correctly but
-- the pit-lane stop around it was answered by another entry, so it stayed
-- open for good and the PitExit that should have closed it was discarded as
-- an orphan.
--
-- Pair within a line type instead: an entry is answered by the next event of
-- its own kind (refuel, service, or unknown), so stops of different kinds
-- may nest or overlap. The guard against fabricating a stop is kept where it
-- belongs -- an entry followed by another entry of the same kind, or by
-- nothing, is still left open, and an exit with no entry of its kind before
-- it is still discarded.
--
-- A nested fuel stop is therefore two rows, the pit-lane stop and the refuel
-- stop inside it; anything adding up time in the pits must merge overlaps.
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
        -- NULL when there is no next event at all; every CASE below falls
        -- through to its open-stop branch on NULL exactly as it does on false.
        next_type = 'pit_exit' AS closes
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
