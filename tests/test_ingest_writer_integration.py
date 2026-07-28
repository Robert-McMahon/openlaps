"""Ingest-writer against a real nats-server and a real TimescaleDB.

Batches are published by the *real* agent-side `JetStreamPublisher` from
batches built by the *real* `Batcher`, so nothing about the encoding or the
transport is simulated. Skipped automatically when docker is unavailable —
see the `nats_url` and `timescale_dsn` fixtures in conftest.

The writer consumes stream `TELE` directly here. In a deployment it consumes
the pit's sourced `TELE_VEHICLE`, but sourcing preserves subjects and
sequence order, and standing up a leafnode pair is P3.6's job — what this
file tests is the writer.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable

import nats
import psycopg
import pytest

from agent.pipeline import TickBatch
from agent.publisher import JetStreamPublisher
from core.batcher import Batcher
from core.catalog import ChannelPolicy
from core.pb import telemetry_pb2 as pb
from core.samples import Sample
from pit.db.migrate import apply_migrations
from pit.ingest_writer.store import TimescaleStore
from pit.ingest_writer.writer import IngestWriter, WriterSettings

VEHICLE = "example-club-racer"
MS = 1_000_000  # ns
BASE_EPOCH_MS = 1_780_000_000_000
DURABLE = "test-ingest-writer"

CHANNELS = (
    # name, units, wire type, scale
    ("car.rpm", "rpm", pb.DOUBLE, 0.0),
    ("car.coolant_temp", "K", pb.UINT, 0.1),
    ("lap.event", "", pb.STRING, 0.0),
    ("sys.agent.status", "", pb.STRING, 0.0),
)


# --- building and publishing ------------------------------------------------


def _catalog(registry_seq: int, first_id: int = 1):
    """A registry plus batcher policies; `first_id` renumbers the wire ids."""
    registry = pb.ChannelRegistry(
        registry_seq=registry_seq, vehicle_id=VEHICLE, created_unix_ms=BASE_EPOCH_MS
    )
    policies: dict[int, ChannelPolicy] = {}
    ids: dict[str, int] = {}
    for index, (name, units, value_type, scale) in enumerate(CHANNELS):
        wire_id = first_id + index
        registry.channels.add(
            id=wire_id,
            name=name,
            source_ref=f"can0:test.{name}",
            units=units,
            type=value_type,
            scale=scale,
        )
        policies[wire_id] = ChannelPolicy(name=name, value_type=value_type, rbe=None, scale=scale)
        ids[name] = wire_id
    return registry, policies, ids


def _tick(catalog, values: list[tuple[str, object]], *, epoch_unix_ms: int) -> TickBatch:
    """One TickBatch through the real batcher, ready for the real publisher."""
    registry, policies, ids = catalog
    batcher = Batcher(registry.registry_seq, policies, tick_ms=20)
    epoch_mono_ns = (epoch_unix_ms - BASE_EPOCH_MS) * MS
    for index, (name, value) in enumerate(values):
        batcher.add(
            "can0",
            ids[name],
            Sample(f"can0:test.{name}", epoch_mono_ns + index * MS, float(epoch_unix_ms), value),
        )
    return TickBatch(
        source_class="can0",
        payload=batcher.tick(epoch_unix_ms, epoch_mono_ns)["can0"],
        epoch_unix_ms=epoch_unix_ms,
        epoch_mono_ns=epoch_mono_ns,
        msg_id=f"can0:{epoch_unix_ms}",
    )


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 20.0, message: str = "") -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for {message}")


def _publish(nats_url: str, catalog, batches: list[TickBatch]) -> None:
    """Publish a registry generation and some batches through the real publisher."""
    registry, _, _ = catalog
    publisher = JetStreamPublisher(
        nats_url=nats_url,
        vehicle_id=VEHICLE,
        registry_payload=registry.SerializeToString(),
        registry_interval_s=3600.0,
    )
    publisher.start()
    try:
        _wait_until(lambda: publisher.registry_publishes >= 1, message="registry publish")
        for batch in batches:
            publisher.submit(batch)
        assert publisher.drain(timeout_s=15.0)
    finally:
        publisher.stop()


async def _publish_raw(nats_url: str, subject: str, payload: bytes, headers=None) -> None:
    client = await nats.connect(nats_url)
    try:
        await client.jetstream().publish(subject, payload, headers=headers)
    finally:
        await client.close()


# --- running the writer -----------------------------------------------------


def _settings(nats_url: str, dsn: str, **overrides) -> WriterSettings:
    defaults = {
        "nats_url": nats_url,
        "vehicle_id": VEHICLE,
        "dsn": dsn,
        "stream": "TELE",
        "durable": DURABLE,
        "flush_interval_s": 0.05,
        "batch_rows": 100,
        "fetch_batch": 50,
        "health_port": 0,
        "stall_retry_s": 0.2,
    }
    return WriterSettings(**{**defaults, **overrides})


async def _run_until(
    settings: WriterSettings,
    predicate: Callable[[IngestWriter], bool],
    *,
    timeout_s: float = 25.0,
    store: TimescaleStore | None = None,
    meanwhile: Callable[[], object] | None = None,
    meanwhile_when: Callable[[IngestWriter], bool] | None = None,
) -> IngestWriter:
    """Run a writer until ``predicate`` holds (or time runs out), then stop it.

    ``meanwhile`` runs once, without stopping the writer, as soon as
    ``meanwhile_when`` holds — for the cases where something has to happen on
    the stream *while* the writer is watching it.
    """
    writer = IngestWriter(settings, store=store)
    stop = asyncio.Event()
    task = asyncio.create_task(writer.run(stop))
    deadline = time.monotonic() + timeout_s
    did_meanwhile = meanwhile is None
    try:
        while time.monotonic() < deadline:
            if task.done():
                break
            if not did_meanwhile and (meanwhile_when is None or meanwhile_when(writer)):
                did_meanwhile = True
                await meanwhile()
            if predicate(writer):
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=15.0)
    return writer


@pytest.fixture
def dsn(timescale_dsn):
    """A migrated, empty pit database."""
    with psycopg.connect(timescale_dsn) as conn:
        apply_migrations(conn)
    return timescale_dsn


def _query(dsn: str, sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(sql, params).fetchall()


# --- tests ------------------------------------------------------------------


def test_batches_become_rows_with_names_times_and_scaling(nats_url, dsn):
    catalog = _catalog(registry_seq=1)
    batches = [
        _tick(
            catalog,
            [("car.rpm", 4500.0), ("car.coolant_temp", 355.0)],
            epoch_unix_ms=BASE_EPOCH_MS,
        ),
        _tick(
            catalog,
            [("car.rpm", 4600.0), ("sys.agent.status", '{"state":"running"}')],
            epoch_unix_ms=BASE_EPOCH_MS + 100,
        ),
    ]
    _publish(nats_url, catalog, batches)

    writer = asyncio.run(_run_until(_settings(nats_url, dsn), lambda w: w.health.rows_written >= 4))
    assert writer.health.rows_written == 4

    rows = _query(
        dsn,
        "SELECT channel, units, value, value_text, "
        "EXTRACT(EPOCH FROM time) * 1000 FROM v_samples_named ORDER BY time, channel",
    )
    # Ordered by capture time: each batch's samples are 1 ms apart within it.
    assert [(row[0], row[1]) for row in rows] == [
        ("car.rpm", "rpm"),
        ("car.coolant_temp", "K"),
        ("car.rpm", "rpm"),
        ("sys.agent.status", ""),
    ]
    by_channel = {row[0]: row for row in rows}
    # scale/offset applied: the wire carried 3550, the database holds 355.0 K.
    assert by_channel["car.coolant_temp"][2] == pytest.approx(355.0)
    assert by_channel["sys.agent.status"][3] == '{"state":"running"}'
    # Exact capture time, including the per-sample offset within the tick.
    assert float(by_channel["car.coolant_temp"][4]) == pytest.approx(BASE_EPOCH_MS + 1.0, abs=0.5)

    cursor = _query(
        dsn, "SELECT stream, stream_seq FROM ingest_cursor WHERE consumer = %s", (DURABLE,)
    )
    assert cursor[0][0] == "TELE"
    assert cursor[0][1] >= 3  # registry + two batches


def test_a_restart_mid_stream_duplicates_nothing_and_skips_nothing(nats_url, dsn):
    """The cursor, not the dedupe window, is what makes redelivery a no-op."""
    catalog = _catalog(registry_seq=1)
    first = [
        _tick(catalog, [("car.rpm", float(4000 + i))], epoch_unix_ms=BASE_EPOCH_MS + i)
        for i in range(20)
    ]
    _publish(nats_url, catalog, first)

    writer = asyncio.run(
        _run_until(_settings(nats_url, dsn), lambda w: w.health.rows_written >= 20)
    )
    committed = writer.cursor
    assert committed > 0

    # A second lifetime of the same durable, with more data behind it. The
    # earlier messages are redelivered (the consumer's ack floor is not the
    # cursor) and must be recognised and skipped, not rewritten.
    second = [
        _tick(catalog, [("car.rpm", float(5000 + i))], epoch_unix_ms=BASE_EPOCH_MS + 100 + i)
        for i in range(20)
    ]
    _publish(nats_url, catalog, second)
    restarted = asyncio.run(
        _run_until(_settings(nats_url, dsn), lambda w: w.health.rows_written >= 20)
    )
    assert restarted.cursor > committed

    values = _query(dsn, "SELECT value FROM samples ORDER BY time")
    assert len(values) == 40, "a redelivered message was written twice"
    assert [row[0] for row in values] == [float(4000 + i) for i in range(20)] + [
        float(5000 + i) for i in range(20)
    ]


def test_one_channel_key_spans_a_registry_rollover(nats_url, dsn):
    """Wire ids renumber across generations; the stored identity does not."""
    first = _catalog(registry_seq=1, first_id=1)
    _publish(nats_url, first, [_tick(first, [("car.rpm", 4500.0)], epoch_unix_ms=BASE_EPOCH_MS)])

    # A catalog reload: same channels, new generation, different wire ids.
    second = _catalog(registry_seq=2, first_id=50)
    _publish(
        nats_url, second, [_tick(second, [("car.rpm", 4700.0)], epoch_unix_ms=BASE_EPOCH_MS + 50)]
    )

    asyncio.run(_run_until(_settings(nats_url, dsn), lambda w: w.health.rows_written >= 2))

    keys = _query(dsn, "SELECT count(*) FROM channels WHERE name = 'car.rpm'")
    assert keys[0][0] == 1
    mapped = _query(
        dsn,
        "SELECT registry_seq, wire_id FROM channel_map cm JOIN channels c USING (channel_key) "
        "WHERE c.name = 'car.rpm' ORDER BY registry_seq",
    )
    assert mapped == [(1, 1), (2, 50)]
    assert [row[0] for row in _query(dsn, "SELECT value FROM v_samples_named ORDER BY time")] == [
        4500.0,
        4700.0,
    ]


def test_laps_and_sectors_are_materialised_from_lap_events(nats_url, dsn):
    catalog = _catalog(registry_seq=1)
    crossing = BASE_EPOCH_MS / 1000.0

    def event(**payload) -> str:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    batches = [
        _tick(
            catalog,
            [
                (
                    "lap.event",
                    event(
                        type="sector_completed",
                        time=crossing + 40.0,
                        line="Sector1",
                        lap_number=4,
                        sector=1,
                        split_time=40.0,
                        valid=True,
                        direction="counterclockwise",
                        pit_status="track",
                    ),
                )
            ],
            epoch_unix_ms=BASE_EPOCH_MS,
        ),
        _tick(
            catalog,
            [
                (
                    "lap.event",
                    event(
                        type="lap_completed",
                        time=crossing + 108.842,
                        line="StartFinish",
                        lap_number=4,
                        lap_time=108.842,
                        valid=True,
                        direction="counterclockwise",
                        pit_status="track",
                        track_name="Wanneroo",
                    ),
                )
            ],
            epoch_unix_ms=BASE_EPOCH_MS + 20,
        ),
        # Malformed: counted, and the raw sample is still stored.
        _tick(catalog, [("lap.event", "{not json")], epoch_unix_ms=BASE_EPOCH_MS + 40),
    ]
    _publish(nats_url, catalog, batches)

    writer = asyncio.run(_run_until(_settings(nats_url, dsn), lambda w: w.health.rows_written >= 3))
    assert writer.health.laps_written == 1
    assert writer.health.malformed_lap_events == 1

    laps = _query(dsn, "SELECT lap_number, lap_time_s, valid, track_name, session_id FROM v_laps")
    assert laps == [(4, pytest.approx(108.842), True, "Wanneroo", None)]
    sectors = _query(dsn, "SELECT sector, split_time_s FROM lap_sectors")
    assert sectors == [(1, pytest.approx(40.0))]
    # Nothing is lost: every lap.event is still a row, malformed or not.
    assert _query(dsn, "SELECT count(*) FROM samples WHERE value_text IS NOT NULL")[0][0] == 3


def test_an_unknown_registry_seq_stalls_until_the_generation_arrives(nats_url, dsn):
    """Never decode against the wrong generation; wait for the right one."""
    first = _catalog(registry_seq=1)
    _publish(nats_url, first, [_tick(first, [("car.rpm", 4500.0)], epoch_unix_ms=BASE_EPOCH_MS)])

    # A batch from a generation whose registry has not been published.
    second = _catalog(registry_seq=2, first_id=50)
    orphan = _tick(second, [("car.rpm", 4700.0)], epoch_unix_ms=BASE_EPOCH_MS + 50)
    asyncio.run(_publish_raw(nats_url, f"tele.{VEHICLE}.can0", orphan.payload))

    async def republish_the_missing_generation() -> None:
        # What the agent's registry republish interval does for real.
        registry, _, _ = second
        await _publish_raw(
            nats_url,
            f"tele.{VEHICLE}.catalog",
            registry.SerializeToString(),
            headers={"Openlaps-Msg-Type": "registry"},
        )

    writer = asyncio.run(
        _run_until(
            _settings(nats_url, dsn),
            lambda w: w.health.rows_written >= 2,
            timeout_s=25.0,
            meanwhile=republish_the_missing_generation,
            # Only once the writer has actually stalled: until then, the
            # undecodable batch must not have produced a row.
            meanwhile_when=lambda w: w.health.unknown_seq_stalls >= 1,
        )
    )
    assert writer.health.unknown_seq_stalls >= 1
    assert writer.health.rows_written == 2
    assert [row[0] for row in _query(dsn, "SELECT value FROM samples ORDER BY time")] == [
        4500.0,
        4700.0,
    ]


def test_a_poison_batch_is_dropped_without_wedging_the_consumer(nats_url, dsn):
    catalog = _catalog(registry_seq=1)
    _publish(
        nats_url, catalog, [_tick(catalog, [("car.rpm", 4500.0)], epoch_unix_ms=BASE_EPOCH_MS)]
    )

    poison = pb.SampleBatch()
    poison.ParseFromString(
        _tick(catalog, [("car.rpm", 1.0)], epoch_unix_ms=BASE_EPOCH_MS + 10).payload
    )
    poison.format_version = 99
    asyncio.run(_publish_raw(nats_url, f"tele.{VEHICLE}.can0", poison.SerializeToString()))

    # A batch published *after* the poison one must still land.
    _publish(
        nats_url,
        catalog,
        [_tick(catalog, [("car.rpm", 4900.0)], epoch_unix_ms=BASE_EPOCH_MS + 20)],
    )

    writer = asyncio.run(
        _run_until(_settings(nats_url, dsn), lambda w: w.health.rows_written >= 2, timeout_s=20.0)
    )
    assert writer.health.bad_version_batches == 1
    assert [row[0] for row in _query(dsn, "SELECT value FROM samples ORDER BY time")] == [
        4500.0,
        4900.0,
    ]


def test_a_database_outage_mid_run_loses_nothing(nats_url, dsn):
    """DB down mid-run: stop acking, let the consumer backlog, never drop."""

    class FlakyStore(TimescaleStore):
        """Refuses the first two flushes the way a dead connection would."""

        def __init__(self, dsn: str) -> None:
            super().__init__(dsn)
            self.refusals = 0

        async def flush(self, **kwargs):
            if self.refusals < 2:
                self.refusals += 1
                raise psycopg.OperationalError("connection is closed (simulated outage)")
            return await super().flush(**kwargs)

    catalog = _catalog(registry_seq=1)
    batches = [
        _tick(catalog, [("car.rpm", float(4000 + i))], epoch_unix_ms=BASE_EPOCH_MS + i)
        for i in range(10)
    ]
    _publish(nats_url, catalog, batches)

    store = FlakyStore(dsn)
    writer = asyncio.run(
        _run_until(
            _settings(nats_url, dsn),
            lambda w: w.health.rows_written >= 10,
            timeout_s=30.0,
            store=store,
        )
    )
    assert store.refusals == 2
    assert writer.health.db_errors >= 2
    values = [row[0] for row in _query(dsn, "SELECT value FROM samples ORDER BY time")]
    assert values == [float(4000 + i) for i in range(10)], "an outage lost or duplicated rows"


def test_a_database_that_is_down_at_startup_is_not_consumed_past(nats_url):
    """No docker needed: an unreachable DSN must retry, not consume."""
    settings = _settings(nats_url, "postgresql://127.0.0.1:1/nope?connect_timeout=1")

    async def scenario() -> IngestWriter:
        writer = IngestWriter(settings)
        stop = asyncio.Event()
        task = asyncio.create_task(writer.run(stop))
        await asyncio.sleep(1.5)
        stop.set()
        await asyncio.wait_for(task, timeout=10.0)
        return writer

    writer = asyncio.run(scenario())
    assert writer.health.db_errors >= 1
    assert writer.health.batches == 0, "consumed telemetry with no database to write it to"
    assert writer.cursor == 0
