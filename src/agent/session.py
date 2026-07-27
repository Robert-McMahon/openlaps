"""Session identity cache with last-known state persisted to disk.

``docs/AGENT_DESIGN.md`` -> Timing engine integration: session identity
arrives on ``cmd.<vehicle>.session`` (last-value semantics on the CMD
stream) and is cached with last-known state persisted to disk, so a vehicle
agent that restarts while the pit is unreachable still stamps laps with the
session that was active when it went down.

The payload is a small JSON object owned by the pit's session-control
service; the agent treats it as opaque apart from the identity keys the
timing wrapper stamps onto derived channels (``session_id``, ``driver``,
``stint_number``, ``track_name``, ...). Unknown keys are preserved.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class SessionStore:
    """Thread-safe holder for the current session payload, persisted as JSON."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._current: dict[str, object] = {}
        self.malformed_updates = 0
        self._load()

    def current(self) -> dict[str, object]:
        """Return a copy of the last-known session state ({} if none yet)."""
        with self._lock:
            return dict(self._current)

    def update_from_bytes(self, payload: bytes) -> dict[str, object] | None:
        """Apply one ``cmd.<vehicle>.session`` payload; None if malformed.

        A malformed command must never take the agent down — it is counted,
        logged, and the previous session state stays in force.
        """
        try:
            decoded = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.malformed_updates += 1
            logger.warning("session: discarding malformed session payload: %s", exc)
            return None
        if not isinstance(decoded, dict):
            self.malformed_updates += 1
            logger.warning("session: discarding non-object session payload")
            return None
        self.update(decoded)
        return decoded

    def update(self, state: dict[str, object]) -> None:
        """Replace the current session state and persist it."""
        with self._lock:
            self._current = dict(state)
            self._persist_locked()

    def _load(self) -> None:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("session: cannot read %s: %s", self._path, exc)
            return
        try:
            decoded = json.loads(text)
            if not isinstance(decoded, dict):
                raise TypeError("session state must be a JSON object")
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("session: ignoring corrupt state file %s: %s", self._path, exc)
            return
        self._current = decoded

    def _persist_locked(self) -> None:
        payload = json.dumps(self._current, indent=2, sort_keys=True)
        temporary_name: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                delete=False,
            ) as temporary:
                temporary.write(payload + "\n")
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_name = temporary.name
            os.replace(temporary_name, self._path)
        except OSError as exc:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
            # Persistence is best-effort: losing it costs restart continuity,
            # not live correctness, so it must never interrupt the pipeline.
            logger.warning("session: cannot persist state to %s: %s", self._path, exc)
