-- 011_field_timing: the other cars, from a timing provider (P7.10).
--
-- The timing-feed service ingests the field -- standings, laps, transponder
-- passings and the flag state -- from the Natsoft TCP feed, a browser relay
-- or a Timing71 service, and writes it here directly in the pit-monitor
-- pattern (ADR 0011 decision 2): no registry, never crosses the radio,
-- pit-owned namespace. The column vocabulary is a superset of Timing71's
-- Common Timing Data columns (locked decision 8), so every source maps onto
-- the same tables and the race forecast (P7.11) reads one shape.
--
-- Rows are written on change, not on every update: a car's row lands when
-- anything the timing screen shows for it changes, a session row when the
-- flag, the clock or the track temperature changes. `epoch` on field_cars
-- is when the *set* of cars last changed (a full leaderboard after a
-- reconnect, a different session): "the latest snapshot" is the latest row
-- per car within the latest epoch, which is what v_field_standings shows.
--
-- Amends the shape docs/PIT_SCHEMA.md declared: field_cars gains `epoch`,
-- field_passings gains `tod` (the timekeepers' own stamp, which is what the
-- clock-offset measurement compares) and `car_number`, competitor_id is
-- TEXT because a passing's ID may be 'Safety' or 'Course', and field_laps
-- is the derived table the brief describes.

