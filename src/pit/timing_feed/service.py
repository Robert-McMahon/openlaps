"""The timing feed service: the other cars, into the pit database (P7.10).

One source at a time -- the Natsoft feed (public or the timekeepers' local
one; same client, different address), or a capture file replayed -- plus
two ingest endpoints for the shapes other providers arrive in: a standings
snapshot posted by the browser relay, and Timing71's standalone WebSocket
messages. Every document becomes the rows it changed in ``field_session``,
``field_cars``, ``field_laps`` and ``field_passings``, written in one
transaction; every live document is also appended to a capture file so the
session leaves a replayable fixture behind.

Alongside, the service reconciles our own car: the feed's lap count for
the race plan's car number against ``v_laps``, as a ``field.lap_count``
finding when they disagree by more than a lap, and the timekeepers' stamp
on our main-line passings against our own crossings, as a clock offset in
``pit_metrics``.

Database work is synchronous psycopg on a worker thread behind one lock;
the source and the ingest queue run on the event loop, as the strategy
service does.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pit.db.dsn import dsn_from_env
from pit.timing_feed.capture import CaptureWriter, capture_path
from pit.timing_feed.database import FieldDatabase
from pit.timing_feed.health import HealthState
from pit.timing_feed.ingest import serve_ingest
from pit.timing_feed.model import Batch, DocumentError, FieldState, PassingRow, Snapshot
from pit.timing_feed.reconcile import Finding, clock_offset_s, lap_count_finding
from pit.timing_feed.sources import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    Document,
    NatsoftSource,
    ReplaySource,
)

logger = logging.getLogger(__name__)

_REPORT_INTERVAL_S = 60.0
_DB_RETRY_S = 5.0
_QUEUE_SIZE = 256
SOURCE_KINDS = ("natsoft", "replay", "none")


@dataclass(frozen=True, slots=True)
class TimingFeedSettings:
    """Deploy-time wiring; see ``example.env``."""

    dsn: str
    vehicle_id: str
    source_kind: str = "natsoft"
    feed_host: str = DEFAULT_HOST
    feed_port: int = DEFAULT_PORT
    replay_file: str | None = None
    replay_paced: bool = True
    replay_speed: float = 1.0
    capture_dir: str | None = None
    http_port: int = 8089
    http_host: str = ""
    reconcile_s: float = 10.0
    lap_tolerance: int = 1
    connect_timeout_s: float = 10.0
    idle_timeout_s: float = 120.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TimingFeedSettings:
        """Build settings from the environment; a bad value is a startup error."""
        env = os.environ if env is None else env
        vehicle = env.get("OPENLAPS_VEHICLE_ID", "").strip()
        if not vehicle:
            raise ValueError("OPENLAPS_VEHICLE_ID must name the car the pit watches")
        kind = (env.get("OPENLAPS_TIMING_FEED_SOURCE", "natsoft").strip() or "natsoft").lower()
        if kind not in SOURCE_KINDS:
            choices = ", ".join(SOURCE_KINDS)
            raise ValueError(f"OPENLAPS_TIMING_FEED_SOURCE must be one of {choices}")

        def number(name: str, default: str, *, positive: bool = True) -> float:
            try:
                value = float(env.get(name, default).strip() or default)
            except ValueError:
                raise ValueError(f"{name} must be a number") from None
            if positive and value <= 0:
                raise ValueError(f"{name} must be positive")
            return value

        feed_port = int(number("OPENLAPS_TIMING_FEED_HOST_PORT", str(DEFAULT_PORT)))
        http_port = int(number("OPENLAPS_TIMING_FEED_PORT", "8089", positive=False))
        if not 1 <= feed_port <= 65535:
            raise ValueError("OPENLAPS_TIMING_FEED_HOST_PORT must be between 1 and 65535")
        if not 0 <= http_port <= 65535:
            raise ValueError("OPENLAPS_TIMING_FEED_PORT must be between 0 and 65535")
        replay = env.get("OPENLAPS_TIMING_FEED_REPLAY_FILE", "").strip() or None
        if kind == "replay" and not replay:
            raise ValueError("OPENLAPS_TIMING_FEED_REPLAY_FILE is required for a replay source")
        paced = (env.get("OPENLAPS_TIMING_FEED_REPLAY_PACED", "1").strip() or "1").lower()
        return cls(
            dsn=dsn_from_env(env),
            vehicle_id=vehicle,
            source_kind=kind,
            feed_host=env.get("OPENLAPS_TIMING_FEED_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST,
            feed_port=feed_port,
            replay_file=replay,
            replay_paced=paced not in ("0", "false", "no"),
            replay_speed=number("OPENLAPS_TIMING_FEED_REPLAY_SPEED", "1"),
            capture_dir=env.get("OPENLAPS_TIMING_FEED_CAPTURE_DIR", "").strip() or None,
            http_port=http_port,
            http_host=env.get("OPENLAPS_TIMING_FEED_HTTP_HOST", "").strip(),
            reconcile_s=number("OPENLAPS_TIMING_FEED_RECONCILE_S", "10"),
            lap_tolerance=int(number("OPENLAPS_TIMING_FEED_LAP_TOLERANCE", "1", positive=False)),
            connect_timeout_s=number("OPENLAPS_TIMING_FEED_CONNECT_TIMEOUT_S", "10"),
            idle_timeout_s=number("OPENLAPS_TIMING_FEED_IDLE_TIMEOUT_S", "120"),
        )


class TimingFeedService:
    """Consumes one source and the ingest queue; writes; reconciles our car."""

    def __init__(
        self,
        settings: TimingFeedSettings,
        *,
        database: FieldDatabase | None = None,
        source=None,
        clock=None,
    ) -> None:
        """Build the service; ``database``, ``source`` and ``clock`` are injectable."""
        self.settings = settings
        self.health = HealthState()
        self.health.source = settings.source_kind
        self._db = (
            FieldDatabase(settings.dsn, settings.vehicle_id) if database is None else database
        )
        self._source = source
        self._clock = clock or (lambda: datetime.now(UTC))
        self.state = FieldState(settings.source_kind if settings.source_kind != "none" else "relay")
        self._ingest: asyncio.Queue[Snapshot] | None = None
        self._db_lock = asyncio.Lock()
        self._capture: CaptureWriter | None = None
        self._last_report = 0.0
        self._adopted = False
        self._our_passings: list[PassingRow] = []
        self.source_finished = asyncio.Event()

    # -- the sink the ingest server calls on the loop thread

    def submit(self, snapshot: Snapshot) -> None:
        """Queue a snapshot from an ingest endpoint; the newest wins when full."""
        if self._ingest is None:
            return
        if self._ingest.full():
            self._ingest.get_nowait()
            self.health.batches_dropped += 1
        self._ingest.put_nowait(snapshot)

    # -- documents

    async def handle_document(self, document: Document, *, capture: bool) -> Batch | None:
        """Apply one document, capture it, write what changed."""
        self.health.observe_document()
        if capture and self._capture is not None:
            self._capture.write(document.at, document.text)
            self.health.capture_written = self._capture.written
        try:
            batch = self.state.apply_document(document.text, document.at)
        except DocumentError as exc:
            self.health.documents_rejected += 1
            logger.warning("timing-feed: document rejected: %s", str(exc)[:200])
            return None
        self._note_passings(batch)
        await self._write(batch)
        return batch

    async def handle_snapshot(self, snapshot: Snapshot) -> Batch | None:
        self.health.observe_document()
        try:
            batch = self.state.apply_snapshot(snapshot)
        except DocumentError as exc:
            self.health.documents_rejected += 1
            logger.warning("timing-feed: snapshot rejected: %s", str(exc)[:200])
            return None
        await self._write(batch)
        return batch

    async def _write(self, batch: Batch) -> None:
        if not batch:
            return
        async with self._db_lock:
            try:
                rows = await asyncio.to_thread(self._db.write, batch)
            except Exception as exc:  # noqa: BLE001 - every database failure: count, redial later
                self.health.observe_db_error(exc)
                self.health.batches_dropped += 1
                logger.warning("timing-feed: write failed, batch dropped: %s", exc)
                return
        self.health.observe_write(rows)

    def _note_passings(self, batch: Batch) -> None:
        our = self.health.our_car
        if our is None:
            return
        for passing in batch.passings:
            if passing.car_number == our and passing.line == "main" and passing.tod is not None:
                self._our_passings.append(passing)
        del self._our_passings[:-32]

    # -- reconciliation

    async def reconcile(self) -> list[Finding]:
        """One pass: our car number, the two lap counts, the clock offset."""
        async with self._db_lock:
            return await asyncio.to_thread(self._reconcile_sync)

    def _reconcile_sync(self) -> list[Finding]:
        now = self._clock()
        if not self._adopted:
            adopted = self._db.adopt_open_findings()
            if adopted:
                logger.info("timing-feed: adopted %d open finding(s) from a previous run", adopted)
            self._adopted = True
        our = self._db.our_car()
        findings: list[Finding] = []
        if our is None or not our.car_number:
            self.health.our_car = None
            self.health.session_id = None if our is None else our.session_id
            self.health.feed_laps = None
            self.health.vehicle_laps = None
        else:
            self.health.our_car = our.car_number
            self.health.session_id = our.session_id
            feed_row = self.state.cars.get(our.car_number)
            vehicle_laps, _last = self._db.vehicle_laps(our.session_id)
            self.health.feed_laps = None if feed_row is None else feed_row.laps
            self.health.vehicle_laps = vehicle_laps
            finding = lap_count_finding(
                our.car_number,
                None if feed_row is None else feed_row.laps,
                vehicle_laps,
                tolerance=self.settings.lap_tolerance,
            )
            if finding is not None:
                findings.append(finding)
            self._clock_offset(our.session_id, now)
        self._db.reconcile_findings(findings, now)
        self.health.findings_open = len(self._db.open_findings)
        for finding in findings:
            logger.info(
                "timing-feed: %s %s: %s",
                finding.severity,
                finding.monitor,
                finding.summary.get("message"),
            )
        return findings

    def _clock_offset(self, session_id: str, now: datetime) -> None:
        if not self._our_passings:
            return
        passings, self._our_passings = self._our_passings, []
        earliest = min(p.tod for p in passings if p.tod is not None)
        crossings = self._db.vehicle_crossings(session_id, earliest - timedelta(seconds=60))
        for passing in passings:
            assert passing.tod is not None
            offset = clock_offset_s(passing.tod, crossings)
            if offset is None:
                continue
            self.health.clock_offset_s = offset
            self._db.write_metric(passing.tod, "clock_offset_s", offset)
            self._db.write_metric(
                passing.time, "feed_latency_s", (passing.time - passing.tod).total_seconds()
            )

    # -- the loops

    def _build_source(self, stop: asyncio.Event):
        if self._source is not None:
            return self._source
        kind = self.settings.source_kind
        if kind == "replay":
            assert self.settings.replay_file is not None
            return ReplaySource(
                self.settings.replay_file,
                paced=self.settings.replay_paced,
                speed=self.settings.replay_speed,
            )
        if kind == "natsoft":
            return NatsoftSource(
                self.settings.feed_host,
                self.settings.feed_port,
                health=self.health,
                connect_timeout_s=self.settings.connect_timeout_s,
                idle_timeout_s=self.settings.idle_timeout_s,
                stop=stop,
            )
        return None

    async def run(self, stop: asyncio.Event) -> None:
        """Run until ``stop`` is set."""
        self._ingest = asyncio.Queue(maxsize=_QUEUE_SIZE)
        source = self._build_source(stop)
        capture = source is not None and getattr(source, "kind", "") == "natsoft"
        if capture and self.settings.capture_dir:
            path = capture_path(self.settings.capture_dir, "natsoft", self._clock())
            self._capture = CaptureWriter(path)
            self.health.capture_path = str(path)
        logger.info(
            "timing-feed: starting for %s, source=%s%s",
            self.settings.vehicle_id,
            self.settings.source_kind,
            f" ({self.settings.feed_host}:{self.settings.feed_port})"
            if self.settings.source_kind == "natsoft"
            else "",
        )
        tasks = [
            asyncio.create_task(self._ingest_loop(stop), name="ingest"),
            asyncio.create_task(self._reconcile_loop(stop), name="reconcile"),
        ]
        if source is not None:
            tasks.append(
                asyncio.create_task(self._source_loop(source, stop, capture), name="source")
            )
        else:
            self.source_finished.set()
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._capture is not None:
                self._capture.close()
            await asyncio.to_thread(self._db.close)
            logger.info("timing-feed: stopped")

    async def _source_loop(self, source, stop: asyncio.Event, capture: bool) -> None:
        try:
            async for document in source:
                if stop.is_set():
                    break
                await self.handle_document(document, capture=capture)
                self._report()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a source that dies is logged, not fatal
            logger.exception("timing-feed: source failed")
        finally:
            self.health.source_connected = False
            self.source_finished.set()
            if getattr(source, "kind", "") == "replay":
                logger.info(
                    "timing-feed: replay finished after %d documents", self.health.documents
                )

    async def _ingest_loop(self, stop: asyncio.Event) -> None:
        assert self._ingest is not None
        while not stop.is_set():
            snapshot = await self._ingest.get()
            await self.handle_snapshot(snapshot)
            self._report()

    async def _reconcile_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            delay = self.settings.reconcile_s
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - count, redial later
                self.health.observe_db_error(exc)
                logger.warning("timing-feed: reconciliation failed: %s", exc)
                delay = _DB_RETRY_S
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    def _report(self) -> None:
        now = time.monotonic()
        if now - self._last_report < _REPORT_INTERVAL_S:
            return
        self._last_report = now
        logger.info("timing-feed: %s", self.health.log_line())


async def run_service(settings: TimingFeedSettings, stop: asyncio.Event) -> None:
    """Run one timing feed service with its HTTP surface until ``stop`` is set."""
    service = TimingFeedService(settings)
    server = serve_ingest(
        asyncio.get_running_loop(),
        service.submit,
        service.health,
        port=settings.http_port,
        host=settings.http_host,
    )
    try:
        await service.run(stop)
    finally:
        server.shutdown()
        server.server_close()
