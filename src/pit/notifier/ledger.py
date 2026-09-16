"""The alert ledger: every firing, resolution and acknowledgement, in the archive.

Same shape as the pit-monitor's store (sync psycopg, one transaction per
write, reconnect on failure). The ledger is the part of the notifier that
must never take the annunciator down with it: a write failure is counted,
logged and shown on /health, and the alert still reaches the page.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime

import psycopg

from pit.notifier.alerts import Event

logger = logging.getLogger(__name__)

_INSERT_EVENT = """
INSERT INTO alert_events
    (time, rule_uid, alertname, status, severity, labels, annotations, fingerprint, started_at)
VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
"""

_INSERT_ACK = """
INSERT INTO alert_acks (fingerprint, started_at, acked_at, acked_by, note)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (fingerprint, started_at) DO UPDATE
SET acked_at = EXCLUDED.acked_at, acked_by = EXCLUDED.acked_by, note = EXCLUDED.note
"""


class AlertLedger:
    """Owns one connection to the pit database and the two inserts that use it."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: psycopg.Connection | None = None
        self.errors = 0
        self.written = 0

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._conn.closed

    def record(self, events: Sequence[Event]) -> int:
        """Persist the firing/resolved/ack events of one notification."""
        rows = [
            (
                event.at,
                event.alert.rule_uid,
                event.alert.name,
                event.alert.status,
                event.alert.severity,
                json.dumps(event.alert.labels),
                json.dumps(event.alert.annotations),
                event.alert.fingerprint,
                event.alert.started_at,
            )
            for event in events
            if event.kind in ("firing", "resolved")
        ]
        acks = [
            (event.alert.fingerprint, event.alert.started_at, event.at, event.by, event.note)
            for event in events
            if event.kind == "ack"
        ]
        if not rows and not acks:
            return 0
        try:
            conn = self._connection()
            with conn.transaction():
                cursor = conn.cursor()
                if rows:
                    cursor.executemany(_INSERT_EVENT, rows)
                if acks:
                    cursor.executemany(_INSERT_ACK, acks)
        except psycopg.Error as exc:
            self.errors += 1
            self.close()
            logger.error("notifier: ledger write failed (%s); alert still annunciated", exc)
            return 0
        self.written += len(rows) + len(acks)
        return len(rows) + len(acks)

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - closing an already-broken connection
            pass

    def _connection(self) -> psycopg.Connection:
        if self._conn is not None and not self._conn.closed:
            return self._conn
        self._conn = psycopg.connect(self._dsn, autocommit=True, connect_timeout=5)
        return self._conn


def utc_now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)
