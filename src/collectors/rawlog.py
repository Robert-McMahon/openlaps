"""Segmented, fsync-bounded raw capture files (``docs/RAW_CAPTURE.md``).

One :class:`RawLogWriter` owns one capture *run*: a directory named for the
moment the run started, holding a ``manifest.json`` and numbered segment
files. Two durability rules bound what an abrupt power cut can cost:

- the current segment is flushed and fsync'd at least every
  ``fsync_interval_s``, so the loss window is seconds of tail, not the file;
- segments rotate every ``rotate_interval_s``, and a rotated-out segment
  (and the directory entry naming it) is fsync'd on close, so anything
  older than the current segment is durable outright.

Writers are single-threaded: exactly one thread calls :meth:`write_line`
and :meth:`close`. Write failures (disk full, permissions) raise ``OSError``
to the caller — capture policy on failure belongs to the capturing service,
not here.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger(__name__)

DEFAULT_ROTATE_INTERVAL_S = 600.0
DEFAULT_FSYNC_INTERVAL_S = 1.0


class RawLogWriter:
    """Append raw capture lines to fsync-bounded, time-rotated segments."""

    def __init__(
        self,
        base_dir: str | Path,
        source: str,
        manifest: dict[str, object] | None = None,
        *,
        rotate_interval_s: float = DEFAULT_ROTATE_INTERVAL_S,
        fsync_interval_s: float = DEFAULT_FSYNC_INTERVAL_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rotate_interval_s = rotate_interval_s
        self._fsync_interval_s = fsync_interval_s
        self._monotonic = monotonic
        self._segment: BinaryIO | None = None
        self._segment_seq = 0
        self._closed = False

        self.run_dir = _create_run_dir(Path(base_dir) / source)
        self._write_manifest(source, dict(manifest or {}))
        self._open_segment()

    def write_line(self, line: bytes) -> None:
        """Append one line (caller includes the terminator) to the run."""
        if self._closed or self._segment is None:
            raise OSError("raw log writer is closed")
        self._segment.write(line)
        now = self._monotonic()
        if now >= self._rotate_due:
            self._rotate()
        elif now >= self._fsync_due:
            self._segment.flush()
            os.fsync(self._segment.fileno())
            self._fsync_due = now + self._fsync_interval_s

    def close(self) -> None:
        """Flush, fsync, and close the current segment. Best-effort, idempotent."""
        if self._closed:
            return
        self._closed = True
        segment = self._segment
        self._segment = None
        if segment is None:
            return
        try:
            segment.flush()
            os.fsync(segment.fileno())
            segment.close()
            _fsync_dir(self.run_dir)
        except OSError:
            logger.warning("raw log: close of %s failed", self.run_dir, exc_info=True)

    def _open_segment(self) -> None:
        self._segment_seq += 1
        path = self.run_dir / f"{self._segment_seq:04d}.log"
        self._segment = path.open("wb")
        now = self._monotonic()
        self._rotate_due = now + self._rotate_interval_s
        self._fsync_due = now + self._fsync_interval_s

    def _rotate(self) -> None:
        segment = self._segment
        assert segment is not None
        segment.flush()
        os.fsync(segment.fileno())
        segment.close()
        self._segment = None
        _fsync_dir(self.run_dir)
        self._open_segment()

    def _write_manifest(self, source: str, manifest: dict[str, object]) -> None:
        manifest.setdefault("source", source)
        manifest["started_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        manifest["t_wall_ms"] = time.time() * 1000.0
        manifest["t_mono_ns"] = time.monotonic_ns()
        path = self.run_dir / "manifest.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_dir(self.run_dir)


def _create_run_dir(source_dir: Path) -> Path:
    """Create a UTC-stamped run directory, suffixing on a same-second restart."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for attempt in range(100):
        run_dir = source_dir / (stamp if attempt == 0 else f"{stamp}-{attempt}")
        try:
            run_dir.mkdir(parents=True)
        except FileExistsError:
            continue
        _fsync_dir(source_dir)
        return run_dir
    raise OSError(f"cannot create a unique run directory under {source_dir}")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
