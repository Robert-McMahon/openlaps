"""The pit's durable consumer: decoded batches in, Timescale rows out.

A durable JetStream **pull** consumer on the pit's sourced `TELE_VEHICLE`
stream. Pull rather than push because the writer should set its own pace:
when the database is slow the fetches simply stop, and backpressure is the
absence of a request rather than a queue somewhere.

**Idempotency is the cursor, not the dedupe window.** JetStream redelivers
on crash, `samples` has no unique key, and `COPY` cannot `ON CONFLICT`, so
duplicate suppression has to come from somewhere else. `Nats-Msg-Id` dedupe
(`docs/WIRE_FORMAT.md`) is not that somewhere: it only makes reconnect-scale
republication idempotent *inside the stream's `duplicate_window`*, which is
two minutes — it says nothing about a writer that crashed and came back an
hour later, or a consumer that was recreated. What does hold is
`ingest_cursor`: every flush advances it in the same transaction as the rows
it describes, so on startup any delivered message at or below it is known to
be committed and is acked without writing.

That guarantee rests on one invariant: **the cursor never advances past a
message the writer declined to process.** Two things can make the writer
decline, and neither hands a message back to the server — returning one
would let newer messages be committed ahead of it, and a single sequence
number cannot express "everything except that one":

- **An unknown `registry_seq`.** The generation is somewhere on the stream
  and the batch is undecodable until it turns up. The writer commits
  everything ahead of the batch, *holds* it and the rest of the fetch
  in memory (never acked, never NAKed, so delivery order is preserved),
  rescans the catalog subject, and re-processes what it held.
- **A failed flush.** The database is away. The buffer and the unacked
  messages stay exactly as they are and the flush is retried; the consumer
  backlogs, bounded by `max_ack_pending`, which is the "stop acking, never
  drop" behaviour this service is supposed to have.

Two smaller guards fall out of holding messages rather than returning them.
A duplicate that arrives because `ack_wait` expired mid-flush is recognised
by sequence and acked without writing. And a flush the database *rejects* —
as opposed to being unable to accept — is retried without its lap rows (the
only rows here with constraints) and then dropped loudly, because retrying
a row the schema will never accept would wedge ingest behind it forever.

An unknown `format_version` is the one case that is *not* transient: the
payload shape is one this build does not understand and never will, so it is
logged once per version, counted, dropped, and the cursor moves past it. A
poison batch must not wedge the consumer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import nats
import nats.errors
import psycopg
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.js import JetStreamContext, api

from core.pb import telemetry_pb2 as pb
from pit.db.dsn import dsn_from_env
from pit.ingest_writer.health import HealthState, serve_health
from pit.ingest_writer.laps import LapMaterialiser, LapRow, PitStatusUpdate
from pit.ingest_writer.store import SampleRow, TimescaleStore
from pit.registry_cache import (
    MSG_TYPE_HEADER,
    MSG_TYPE_REGISTRY,
    REGISTRY_SOURCE_CLASS,
    REJECT_UNKNOWN_SEQ,
    DecodedBatch,
    RegistryCache,
)

logger = logging.getLogger(__name__)

LAP_EVENT_CHANNEL = "lap.event"
AGENT_STATUS_CHANNEL = "sys.agent.status"

_CONNECT_BACKOFF_START_S = 0.5
_CONNECT_BACKOFF_MAX_S = 15.0
_REGISTRY_SCAN_TIMEOUT_S = 1.0
_REPORT_INTERVAL_S = 1.0


@dataclass(frozen=True, slots=True)
class BatchRows:
    """What one decoded batch contributes to the next flush."""

    rows: list[SampleRow]
    lap_events: list[str]
    agent_status: bool
    dropped: int
    unresolved: int


def rows_from_batch(batch: DecodedBatch, keys: Mapping[tuple[int, int], int]) -> BatchRows:
    """Turn one decoded batch into `samples` rows.

    The value/value_text split follows `docs/PIT_SCHEMA.md`: numerics —
    including bool as 0/1 and scale/offset-applied integers, which
    `RegistryCache` has already converted to physical values — go in
    `value`, and STRING channels go in `value_text`. Capture time is the
    batch epoch plus the sample's own offset, so per-sample timing survives
    batching exactly.
    """
    rows: list[SampleRow] = []
    lap_events: list[str] = []
    agent_status = False
    dropped = 0
    unresolved = 0
    for sample in batch.samples:
        channel_key = keys.get((batch.registry_seq, sample.channel.id))
        if channel_key is None:
            # The generation is cached but this wire id resolves to no row:
            # only reachable if the database was rebuilt under a live writer.
            unresolved += 1
            continue
        value = sample.value
        number: float | None = None
        text: str | None = None
        if isinstance(value, str):
            text = value
        elif isinstance(value, bool):
            number = 1.0 if value else 0.0
        elif isinstance(value, (int, float)):
            number = float(value)
        else:
            dropped += 1
            continue
        rows.append(
            (
                datetime.fromtimestamp(sample.capture_unix_ms / 1000.0, tz=UTC),
                channel_key,
                number,
                text,
            )
        )
        if sample.channel.name == LAP_EVENT_CHANNEL and text is not None:
            lap_events.append(text)
        elif sample.channel.name == AGENT_STATUS_CHANNEL:
            agent_status = True
    return BatchRows(
        rows=rows,
        lap_events=lap_events,
        agent_status=agent_status,
        dropped=dropped,
        unresolved=unresolved,
    )


@dataclass(frozen=True, slots=True)
class WriterSettings:
    """Deploy-time wiring; nothing about the data model lives here."""

    nats_url: str
    vehicle_id: str
    dsn: str
    stream: str = "TELE_VEHICLE"
    durable: str = "ingest-writer"
    creds_path: str | None = None
    batch_rows: int = 5_000
    flush_interval_s: float = 0.2
    fetch_batch: int = 500
    max_ack_pending: int = 2_000
    max_buffered_rows: int = 200_000
    ack_wait_s: float = 30.0
    health_port: int = 8081
    stall_retry_s: float = 2.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WriterSettings:
        """Build settings from the environment; see `example.env`."""
        env = os.environ if env is None else env
        vehicle_id = env.get("OPENLAPS_VEHICLE_ID", "").strip()
        if not vehicle_id:
            raise ValueError("OPENLAPS_VEHICLE_ID is required (see example.env)")
        return cls(
            nats_url=env.get("OPENLAPS_NATS_URL", "nats://127.0.0.1:4222").strip(),
            vehicle_id=vehicle_id,
            dsn=dsn_from_env(env),
            stream=env.get("OPENLAPS_INGEST_STREAM", "TELE_VEHICLE").strip(),
            durable=env.get("OPENLAPS_INGEST_DURABLE", "ingest-writer").strip(),
            creds_path=env.get("OPENLAPS_NATS_CREDS", "").strip() or None,
            batch_rows=int(env.get("OPENLAPS_INGEST_BATCH_ROWS", "5000")),
            flush_interval_s=int(env.get("OPENLAPS_INGEST_FLUSH_MS", "200")) / 1000.0,
            fetch_batch=int(env.get("OPENLAPS_INGEST_FETCH_BATCH", "500")),
            max_ack_pending=int(env.get("OPENLAPS_INGEST_MAX_ACK_PENDING", "2000")),
            max_buffered_rows=int(env.get("OPENLAPS_INGEST_MAX_BUFFERED_ROWS", "200000")),
            ack_wait_s=float(env.get("OPENLAPS_INGEST_ACK_WAIT_S", "30")),
            health_port=int(env.get("OPENLAPS_INGEST_HEALTH_PORT", "8081")),
        )


class IngestWriter:
    """Consume `tele.<vehicle>.>`, decode, and write rows to TimescaleDB."""

    def __init__(self, settings: WriterSettings, store: TimescaleStore | None = None) -> None:
        self.settings = settings
        self.health = HealthState()
        self.cache = RegistryCache()
        self.laps = LapMaterialiser(settings.vehicle_id)
        self.store = store if store is not None else TimescaleStore(settings.dsn)

        self._keys: dict[tuple[int, int], int] = {}
        self._cursor = 0
        self._max_seq = 0
        self._rows: list[SampleRow] = []
        self._lap_rows: list[LapRow] = []
        self._pit_updates: list[PitStatusUpdate] = []
        self._pending: list[Msg] = []
        self._buffered: set[int] = set()
        self._held: list[Msg] = []
        self._paused = False
        self._flush_due = 0.0
        self._stop = asyncio.Event()

    @property
    def cursor(self) -> int:
        """Highest stream sequence whose rows are committed."""
        return self._cursor

    # -- lifecycle ---------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Consume until ``stop`` is set.

        Database and NATS outages are handled in place. Anything else
        propagates and ends the process, which is the right posture for a
        supervised container: restarting resumes from `ingest_cursor`, and
        that costs nothing.
        """
        self._stop = stop
        if not await self._connect_db(stop):
            return
        self._cursor = await self.store.read_cursor(self.settings.durable)
        self._max_seq = self._cursor
        logger.info(
            "ingest-writer: resuming %s after committed stream_seq %d",
            self.settings.stream,
            self._cursor,
        )
        client = await self._connect_nats(stop)
        if client is None:
            await self.store.close()
            return
        report = asyncio.create_task(self._report_loop(stop))
        try:
            js = client.jetstream()
            await self._scan_registries(js)
            await self._consume(js, stop)
        finally:
            report.cancel()
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - closing a dead client
                pass
            await self.store.close()

    async def _connect_db(self, stop: asyncio.Event) -> bool:
        """Connect with backoff. A database that is down must not be consumed past."""
        backoff = _CONNECT_BACKOFF_START_S
        while not stop.is_set():
            try:
                await self.store.connect()
            except psycopg.Error as exc:
                logger.warning("ingest-writer: cannot reach the database: %s", exc)
                self.health.db_errors += 1
                await _sleep_unless(stop, backoff)
                backoff = min(backoff * 2, _CONNECT_BACKOFF_MAX_S)
                continue
            self.health.db_connects = self.store.connects
            logger.info("ingest-writer: database connected")
            return True
        return False

    async def _connect_nats(self, stop: asyncio.Event) -> NatsClient | None:
        backoff = _CONNECT_BACKOFF_START_S
        options: dict[str, object] = {
            "servers": [self.settings.nats_url],
            "max_reconnect_attempts": -1,
            "reconnected_cb": self._on_reconnected,
        }
        if self.settings.creds_path:
            options["user_credentials"] = self.settings.creds_path
        while not stop.is_set():
            try:
                client = await nats.connect(**options)
            except Exception as exc:  # noqa: BLE001 - any connect failure retries
                logger.warning(
                    "ingest-writer: cannot connect to %s: %s", self.settings.nats_url, exc
                )
                await _sleep_unless(stop, backoff)
                backoff = min(backoff * 2, _CONNECT_BACKOFF_MAX_S)
                continue
            logger.info("ingest-writer: connected to %s", self.settings.nats_url)
            return client
        return None

    async def _on_reconnected(self) -> None:
        self.health.nats_reconnects += 1
        logger.info("ingest-writer: NATS reconnected")

    # -- registry ----------------------------------------------------------------

    async def _scan_registries(self, js: JetStreamContext) -> int:
        """Replay every retained registry generation into the cache and the DB.

        The recovery path for an unknown generation is the stream, not the
        pit's own `channel_map`: the stream is where the producer's truth
        lives, and the agent republishes its registry on an interval
        precisely so a trimmed stream always holds one copy.

        `stream=` is not optional here. The pit's stream is sourced-only and
        declares no subjects of its own (a subject-declaring copy would
        double-capture across the leafnode), so it cannot be found by
        subject lookup — see `deploy/README.md` -> the sourced stream.
        """
        subject = f"tele.{self.settings.vehicle_id}.{REGISTRY_SOURCE_CLASS}"
        subscription = await js.subscribe(
            subject,
            stream=self.settings.stream,
            ordered_consumer=True,
            deliver_policy=api.DeliverPolicy.ALL,
        )
        added = 0
        try:
            while True:
                try:
                    message = await subscription.next_msg(timeout=_REGISTRY_SCAN_TIMEOUT_S)
                except TimeoutError:
                    break
                registry = pb.ChannelRegistry()
                registry.ParseFromString(message.data)
                if not self.cache.known(registry.registry_seq):
                    added += 1
                await self._apply_registry(registry)
        finally:
            try:
                await subscription.unsubscribe()
            except Exception:  # noqa: BLE001 - the scan is done either way
                pass
        logger.info(
            "ingest-writer: registry scan found generations %s", self.cache.generations() or "none"
        )
        return added

    async def _apply_registry(self, registry: pb.ChannelRegistry) -> None:
        """Record one generation in the cache and the database.

        Retried until it lands: without the `channel_map` rows, every batch
        of this generation would decode to samples with nowhere to go.
        """
        while not self._stop.is_set():
            try:
                keys = await self.store.upsert_registry(self.settings.vehicle_id, registry)
            except psycopg.Error as exc:
                logger.error(
                    "ingest-writer: cannot record registry generation %d: %s",
                    registry.registry_seq,
                    exc,
                )
                self.health.db_errors += 1
                await self._reconnect_db()
                continue
            for wire_id, channel_key in keys.items():
                self._keys[(registry.registry_seq, wire_id)] = channel_key
            self.cache.add_registry(registry)
            return

    # -- consume -----------------------------------------------------------------

    async def _subscribe(self, js: JetStreamContext) -> JetStreamContext.PullSubscription:
        return await js.pull_subscribe(
            f"tele.{self.settings.vehicle_id}.>",
            durable=self.settings.durable,
            stream=self.settings.stream,
            config=api.ConsumerConfig(
                durable_name=self.settings.durable,
                ack_policy=api.AckPolicy.EXPLICIT,
                deliver_policy=api.DeliverPolicy.ALL,
                max_ack_pending=self.settings.max_ack_pending,
                ack_wait=self.settings.ack_wait_s,
                filter_subject=f"tele.{self.settings.vehicle_id}.>",
            ),
        )

    async def _consume(self, js: JetStreamContext, stop: asyncio.Event) -> None:
        subscription = await self._subscribe(js)
        self._flush_due = time.monotonic() + self.settings.flush_interval_s
        while not stop.is_set():
            if self._held:
                # Messages retained from a stalled fetch come first, in the
                # order they were delivered. They were never acked or NAKed:
                # holding them rather than handing them back is what keeps
                # delivery order intact while the writer sorts itself out.
                messages, self._held = self._held, []
            elif len(self._rows) >= self.settings.max_buffered_rows:
                # The flush is not landing and the buffer has grown as far as
                # it may. Stop pulling entirely: unacked messages stay on the
                # stream, which is the only place with room for them.
                if not self._paused:
                    self._paused = True
                    logger.warning(
                        "ingest-writer: %d rows buffered, pausing consumption until they land",
                        len(self._rows),
                    )
                await self._flush()
                await _sleep_unless(stop, self.settings.flush_interval_s)
                continue
            else:
                timeout = max(0.01, self._flush_due - time.monotonic())
                try:
                    messages = await subscription.fetch(self.settings.fetch_batch, timeout=timeout)
                except TimeoutError:
                    messages = []
                except Exception as exc:  # noqa: BLE001 - a dead subscription is rebuilt
                    logger.warning("ingest-writer: fetch failed, resubscribing: %s", exc)
                    await _sleep_unless(stop, _CONNECT_BACKOFF_START_S)
                    subscription = await self._resubscribe(js, subscription)
                    continue

            barrier = await self._handle_all(messages)
            if barrier is not None:
                await self._recover_registry(js, barrier, stop)
                continue
            if len(self._rows) >= self.settings.batch_rows or time.monotonic() >= self._flush_due:
                await self._flush()
        await self._flush()

    async def _handle_all(self, messages: list[Msg]) -> int | None:
        """Process a fetch in order; returns the barrier generation if it stalls."""
        for index, message in enumerate(messages):
            barrier = await self._handle(message)
            if barrier is None:
                continue
            # Commit everything ahead of the barrier and retain the rest,
            # this message included, for after the generation is recovered.
            self.health.unknown_seq_stalls += 1
            self.health.stalled = True
            self._held = messages[index:]
            await self._flush()
            return barrier
        return None

    async def _handle(self, message: Msg) -> int | None:
        """Process one message; returns an unknown registry_seq to stall on."""
        sequence = message.metadata.sequence.stream
        if sequence <= self._cursor:
            # Already committed. This is the entire idempotency guarantee:
            # redelivery, however it arose, becomes an ack and nothing else.
            await self._ack(message)
            self.health.messages_skipped += 1
            return None
        if sequence in self._buffered:
            # An in-flight duplicate: `ack_wait` expired on a message this
            # flush is already carrying. Writing it twice would duplicate
            # rows the cursor cannot distinguish afterwards.
            await self._ack(message)
            self.health.messages_skipped += 1
            return None
        if _is_registry(message, self.settings.vehicle_id):
            registry = pb.ChannelRegistry()
            registry.ParseFromString(message.data)
            await self._apply_registry(registry)
            await self._ack(message)
            self.health.registries += 1
            logger.info("ingest-writer: registry generation %d applied", registry.registry_seq)
            return None

        received_mono_ns = time.monotonic_ns()
        batch, reason = self.cache.decode_or_reason(message.data)
        if batch is None:
            self.health.unknown_seq_batches = self.cache.unknown_seq_batches
            self.health.bad_version_batches = self.cache.bad_version_batches
            if reason == REJECT_UNKNOWN_SEQ:
                parsed = pb.SampleBatch()
                parsed.ParseFromString(message.data)
                return parsed.registry_seq
            # Unknown payload shape: permanent, so drop it and move on rather
            # than wedging every later batch behind it.
            await self._ack(message)
            self._max_seq = max(self._max_seq, sequence)
            return None

        self.health.observe_batch(batch.epoch_mono_ns, batch.epoch_unix_ms, received_mono_ns)
        converted = rows_from_batch(batch, self._keys)
        self._rows.extend(converted.rows)
        self.health.samples_dropped += converted.dropped
        self.health.unresolved_channels += converted.unresolved
        if converted.agent_status:
            self.health.note_agent_status()
        for payload in converted.lap_events:
            self._materialise_lap(payload)
        self._pending.append(message)
        self._buffered.add(sequence)
        self._max_seq = max(self._max_seq, sequence)
        return None

    def _materialise_lap(self, payload: str) -> None:
        """Fold one lap.event; the raw sample is written either way."""
        row = self.laps.observe(payload)
        if isinstance(row, LapRow):
            self._lap_rows.append(row)
        elif isinstance(row, PitStatusUpdate):
            self._pit_updates.append(row)
        self.health.malformed_lap_events = self.laps.malformed_events

    # -- flush -------------------------------------------------------------------

    async def _flush(self) -> bool:
        """Commit one flush and ack it; False if the database refused.

        A refusal keeps the buffer and the unacked messages exactly as they
        are and retries on the next pass. Handing the messages back instead
        would let newer ones be committed ahead of them, and the cursor
        cannot express "everything except that one" — so the writer holds
        its position and lets the consumer backlog, which is what
        `max_ack_pending` is there to bound.
        """
        self._flush_due = time.monotonic() + self.settings.flush_interval_s
        if not self._pending_work():
            return True
        laps_landed = True
        try:
            await self._write(self._rows, self._lap_rows, self._pit_updates)
        except psycopg.OperationalError as exc:
            logger.error(
                "ingest-writer: flush failed (%d rows buffered), will retry: %s",
                len(self._rows),
                exc,
            )
            self.health.db_errors += 1
            await self._reconnect_db()
            return False
        except psycopg.Error as exc:
            # Not a connectivity problem: some row in this flush is one the
            # schema will never accept, and retrying it forever would wedge
            # ingest behind it. Laps are the only rows with constraints, so
            # try again without them before giving up on the flush.
            logger.error("ingest-writer: flush rejected (%s); retrying samples only", exc)
            self.health.data_errors += 1
            laps_landed = False
            try:
                await self._write(self._rows, [], [])
            except psycopg.OperationalError as retry_exc:
                # The connection died during the retry. That is not a refusal
                # and must not be treated as one: dropping here would ack
                # messages and advance the cursor over rows no transaction
                # ever committed. Keep everything and let the retry path run.
                logger.error(
                    "ingest-writer: connection lost retrying a rejected flush, "
                    "keeping %d row(s): %s",
                    len(self._rows),
                    retry_exc,
                )
                self.health.db_errors += 1
                await self._reconnect_db()
                return False
            except psycopg.Error:
                logger.exception(
                    "ingest-writer: dropping a flush of %d row(s) the database refuses",
                    len(self._rows),
                )
                self.health.dropped_flushes += 1
                self.health.laps_dropped += len(self._lap_rows)
                self._cursor = max(self._cursor, self._max_seq)
                for message in self._pending:
                    await self._ack(message)
                self._discard()
                return False
        self._cursor = max(self._cursor, self._max_seq)
        self.health.observe_flush(len(self._rows), self._cursor)
        if laps_landed:
            self.health.laps_written += len(self._lap_rows)
            self.health.sectors_written += sum(len(lap.sectors) for lap in self._lap_rows)
        else:
            # The samples landed but the laps in this flush were the rows the
            # schema refused; counting them as written would make the one
            # metric an operator checks say the opposite of what happened.
            self.health.laps_dropped += len(self._lap_rows)
        for message in self._pending:
            await self._ack(message)
        self._discard()
        return True

    def _pending_work(self) -> bool:
        return bool(
            self._rows or self._lap_rows or self._pit_updates or self._max_seq > self._cursor
        )

    async def _write(
        self, rows: list[SampleRow], laps: list[LapRow], pit_updates: list[PitStatusUpdate]
    ) -> None:
        await self.store.flush(
            rows=rows,
            laps=laps,
            pit_updates=pit_updates,
            consumer=self.settings.durable,
            stream=self.settings.stream,
            stream_seq=self._max_seq,
        )

    def _discard(self) -> None:
        self._rows = []
        self._lap_rows = []
        self._pit_updates = []
        self._pending = []
        self._buffered = set()
        if self._paused:
            self._paused = False
            logger.info("ingest-writer: buffer drained, resuming consumption")

    async def _reconnect_db(self) -> None:
        """Rebuild the connection, honouring shutdown while it retries."""
        await self.store.close()
        await self._connect_db(self._stop)

    # -- recovery ----------------------------------------------------------------

    async def _resubscribe(
        self, js: JetStreamContext, subscription: JetStreamContext.PullSubscription
    ) -> JetStreamContext.PullSubscription:
        """Rebuild the pull subscription after the old one dies.

        Only for a broken subscription — never as a recovery step, because
        unsubscribing orphans any redelivery already in flight to the old
        inbox until `ack_wait` expires.
        """
        try:
            await subscription.unsubscribe()
        except Exception:  # noqa: BLE001 - it may already be gone
            pass
        return await self._subscribe(js)

    async def _recover_registry(
        self, js: JetStreamContext, registry_seq: int, stop: asyncio.Event
    ) -> None:
        """Go looking for a generation a batch referenced but we don't hold."""
        logger.warning(
            "ingest-writer: stalled on unknown registry_seq=%d, rescanning the catalog subject",
            registry_seq,
        )
        added = await self._scan_registries(js)
        if self.cache.known(registry_seq):
            logger.info("ingest-writer: registry_seq=%d recovered, resuming", registry_seq)
            self.health.stalled = False
            return
        # Not on the stream yet. The agent republishes its registry on an
        # interval, so waiting is the correct move — dropping the batch would
        # be silent data loss, and guessing is forbidden outright.
        logger.error(
            "ingest-writer: registry_seq=%d is not on the stream (%d other generation(s) "
            "found); waiting for the agent to republish",
            registry_seq,
            added,
        )
        await _sleep_unless(stop, self.settings.stall_retry_s)

    async def _ack(self, message: Msg) -> None:
        try:
            await message.ack()
        except Exception as exc:  # noqa: BLE001 - redelivery is safe, the cursor covers it
            logger.warning("ingest-writer: ack failed: %s", exc)

    # -- reporting ---------------------------------------------------------------

    async def _report_loop(self, stop: asyncio.Event) -> None:
        """Log the health line once a second (`docs/ARCHITECTURE.md`)."""
        try:
            while not stop.is_set():
                await asyncio.sleep(_REPORT_INTERVAL_S)
                self.health.roll()
                logger.info("ingest-writer: %s", self.health.log_line())
        except asyncio.CancelledError:
            return


def _is_registry(message: Msg, vehicle_id: str) -> bool:
    """Registry or batch? The producer's header first, the subject as backup."""
    header = (message.headers or {}).get(MSG_TYPE_HEADER)
    if header:
        return header == MSG_TYPE_REGISTRY
    return message.subject == f"tele.{vehicle_id}.{REGISTRY_SOURCE_CLASS}"


async def _sleep_unless(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, but wake immediately on shutdown."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def run_writer(settings: WriterSettings, stop: asyncio.Event) -> None:
    """Run one writer with its health endpoint until ``stop`` is set."""
    writer = IngestWriter(settings)
    server = serve_health(writer.health, settings.health_port)
    try:
        await writer.run(stop)
    finally:
        server.shutdown()
