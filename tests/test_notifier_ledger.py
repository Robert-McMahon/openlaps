"""Migration 006 and the alert ledger against a real TimescaleDB."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from pit.db.migrate import apply_migrations
from pit.notifier.alerts import AlertBook, parse_notification
from pit.notifier.ledger import AlertLedger

T0 = datetime(2026, 9, 16, 3, 14, 0, tzinfo=UTC)


def _payload(status: str = "firing") -> dict:
    return {
        "alerts": [
            {
                "status": status,
                "labels": {
                    "alertname": "Oil pressure low against RPM",
                    "severity": "critical",
                    "__alert_rule_uid__": "oil-pressure-low",
                },
                "annotations": {"summary": "Oil pressure low against RPM"},
                "startsAt": "2026-09-16T03:13:50Z",
                "fingerprint": "abc123",
            }
        ]
    }


def test_the_ledger_round_trips_events_and_acks_and_grafana_reads_only_the_view(
    timescale_dsn, monkeypatch
):
    monkeypatch.setenv("GRAFANA_DB_USER", "grafana_ro")
    monkeypatch.setenv("GRAFANA_DB_PASSWORD", "ro-secret")
    with psycopg.connect(timescale_dsn, autocommit=True) as conn:
        applied = apply_migrations(conn)
    assert "006_alert_ledger.sql" in applied

    ledger = AlertLedger(timescale_dsn)
    book = AlertBook(heartbeat_rule="notifier-heartbeat", heartbeat_expected_s=60)
    events = book.receive(parse_notification(_payload(), T0), T0)
    assert ledger.record(events) == 1
    ack = book.ack("abc123", "Rob", "sump checked", T0 + timedelta(seconds=30))
    assert ack is not None and ledger.record([ack]) == 1
    resolved = book.receive(parse_notification(_payload("resolved"), T0), T0 + timedelta(minutes=2))
    assert ledger.record(resolved) == 1
    assert ledger.record([]) == 0
    assert ledger.written == 3 and ledger.errors == 0
    ledger.close()

    with psycopg.connect(timescale_dsn) as conn:
        rows = conn.execute(
            "SELECT status, severity, rule_uid, acked_by, note FROM v_alert_events ORDER BY time"
        ).fetchall()
    assert rows == [
        ("firing", "critical", "oil-pressure-low", "Rob", "sump checked"),
        ("resolved", "critical", "oil-pressure-low", "Rob", "sump checked"),
    ]

    parts = dict(psycopg.conninfo.conninfo_to_dict(timescale_dsn))
    parts.update(user="grafana_ro", password="ro-secret")
    with psycopg.connect(make_conninfo(**parts)) as conn:
        assert conn.execute("SELECT count(*) FROM v_alert_events").fetchone()[0] == 2
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM alert_events")


def test_a_ledger_write_failure_is_counted_and_does_not_raise():
    ledger = AlertLedger("host=127.0.0.1 port=1 dbname=x user=x connect_timeout=1")
    book = AlertBook(heartbeat_rule="notifier-heartbeat", heartbeat_expected_s=60)
    events = book.receive(parse_notification(_payload(), T0), T0)
    assert ledger.record(events) == 0
    assert ledger.errors == 1 and not ledger.connected
