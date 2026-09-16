-- 008_session_active: is a session open right now? (P7.1 follow-up)
--
-- Every car-channel alert rule is gated on the last lap.event's pit_status,
-- and a car that ended its day on track leaves that at "track" for good. At
-- the notifier's first deployment (2026-09-16) live-feed-stale fired against
-- a car parked for thirty hours, and once phones were wired it would have
-- paged the crew every five minutes all night. The generated rules
-- (tools/gen_alert_rules.py) now additionally ask this view, so the
-- operator ending the session on the session UI is what ends the alerts.
--
-- Numbered 008 because 007 is the watch service's migration on a parallel
-- branch; the two are independent and apply in filename order.
--
-- A view rather than a grant on `sessions`: the rules run as the Grafana
-- read-only role, which holds SELECT on views only (docs/PIT_SCHEMA.md).

CREATE VIEW v_session_active AS
SELECT
    se.vehicle_id,
    se.session_id,
    se.session_type,
    se.track_name,
    se.started
FROM sessions se
WHERE se.status = 'active';

DO $role$
DECLARE
    role_name TEXT := current_setting('openlaps.grafana_db_user', true);
BEGIN
    IF role_name IS NULL OR role_name = '' THEN
        RAISE EXCEPTION 'GRAFANA_DB_USER must be set while applying migration 008';
    END IF;
    EXECUTE format('GRANT SELECT ON v_session_active TO %I', role_name);
END
$role$;
