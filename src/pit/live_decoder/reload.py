"""Atomic live-decoder config reload with SIGHUP and mtime triggers."""

from __future__ import annotations

import logging
from pathlib import Path

from pit.live_decoder.config import ConfigError, LiveConfig, load_live_config

logger = logging.getLogger(__name__)


class ConfigReloader:
    """Own the last-known-good config and poll its file for replacement."""

    def __init__(self, path: str | Path, poll_interval_s: float = 5.0) -> None:
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be greater than zero")
        self.path = Path(path)
        self.poll_interval_s = poll_interval_s
        self.config = load_live_config(self.path)
        self.mtime_ns = self.path.stat().st_mtime_ns
        self._next_poll = poll_interval_s
        self._forced = False

    def request_reload(self) -> None:
        """Mark a SIGHUP-triggered reload for the event loop to perform."""
        self._forced = True

    def poll(self, now: float) -> LiveConfig | None:
        """Reload when forced or changed; return a new config only on success."""
        if not self._forced and now < self._next_poll:
            return None
        forced = self._forced
        self._forced = False
        self._next_poll = now + self.poll_interval_s
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except OSError as exc:
            logger.error(
                "live-decoder: config reload failed (%s); previous config stays in force", exc
            )
            return None
        if not forced and mtime_ns == self.mtime_ns:
            return None
        try:
            loaded = load_live_config(self.path)
        except ConfigError as exc:
            logger.error(
                "live-decoder: config reload failed (%s); previous config stays in force", exc
            )
            return None
        self.config = loaded
        self.mtime_ns = mtime_ns
        logger.info("live-decoder: reloaded config from %s", self.path)
        return loaded
