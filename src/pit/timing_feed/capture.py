"""The capture file: every raw document a live source delivered, with its time.

One JSON object per line, ``{"t": <unix seconds>, "doc": "<xml>"}``. Plain
enough to read with ``jq``, append-only so a crash mid-session loses one
line at most, and exactly what ``ReplaySource`` reads back -- so a live
session leaves behind its own replay fixture without a separate tool.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class CaptureWriter:
    """Appends documents to one file; opened lazily, flushed per document."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._file = None
        self.written = 0

    def write(self, at: datetime, document: str) -> None:
        """Append one document; an I/O failure is logged once and disables capture."""
        if self._file is None:
            if self.written < 0:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._file = self.path.open("a", encoding="utf-8")
            except OSError as exc:
                logger.error("capture: cannot open %s: %s -- capture disabled", self.path, exc)
                self.written = -1
                return
        line = json.dumps({"t": at.timestamp(), "doc": document}, separators=(",", ":"))
        try:
            self._file.write(line + "\n")
            self._file.flush()
        except OSError as exc:
            logger.error("capture: write to %s failed: %s -- capture disabled", self.path, exc)
            self.close()
            self.written = -1
            return
        self.written += 1

    def close(self) -> None:
        """Close the file if open; safe to call repeatedly."""
        handle, self._file = self._file, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


def read_capture(path: str | Path) -> Iterator[tuple[datetime, str]]:
    """Every ``(arrival time, document)`` in the file, in order; bad lines are skipped."""
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                at = datetime.fromtimestamp(float(record["t"]), tz=UTC)
                document = record["doc"]
                if not isinstance(document, str):
                    raise TypeError("doc is not a string")
            except (ValueError, TypeError, KeyError) as exc:
                logger.warning("capture: %s line %d skipped: %s", path, number, exc)
                continue
            yield at, document


def capture_path(directory: str | Path, source: str, at: datetime) -> Path:
    """Where a live session's capture goes: one file per service start."""
    stamp = at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(directory) / f"{source}-{stamp}.jsonl"
