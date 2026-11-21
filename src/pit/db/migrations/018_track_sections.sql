-- Named sections of a lap (corners and the straights between them), for the
-- driver dashboard's per-corner comparison. The car's timing knows only
-- lines and distance; where "T6" is comes from whoever drew the track map.
--
-- Loaded by tools/track_sections.py from the `sections` list in a track's
-- JSON sidecar (profiles/<profile>/tracks/<Track>.json), which that tool can
-- fill from a MoTeC i2 track file. track_name is the KML stem, the same name
-- laps carry.
--
-- Distances are metres along the lap as the track map measured it, with that
-- map's own lap length alongside: the car's lap distance runs a little short
-- or long of any map (2,531 m against 2,553 m at Collie), so readers compare
-- fractions of a lap, not metres. A section that spans the start line has
-- end_m below start_m.
CREATE TABLE track_sections (
    track_name   TEXT             NOT NULL,
    seq          INT              NOT NULL,
    name         TEXT             NOT NULL,
    kind         TEXT             NOT NULL CHECK (kind IN ('corner', 'straight')),
    start_m      DOUBLE PRECISION NOT NULL CHECK (start_m >= 0),
    end_m        DOUBLE PRECISION NOT NULL CHECK (end_m >= 0),
    lap_length_m DOUBLE PRECISION NOT NULL CHECK (lap_length_m > 0),
    PRIMARY KEY (track_name, seq)
);

CREATE VIEW v_track_sections AS
SELECT
    track_name,
    seq,
    name,
    kind,
    start_m,
    end_m,
    lap_length_m,
    start_m / lap_length_m AS start_fraction,
    end_m / lap_length_m AS end_fraction
FROM track_sections;

DO $role$
DECLARE
    role_name TEXT := current_setting('openlaps.grafana_db_user', true);
BEGIN
    IF role_name IS NULL OR role_name = '' THEN
        RAISE EXCEPTION 'GRAFANA_DB_USER must be set while applying migration 018';
    END IF;
    EXECUTE format('GRANT SELECT ON v_track_sections TO %I', role_name);
END
$role$;
