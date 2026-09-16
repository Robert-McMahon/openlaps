-- 006_alert_ledger: what fired, when, and who acknowledged it (P7.2, ADR 0011).
--
-- Grafana holds alert state, but its history lives in Grafana's own database
-- and is keyed to its rule definitions; an alert that fired at 03:14 is worth
-- more sitting next to the samples it fired on. The notifier -- Grafana's only
-- contact point -- records every firing and resolution here, and every
-- acknowledgement a person makes on the annunciator.
--
-- Written directly by the notifier in the pit-monitor pattern (ADR 0011
-- decision 2): no registry, never crosses the radio, one transaction per
-- notification. Plain tables rather than hypertables: a busy race produces
-- hundreds of rows, not millions.

CREATE TABLE alert_events (
    time        TIMESTAMPTZ NOT NULL,
    -- Grafana's rule uid (the alarm key in profiles/<car>/alarms.yaml), from
    -- the __alert_rule_uid__ label. NULL for a synthetic test alert.
    rule_uid    TEXT        NULL,
    alertname   TEXT        NOT NULL,
    status      TEXT        NOT NULL CHECK (status IN ('firing', 'resolved')),
    severity    TEXT        NOT NULL CHECK (severity IN ('none', 'warning', 'critical')),
    labels      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    annotations JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- Grafana's identity for one alert instance; the same rule firing twice
    -- has the same fingerprint, which is why started_at is carried too.
    fingerprint TEXT        NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX alert_events_time_idx ON alert_events (time DESC);
CREATE INDEX alert_events_fingerprint_idx ON alert_events (fingerprint, started_at);

-- One acknowledgement per firing. Re-acknowledging the same firing updates
-- the row rather than adding one; a later firing of the same rule is a new
-- (fingerprint, started_at) and needs its own acknowledgement.
CREATE TABLE alert_acks (
    fingerprint TEXT        NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL,
    acked_at    TIMESTAMPTZ NOT NULL,
    acked_by    TEXT        NOT NULL,
    note        TEXT        NULL,
    PRIMARY KEY (fingerprint, started_at)
);

-- The stable read surface (docs/PIT_SCHEMA.md). One row per event, with the
-- acknowledgement for that firing joined on, so "what fired last night and
-- who saw it" is one query.
CREATE VIEW v_alert_events AS
SELECT
    e.time,
    e.rule_uid,
    e.alertname,
    e.status,
    e.severity,
    e.fingerprint,
    e.started_at,
    a.acked_at,
    a.acked_by,
    a.note,
    e.labels,
    e.annotations
FROM alert_events e
LEFT JOIN alert_acks a ON a.fingerprint = e.fingerprint AND a.started_at = e.started_at;

DO $role$
DECLARE
    role_name TEXT := current_setting('openlaps.grafana_db_user', true);
BEGIN
    IF role_name IS NULL OR role_name = '' THEN
        RAISE EXCEPTION 'GRAFANA_DB_USER must be set while applying migration 006';
    END IF;
    EXECUTE format('GRANT SELECT ON v_alert_events TO %I', role_name);
END
$role$;
