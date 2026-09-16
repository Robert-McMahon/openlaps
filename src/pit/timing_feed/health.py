"""Pit-local health state for the timing feed, served by the ingest server.

The number that matters is ``last_document_age_s``: a race dashboard that
stops moving means either the session ended or this service lost its
source, and ``source_connected`` beside it says which. ``feed_laps`` and
``vehicle_laps`` are the reconciliation at a glance.
"""

from __future__ import annotations

import time


class HealthState:
    """Counters the loops update and the health endpoint reports."""

    def __init__(self) -> None:
        self.source = "none"
        self.source_connected = False
        self.source_reconnects = 0
        self.documents = 0
        self.documents_rejected = 0
        self.rows_written = 0
        self.batches_dropped = 0
        self.db_errors = 0
        self.ingest_snapshots = 0
        self.ingest_t71_messages = 0
        self.ingest_rejected = 0
        self.capture_path: str | None = None
        self.capture_written = 0
        self.our_car: str | None = None
        self.session_id: str | None = None
        self.feed_laps: int | None = None
        self.vehicle_laps: int | None = None
        self.clock_offset_s: float | None = None
        self.findings_open = 0
        self.last_error: str | None = None
        self._last_document_mono: float | None = None
        self._started = time.monotonic()

    def observe_document(self) -> None:
        self.documents += 1
        self._last_document_mono = time.monotonic()

    def observe_write(self, rows: int) -> None:
        self.rows_written += rows

    def observe_db_error(self, exc: Exception) -> None:
        self.db_errors += 1
        self.last_error = f"database: {exc}"

    @property
    def last_document_age_s(self) -> float | None:
        if self._last_document_mono is None:
            return None
        return round(time.monotonic() - self._last_document_mono, 1)

    def snapshot(self) -> dict[str, object]:
        """Everything `/health` reports."""
        return {
            "uptime_s": round(time.monotonic() - self._started, 1),
            "source": self.source,
            "source_connected": self.source_connected,
            "source_reconnects": self.source_reconnects,
            "documents": self.documents,
            "documents_rejected": self.documents_rejected,
            "last_document_age_s": self.last_document_age_s,
            "rows_written": self.rows_written,
            "batches_dropped": self.batches_dropped,
            "db_errors": self.db_errors,
            "ingest_snapshots": self.ingest_snapshots,
            "ingest_t71_messages": self.ingest_t71_messages,
            "ingest_rejected": self.ingest_rejected,
            "capture_path": self.capture_path,
            "capture_written": self.capture_written,
            "our_car": self.our_car,
            "session_id": self.session_id,
            "feed_laps": self.feed_laps,
            "vehicle_laps": self.vehicle_laps,
            "clock_offset_s": self.clock_offset_s,
            "findings_open": self.findings_open,
            "last_error": self.last_error,
        }

    def log_line(self) -> str:
        return (
            f"source={self.source} connected={self.source_connected} "
            f"documents={self.documents} rows={self.rows_written} "
            f"db_errors={self.db_errors} snapshots={self.ingest_snapshots} "
            f"t71={self.ingest_t71_messages} our_car={self.our_car} "
            f"feed_laps={self.feed_laps} vehicle_laps={self.vehicle_laps} "
            f"last_document_age={self.last_document_age_s}s"
        )