CREATE TABLE field_session (
    time              TIMESTAMPTZ NOT NULL,
    source            TEXT        NOT NULL,
    session_name      TEXT        NULL,
    event_type        TEXT        NULL,
    -- none | green | yellow | red | chequered | ended | white (Timing71's words)
    flag_state        TEXT        NULL,
    -- NULL, or sc | fcy | vsc | code_60 | caution | slow_zone
    sub_status        TEXT        NULL,
    time_remaining_s  DOUBLE PRECISION NULL,
    laps_remaining    INT         NULL,
    time_elapsed_s    DOUBLE PRECISION NULL,
    track_temp        DOUBLE PRECISION NULL
);
SELECT create_hypertable('field_session', 'time', chunk_time_interval => INTERVAL '1 day');
CREATE INDEX field_session_source_idx ON field_session (source, time DESC);

CREATE TABLE field_cars (
    time              TIMESTAMPTZ NOT NULL,
    source            TEXT        NOT NULL,
    epoch             TIMESTAMPTZ NOT NULL,
    car_number        TEXT        NOT NULL,
    competitor_id     TEXT        NULL,
    class             TEXT        NULL,
    position          INT         NULL,
    class_position    INT         NULL,
    laps              INT         NULL,
    last_lap_s        DOUBLE PRECISION NULL,
    best_lap_s        DOUBLE PRECISION NULL,
    gap_lead_s        DOUBLE PRECISION NULL,
    gap_next_s        DOUBLE PRECISION NULL,
    sec1_s            DOUBLE PRECISION NULL,
    sec2_s            DOUBLE PRECISION NULL,
    sec3_s            DOUBLE PRECISION NULL,
    pit_count         INT         NULL,
    in_pit            BOOLEAN     NULL,
    pit_flag          TEXT        NULL,
    driver            TEXT        NULL,
    -- RUN | PIT | OUT | STOP | FIN, or the feed's own DNS / DNF / DSQ
    state             TEXT        NULL
);
SELECT create_hypertable('field_cars', 'time', chunk_time_interval => INTERVAL '1 day');
CREATE INDEX field_cars_car_idx ON field_cars (source, car_number, time DESC);
CREATE INDEX field_cars_epoch_idx ON field_cars (source, epoch DESC);

-- Derived when a car's lap count increments: the lap it just completed with
-- the standings as they stood at the crossing. This is what the forecast
-- fits its lap-time distributions on.
CREATE TABLE field_laps (
    time              TIMESTAMPTZ NOT NULL,
    source            TEXT        NOT NULL,
    car_number        TEXT        NOT NULL,
    competitor_id     TEXT        NULL,
    lap_number        INT         NOT NULL,
    lap_time_s        DOUBLE PRECISION NULL,
    position          INT         NULL,
    class_position    INT         NULL,
    gap_lead_s        DOUBLE PRECISION NULL,
    gap_next_s        DOUBLE PRECISION NULL,
    pit_count         INT         NULL,
    sec1_s            DOUBLE PRECISION NULL,
    sec2_s            DOUBLE PRECISION NULL,
    sec3_s            DOUBLE PRECISION NULL,
    flag_state        TEXT        NULL,
    sub_status        TEXT        NULL
);
SELECT create_hypertable('field_laps', 'time', chunk_time_interval => INTERVAL '1 day');
CREATE INDEX field_laps_car_idx ON field_laps (source, car_number, time DESC);

CREATE TABLE field_passings (
    -- When the document arrived at the pit.
    time              TIMESTAMPTZ NOT NULL,
    source            TEXT        NOT NULL,
    competitor_id     TEXT        NOT NULL,
    -- main | pit_main | pit_entry | pit_exit | int1 | int2, or the raw code
    line              TEXT        NOT NULL,
    passing_type      INT         NULL,
    active            TEXT        NULL,
    -- The timekeepers' stamp on the crossing, when the feed carries one.
    tod               TIMESTAMPTZ NULL,
    car_number        TEXT        NULL
);
SELECT create_hypertable('field_passings', 'time', chunk_time_interval => INTERVAL '1 day');
CREATE INDEX field_passings_car_idx ON field_passings (source, car_number, time DESC);

-- The stable read surface (docs/PIT_SCHEMA.md).

-- The latest row per car within the latest epoch per source: the standings
-- as the timing screen shows them now.
CREATE VIEW v_field_standings AS
WITH latest_epoch AS (
    SELECT source, max(epoch) AS epoch FROM field_cars GROUP BY source
)
SELECT DISTINCT ON (c.source, c.car_number) c.*
FROM field_cars c
JOIN latest_epoch e ON e.source = c.source AND e.epoch = c.epoch
ORDER BY c.source, c.car_number, c.time DESC;

CREATE VIEW v_field_laps AS SELECT * FROM field_laps;

CREATE VIEW v_field_passings AS SELECT * FROM field_passings;

-- Flag and sub-status intervals: one row per change, ended by the next
-- change (NULL while current). What the safety-car model will be fitted on.
CREATE VIEW v_field_flags AS
WITH changes AS (
    SELECT time, source, flag_state, sub_status,
           lag(flag_state) OVER w AS previous_flag,
           lag(sub_status) OVER w AS previous_sub
    FROM field_session
    WHERE flag_state IS NOT NULL
    WINDOW w AS (PARTITION BY source ORDER BY time)
),
starts AS (
    SELECT time AS started_at, source, flag_state, sub_status
    FROM changes
    WHERE previous_flag IS DISTINCT FROM flag_state OR previous_sub IS DISTINCT FROM sub_status
)
SELECT source, flag_state, sub_status, started_at,
       lead(started_at) OVER (PARTITION BY source ORDER BY started_at) AS ended_at,
       extract(epoch FROM coalesce(
           lead(started_at) OVER (PARTITION BY source ORDER BY started_at), now()
       ) - started_at) AS duration_s
FROM starts;

-- Gap to us, per car, at each of our laps: joined on the car number the race
-- plan gives for the open session. Positive gap_to_us_s means the car is
-- behind us on the road; laps_to_us likewise.
CREATE VIEW v_field_gaps AS
WITH ours AS (
    SELECT sa.session_id, rp.car_number
    FROM v_session_active sa
    JOIN v_race_plan rp ON rp.session_id = sa.session_id
    WHERE rp.car_number IS NOT NULL
),
our_laps AS (
    SELECT l.time, l.source, o.session_id, l.car_number AS our_car, l.lap_number,
           l.gap_lead_s AS our_gap_lead_s, l.laps AS our_laps
    FROM (
        SELECT fl.*, fc.laps
        FROM field_laps fl
        LEFT JOIN LATERAL (
            SELECT laps FROM field_cars c
            WHERE c.source = fl.source AND c.car_number = fl.car_number AND c.time <= fl.time
            ORDER BY c.time DESC LIMIT 1
        ) fc ON true
    ) l
    JOIN ours o ON o.car_number = l.car_number
)
SELECT ol.time, ol.source, ol.session_id, ol.our_car, ol.lap_number,
       other.car_number, other.position, other.class, other.laps, other.gap_lead_s,
       other.gap_lead_s - ol.our_gap_lead_s AS gap_to_us_s,
       ol.our_laps - other.laps AS laps_to_us
FROM our_laps ol
JOIN LATERAL (
    SELECT DISTINCT ON (c.car_number) c.car_number, c.position, c.class, c.laps, c.gap_lead_s
    FROM field_cars c
    WHERE c.source = ol.source AND c.car_number <> ol.our_car AND c.time <= ol.time
      AND c.time >= ol.time - INTERVAL '30 minutes'
    ORDER BY c.car_number, c.time DESC
) other ON true;

DO $role$
DECLARE
    role_name TEXT := current_setting('openlaps.grafana_db_user', true);
BEGIN
    IF role_name IS NULL OR role_name = '' THEN
        RAISE EXCEPTION 'GRAFANA_DB_USER must be set while applying migration 011';
    END IF;
    EXECUTE format(
        'GRANT SELECT ON v_field_standings, v_field_laps, v_field_passings, v_field_flags, '
        'v_field_gaps TO %I',
        role_name
    );
END
$role$;
