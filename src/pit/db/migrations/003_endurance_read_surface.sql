-- 003_endurance_read_surface: endurance strategy read views.
--
-- Keep every dashboard-facing join and event pairing behind a stable view.

CREATE VIEW v_lap_sectors AS
SELECT
    vl.lap_id,
    vl.vehicle_id,
    vl.track_name,
    vl.lap_number,
    vl.crossed_at AS lap_crossed_at,
    vl.lap_time_s,
    vl.valid,
    vl.pit_status,
    vl.direction,
    vl.session_id,
    vl.session_type,
    vl.car,
    vl.stint_number,
    vl.driver,
    ls.sector,
    ls.split_time_s,
    ls.crossed_at
FROM v_laps vl
JOIN lap_sectors ls ON ls.lap_id = vl.lap_id;

-- Pit stops remain events in samples. Restrict and parse the JSON once, then
-- pair each entry with the next pit event for the same vehicle. An exit can
-- therefore never manufacture a stop without an entry, and an exit whose line
-- type differs from the entry's (a refuel entry answered by a service exit --
-- a missed crossing somewhere between them) leaves the stop open rather than
-- closing it into a phantom stop spanning the gap.
CREATE VIEW v_pit_stops AS
WITH pit_events AS (
    SELECT
        s.time,
        c.vehicle_id,
        s.value_text::jsonb ->> 'type' AS event_type,
        s.value_text::jsonb ->> 'line' AS line,
        CASE
            WHEN lower(s.value_text::jsonb ->> 'line') LIKE '%refuel%' THEN 'refuel'
            WHEN lower(s.value_text::jsonb ->> 'line') LIKE '%service%' THEN 'service'
            ELSE 'unknown'
        END AS line_type
    FROM samples s
    JOIN channels c ON c.channel_key = s.channel_key
    WHERE c.name = 'lap.event'
      AND s.value_text IS NOT NULL
      AND s.value_text::jsonb ->> 'type' IN ('pit_entry', 'pit_exit')
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

-- A lap's counter window is irregular, so this remains a lateral lookup over
-- raw numeric samples. Inspect every adjacent pair: comparing endpoints alone
-- can miss a reset followed by enough consumption to overtake the old value.
CREATE VIEW v_lap_fuel AS
SELECT
    vl.lap_id,
    vl.vehicle_id,
    vl.track_name,
    vl.lap_number,
    vl.crossed_at,
    vl.lap_time_s,
    vl.valid,
    vl.pit_status,
    vl.direction,
    vl.session_id,
    vl.session_type,
    vl.car,
    vl.stint_number,
    vl.driver,
    fuel.first_value AS fuel_counter_start_cc,
    fuel.last_value AS fuel_counter_end_cc,
    CASE
        WHEN fuel.sample_count = 0 OR fuel.counter_reset THEN NULL
        ELSE fuel.last_value - fuel.first_value
    END AS fuel_used_cc,
    CASE
        WHEN fuel.sample_count = 0 OR fuel.counter_reset THEN NULL
        ELSE (fuel.last_value - fuel.first_value) / 1000.0
    END AS fuel_used_l,
    CASE
        WHEN fuel.sample_count = 0 THEN 'missing'
        WHEN fuel.counter_reset THEN 'counter_reset'
        ELSE 'clean'
    END AS measurement_status
FROM v_laps vl
CROSS JOIN LATERAL (
    SELECT
        count(*) AS sample_count,
        (array_agg(counter_value ORDER BY time))[1] AS first_value,
        (array_agg(counter_value ORDER BY time DESC))[1] AS last_value,
        coalesce(bool_or(previous_value IS NOT NULL AND counter_value < previous_value), false)
            AS counter_reset
    FROM (
        SELECT
            s.time,
            s.value AS counter_value,
            lag(s.value) OVER (ORDER BY s.time) AS previous_value
        FROM samples s
        JOIN channels c ON c.channel_key = s.channel_key
        WHERE c.vehicle_id = vl.vehicle_id
          AND c.name = 'car.fuel_total_used'
          AND s.value IS NOT NULL
          AND vl.lap_time_s IS NOT NULL
          AND s.time >= vl.crossed_at - vl.lap_time_s * INTERVAL '1 second'
          AND s.time <= vl.crossed_at
    ) counter_samples
) fuel;

-- Stint-scale level regression is deliberately separate from per-lap counter
-- burn. Reject readings accompanied by cranking-level supply voltage (<12 V);
-- if no voltage sample exists within one second, preserve the level reading
-- but leave that limitation visible in the documented contract.
CREATE VIEW v_stint_fuel_level AS
SELECT
    st.stint_id,
    st.session_id,
    se.vehicle_id,
    se.session_type,
    se.track_name,
    se.car,
    st.stint_number,
    d.name AS driver,
    st.started,
    st.ended,
    level_stats.sample_count,
    level_stats.first_level AS level_start_l,
    level_stats.last_level AS level_end_l,
    level_stats.first_level - level_stats.last_level AS level_used_l,
    level_stats.slope_per_second * 3600.0 AS level_trend_l_per_hour
FROM stints st
JOIN sessions se ON se.session_id = st.session_id
JOIN drivers d ON d.driver_id = st.driver_id
CROSS JOIN LATERAL (
    SELECT
        count(*) AS sample_count,
        (array_agg(level_value ORDER BY time))[1] AS first_level,
        (array_agg(level_value ORDER BY time DESC))[1] AS last_level,
        regr_slope(level_value, extract(epoch FROM time)) AS slope_per_second
    FROM (
        SELECT level.time, level.value AS level_value
        FROM samples level
        JOIN channels level_channel ON level_channel.channel_key = level.channel_key
        LEFT JOIN LATERAL (
            SELECT battery.value
            FROM samples battery
            JOIN channels battery_channel
              ON battery_channel.channel_key = battery.channel_key
            WHERE battery_channel.vehicle_id = se.vehicle_id
              AND battery_channel.name = 'car.battery_v'
              AND battery.value IS NOT NULL
              AND battery.time BETWEEN level.time - INTERVAL '1 second'
                                   AND level.time + INTERVAL '1 second'
            ORDER BY abs(extract(epoch FROM (battery.time - level.time)))
            LIMIT 1
        ) nearest_battery ON true
        WHERE level_channel.vehicle_id = se.vehicle_id
          AND level_channel.name = 'car.fuel_level'
          AND level.value IS NOT NULL
          AND level.time >= st.started
          AND level.time <= coalesce(st.ended, clock_timestamp())
          AND (nearest_battery.value IS NULL OR nearest_battery.value >= 12.0)
    ) accepted_levels
) level_stats;

DO $role$
DECLARE
    role_name TEXT := current_setting('openlaps.grafana_db_user', true);
BEGIN
    IF role_name IS NULL OR role_name = '' THEN
        RAISE EXCEPTION 'GRAFANA_DB_USER must be set while applying migration 003';
    END IF;
    EXECUTE format(
        'GRANT SELECT ON v_lap_sectors, v_pit_stops, v_lap_fuel, v_stint_fuel_level TO %I',
        role_name
    );
END
$role$;
