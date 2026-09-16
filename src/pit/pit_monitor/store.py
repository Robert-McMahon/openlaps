"""The pit-monitor's one statement: append a poll's readings to `pit_metrics`.

Sync psycopg, not the async connection the ingest-writer uses, because this
service has no concurrency to arrange -- one poll every few seconds, one
INSERT, no consumer to ack. A poll is one transaction: a partial poll in the
table would read as a metric that stopped reporting.

The connection is reopened on failure rather than held open and hoped for.
A pit-monitor that keeps polling while its database connection is dead is
the failure this service exists to make visible, so it must not be one of
its own blind spots.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime

import psycopg

logger = logging.getLogger(__name__)

# (source, metric, time, value, value_text)
MetricRow = tuple[str, str, datetime, float | None, str | None]

_INSERT = """
INSERT INTO pit_metrics (source, metric, time, value, value_text)
VALUES (%s, %s, %s, %s, %s)
"""


def to_rows(readings: Sequence[tuple[str, str, float | str]], stamp: datetime) -> list[MetricRow]:
    """Stamp readings and split each value into its numeric or text column."""
    rows: list[MetricRow] = []
    for source, metric, value in readings:
        if isinstance(value, str):
            rows.append((source, metric, stamp, None, value))
        else:
            rows.append((source, metric, stamp, float(value), None))
    return rows


class PitMetricStore:
    """Owns one connection to the pit database and the insert that uses it."""

    def __init__(self, dsn: str) -> None:
        """Build a store over ``dsn``; no connection is opened until needed."""
        self._dsn = dsn
        self._conn: psycopg.Connection | None = None

    def write(self, rows: Sequence[MetricRow]) -> int:
        """Insert one poll's rows in a single transaction; returns the count."""
        if not rows:
            return 0
        conn = self._connection()
        try:
            with conn.transaction():
                conn.cursor().executemany(_INSERT, rows)
        except psycopg.Error:
            # Drop the connection rather than reuse one whose session state
            # after an error is anyone's guess; the next poll redials.
            self.close()
            raise
        return len(rows)

    def close(self) -> None:
        """Close the connection if one is open; safe to call repeatedly."""
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
        self._conn = psycopg.connect(self._dsn, autocommit=True)
        return self._conn
