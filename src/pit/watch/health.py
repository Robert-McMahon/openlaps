"""Small health endpoint with explicit data and dependency readiness."""

import time

from pit.timing_extrapolator.health import serve_health  # shared HTTP JSON adapter


class Health:
    def __init__(self) -> None:
        self.nats_connected = False
        self.mqtt_connected = False
        self.database_ok = False
        self.last_tick = None
        self.status = {}
        self.malformed = 0
        self.dropped = 0
        self.unknown_registry = 0
        self.bad_version = 0
        # Batches older than the live window, skipped before any decode: a
        # source catch-up after a pit outage arrives as "new" messages.
        self.backfill_skipped = 0
        # Messages the NATS client discarded because this service fell
        # behind. Counted, not logged per message: see WatchService._nats_error.
        self.slow_consumer_drops = 0

    def snapshot(self) -> dict:
        return dict(
            healthy=self.nats_connected
            and self.database_ok
            and self.last_tick is not None
            and time.time() - self.last_tick < 10,
            nats_connected=self.nats_connected,
            mqtt_connected=self.mqtt_connected,
            database_ok=self.database_ok,
            last_tick=self.last_tick,
            monitors=self.status,
            malformed=self.malformed,
            dropped=self.dropped,
            unknown_registry=self.unknown_registry,
            bad_version=self.bad_version,
            backfill_skipped=self.backfill_skipped,
            slow_consumer_drops=self.slow_consumer_drops,
        )


__all__ = ["Health", "serve_health"]
