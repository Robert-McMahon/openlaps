"""Pit-local health surface for the ingest-writer.

Pit-side ingest lag is a pit-local concern, not a telemetry channel that
crosses the radio (`docs/ARCHITECTURE.md` -> Link dropout and recovery), so
it is reported here: logged at 1 Hz and served as JSON on `/health`.

**On the lag figure.** `batch_epoch_mono_ns` is a monotonic reading from the
*vehicle's* clock and has no shared epoch with the pit's, so absolute
one-way latency is not measurable from it (`docs/WIRE_FORMAT.md` ->
Timestamp scheme). What is measurable, and what actually matters
operationally, is how much worse transit is now than the best transit
recently observed: track the minimum of `local_mono - batch_mono` over a
rolling window and report the excess over it. The figure reads ~0 when the
link is healthy and climbs the moment the pit falls behind, which is the
question an operator is asking. `wall_lag_ms` is reported alongside it as
the naive wall-clock difference, which is easier to interpret but only as
trustworthy as the vehicle's GPS-disciplined clock.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)

# How long a monotonic-offset estimate is trusted before it is re-baselined;
# without this the minimum would ratchet down forever on clock drift.
_OFFSET_WINDOW_S = 60.0


class HealthState:
    """Counters the writer updates and the health endpoint reports."""

    def __init__(self) -> None:
        self.rows_written = 0
        self.samples_dropped = 0
        self.flushes = 0
        self.batches = 0
        self.registries = 0
        self.messages_skipped = 0
        self.laps_written = 0
        self.sectors_written = 0
        self.malformed_lap_events = 0
        self.unknown_seq_batches = 0
        self.unknown_seq_stalls = 0
        self.bad_version_batches = 0
        self.unresolved_channels = 0
        self.db_errors = 0
        self.data_errors = 0
        self.dropped_flushes = 0
        self.db_connects = 0
        self.nats_reconnects = 0
        self.last_stream_seq = 0
        self.stalled = False

        self.rows_per_s = 0.0
        self.flushes_per_s = 0.0
        self.lag_ms = 0.0
        self.wall_lag_ms = 0.0

        self._agent_status_mono: float | None = None
        self._offset_ns: int | None = None
        self._offset_set_mono = 0.0
        self._window_start = time.monotonic()
        self._window_rows = 0
        self._window_flushes = 0
        self._started = time.monotonic()

    # -- writer-side updates ---------------------------------------------------

    def observe_batch(self, epoch_mono_ns: int, epoch_unix_ms: int, received_mono_ns: int) -> None:
        """Record one batch's transit, updating the lag estimate."""
        self.batches += 1
        if epoch_mono_ns:
            observed = received_mono_ns - epoch_mono_ns
            now = time.monotonic()
            stale = now - self._offset_set_mono > _OFFSET_WINDOW_S
            if self._offset_ns is None or stale or observed < self._offset_ns:
                self._offset_ns = observed
                self._offset_set_mono = now
            self.lag_ms = max(0.0, (observed - self._offset_ns) / 1e6)
        if epoch_unix_ms:
            self.wall_lag_ms = time.time() * 1000.0 - epoch_unix_ms

    def observe_flush(self, rows: int, stream_seq: int) -> None:
        """Record one committed flush."""
        self.flushes += 1
        self.rows_written += rows
        self._window_flushes += 1
        self._window_rows += rows
        self.last_stream_seq = max(self.last_stream_seq, stream_seq)

    def note_agent_status(self) -> None:
        """A `sys.agent.status` sample arrived; staleness resets."""
        self._agent_status_mono = time.monotonic()

    @property
    def agent_status_age_s(self) -> float | None:
        """Seconds since the last `sys.agent.status`, the pit's liveness signal.

        `docs/AGENT_DESIGN.md` assigns watching this to the pit: it replaces
        the predecessor's MQTT last-will, so staleness here — not a message —
        is how a dead vehicle agent announces itself.
        """
        if self._agent_status_mono is None:
            return None
        return round(time.monotonic() - self._agent_status_mono, 1)

    def roll(self) -> None:
        """Close the rate window; called once a second by the writer."""
        elapsed = time.monotonic() - self._window_start
        if elapsed <= 0:
            return
        self.rows_per_s = self._window_rows / elapsed
        self.flushes_per_s = self._window_flushes / elapsed
        self._window_start = time.monotonic()
        self._window_rows = 0
        self._window_flushes = 0

    # -- reporting -------------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        """Everything `/health` reports, and what the 1 Hz log line summarises."""
        return {
            "uptime_s": round(time.monotonic() - self._started, 1),
            "stalled": self.stalled,
            "rows_written": self.rows_written,
            "rows_per_s": round(self.rows_per_s, 1),
            "flushes": self.flushes,
            "flushes_per_s": round(self.flushes_per_s, 2),
            "batches": self.batches,
            "registries": self.registries,
            "last_stream_seq": self.last_stream_seq,
            "messages_skipped": self.messages_skipped,
            "lag_ms": round(self.lag_ms, 1),
            "wall_lag_ms": round(self.wall_lag_ms, 1),
            "agent_status_age_s": self.agent_status_age_s,
            "laps_written": self.laps_written,
            "sectors_written": self.sectors_written,
            "malformed_lap_events": self.malformed_lap_events,
            "unknown_seq_batches": self.unknown_seq_batches,
            "unknown_seq_stalls": self.unknown_seq_stalls,
            "bad_version_batches": self.bad_version_batches,
            "unresolved_channels": self.unresolved_channels,
            "samples_dropped": self.samples_dropped,
            "db_errors": self.db_errors,
            "data_errors": self.data_errors,
            "dropped_flushes": self.dropped_flushes,
            "db_connects": self.db_connects,
            "nats_reconnects": self.nats_reconnects,
        }

    def log_line(self) -> str:
        """The 1 Hz summary: the numbers worth watching scroll past."""
        return (
            f"rows/s={self.rows_per_s:.0f} flushes/s={self.flushes_per_s:.1f} "
            f"lag={self.lag_ms:.0f}ms wall_lag={self.wall_lag_ms:.0f}ms "
            f"seq={self.last_stream_seq} laps={self.laps_written} "
            f"agent_status_age={self.agent_status_age_s}s" + (" STALLED" if self.stalled else "")
        )


class _HealthHandler(BaseHTTPRequestHandler):
    """Serves the writer's snapshot; nothing else, no state of its own."""

    state: HealthState

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        if self.path.split("?")[0] != "/health":
            self.send_error(404)
            return
        payload = json.dumps(self.state.snapshot(), sort_keys=True).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        """Silence per-request stderr logging; the writer owns the log."""


def serve_health(state: HealthState, port: int, host: str = "") -> ThreadingHTTPServer:
    """Serve `/health` on a daemon thread; returns the server for shutdown."""
    handler = type("HealthHandler", (_HealthHandler,), {"state": state})
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="health", daemon=True)
    thread.start()
    logger.info("health: serving /health on port %d", server.server_port)
    return server
