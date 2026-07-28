"""The deployed topology: a leafnode pair, a sourced stream, and recovery.

Every other integration test in this repository publishes into a
single-server `TELE` that declares `tele.<vehicle>.>`. That is not the shape
anything actually runs in, and the difference is not cosmetic — it is where
subject-based stream lookup stops working and where ADR 0002's "the single
biggest unvalidated assumption in the whole rewrite" gets its first real
exercise. This file is the only place both ends exist at once.

The `leafnode_pair` fixture (conftest) stands up two `nats-server`
containers configured exactly as `deploy/nats/vehicle.conf` and
`deploy/nats/pit.conf` configure them, minus TLS: distinct JetStream domains
`veh`/`pit`, the pit dialling the vehicle, and a leafnode that can be cut
and restored. Skipped automatically when docker is unavailable.

What this does *not* prove is RF. Severing a docker network is an abrupt,
clean, symmetric cut; a half-duplex HaLow radio under load is none of those.
That measurement is Phase 4's garage bench test.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import aiomqtt
import nats
import psycopg
import pytest
from nats.js import api
from nats.js.errors import NotFoundError

sys.path.insert(0, str(Path(__file__).parents[1] / "deploy"))

import provision_pit_streams as provision  # noqa: E402
from test_ingest_writer_integration import (  # noqa: E402
    BASE_EPOCH_MS,
    VEHICLE,
    _catalog,
    _publish,
    _tick,
)

from pit.db.migrate import apply_migrations  # noqa: E402
from pit.ingest_writer.writer import IngestWriter, WriterSettings  # noqa: E402
from pit.live_decoder.service import LiveDecoder, LiveDecoderSettings  # noqa: E402

PIT_STREAM = "TELE_VEHICLE"
DURABLE = "test-topology-writer"


# --- helpers ----------------------------------------------------------------


async def _provision_pit(pair) -> None:
    """Create the sourced stream exactly as deploy/provision_pit_streams.py does."""
    client = await nats.connect(pair.pit_url)
    try:
        await provision.ensure_pit_stream(
            client.jetstream(),
            provision.pit_stream_config(name=PIT_STREAM, source_stream="TELE", domain="veh"),
        )
    finally:
        await client.close()


async def _stream_messages(url: str, stream: str) -> int:
    client = await nats.connect(url)
    try:
        info = await client.jetstream().stream_info(stream)
        return info.state.messages
    finally:
        await client.close()


async def _await_sourced(pair, expected: int, *, timeout_s: float = 60.0) -> int:
    """Wait for the pit stream to reach ``expected`` messages; return what it has."""
    deadline = time.monotonic() + timeout_s
    count = -1
    while time.monotonic() < deadline:
        count = await _stream_messages(pair.pit_url, PIT_STREAM)
        if count >= expected:
            return count
        await asyncio.sleep(0.25)
    return count


async def _read_all(url: str, stream: str, subject: str, expected: int) -> list[bytes]:
    """Every retained payload on ``subject``, in stream order.

    Read back through the stream rather than counted at the consumer: a
    count-only assertion passes on a stream that dropped one message and
    duplicated another, which is precisely the failure mode a link dropout
    would produce.
    """
    client = await nats.connect(url)
    try:
        subscription = await client.jetstream().subscribe(
            subject,
            stream=stream,
            ordered_consumer=True,
            deliver_policy=api.DeliverPolicy.ALL,
        )
        payloads: list[bytes] = []
        try:
            while len(payloads) < expected:
                message = await subscription.next_msg(timeout=5.0)
                payloads.append(message.data)
            # One more beat: if anything extra is on the stream, catch it
            # here rather than declaring success at the expected count.
            try:
                extra = await subscription.next_msg(timeout=1.0)
                payloads.append(extra.data)
            except TimeoutError:
                pass
        except TimeoutError:
            pass
        finally:
            await subscription.unsubscribe()
        return payloads
    finally:
        await client.close()


async def _create_vehicle_streams(pair) -> None:
    """TELE and CMD as the agent creates them (src/agent/publisher.py)."""
    client = await nats.connect(pair.vehicle_url)
    try:
        js = client.jetstream()
        await js.add_stream(
            api.StreamConfig(
                name="TELE",
                subjects=[f"tele.{VEHICLE}.>"],
                storage=api.StorageType.FILE,
                duplicate_window=120.0,
            )
        )
        await js.add_stream(
            api.StreamConfig(
                name="CMD",
                subjects=[f"cmd.{VEHICLE}.>"],
                storage=api.StorageType.FILE,
                max_msgs_per_subject=1,
            )
        )
    finally:
        await client.close()


@pytest.fixture
def pit_dsn(timescale_dsn):
    """A migrated, empty pit database."""
    with psycopg.connect(timescale_dsn) as conn:
        apply_migrations(conn)
    return timescale_dsn


# --- tests ------------------------------------------------------------------


def test_batches_cross_the_leafnode_and_become_pit_rows(leafnode_pair, pit_dsn):
    """Vehicle TELE -> sourced TELE_VEHICLE -> Timescale, end to end.

    The batches are built by the real `Batcher` and published by the real
    agent-side `JetStreamPublisher`, so the encoding crossing the link is
    the encoding the car produces.
    """

    async def exercise() -> IngestWriter:
        await _provision_pit(leafnode_pair)
        catalog = _catalog(registry_seq=1)
        batches = [
            _tick(
                catalog,
                [("car.rpm", 4500.0 + index), ("car.coolant_temp", 355.0)],
                epoch_unix_ms=BASE_EPOCH_MS + index,
            )
            for index in range(5)
        ]
        # The publisher is synchronous and creates TELE itself; run it off
        # the loop so its own thread is not fighting this one.
        await asyncio.to_thread(_publish, leafnode_pair.vehicle_url, catalog, batches)

        # registry + five batches
        assert await _await_sourced(leafnode_pair, 6) == 6

        settings = WriterSettings(
            nats_url=leafnode_pair.pit_url,
            vehicle_id=VEHICLE,
            dsn=pit_dsn,
            stream=PIT_STREAM,
            durable=DURABLE,
            flush_interval_s=0.05,
            batch_rows=100,
            fetch_batch=50,
            health_port=0,
        )
        writer = IngestWriter(settings)
        stop = asyncio.Event()
        task = asyncio.create_task(writer.run(stop))
        deadline = time.monotonic() + 40.0
        try:
            while time.monotonic() < deadline and writer.health.rows_written < 10:
                if task.done():
                    break
                await asyncio.sleep(0.05)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=15.0)
        return writer

    writer = asyncio.run(exercise())
    assert writer.health.rows_written == 10

    with psycopg.connect(pit_dsn) as conn:
        rows = conn.execute(
            "SELECT channel, units, value FROM v_samples_named ORDER BY time, channel"
        ).fetchall()
    assert len(rows) == 10
    # Names resolved through the registry that crossed the same link, and
    # scale applied: the wire carried 3550, the database holds 355.0 K.
    assert {(row[0], row[1]) for row in rows} == {("car.rpm", "rpm"), ("car.coolant_temp", "K")}
    coolant = [row[2] for row in rows if row[0] == "car.coolant_temp"]
    assert coolant == [pytest.approx(355.0)] * 5

    with psycopg.connect(pit_dsn) as conn:
        cursor = conn.execute(
            "SELECT stream FROM ingest_cursor WHERE consumer = %s", (DURABLE,)
        ).fetchone()
    assert cursor[0] == PIT_STREAM


def test_the_sourced_stream_is_invisible_to_subject_lookup(leafnode_pair):
    """The one-line regression test for the whole class of bug.

    `nats-py` resolves a stream from a subject via `$JS.API.STREAM.NAMES`
    with a subject filter, and the server matches it against a stream's
    *declared* subjects. `TELE_VEHICLE` declares none — it must not, or it
    double-captures — so a bare subscribe finds nothing. Every pit service
    passes `stream=` from configuration because of this; if a future one
    forgets, this test is what says so.
    """

    async def exercise() -> None:
        await _create_vehicle_streams(leafnode_pair)
        await _provision_pit(leafnode_pair)
        client = await nats.connect(leafnode_pair.pit_url)
        try:
            js = client.jetstream()
            with pytest.raises(NotFoundError):
                await js.find_stream_name_by_subject(f"tele.{VEHICLE}.>")
            with pytest.raises(NotFoundError):
                await js.subscribe(f"tele.{VEHICLE}.>", ordered_consumer=True)

            # Named explicitly, the very same subject filter resolves.
            qualified = await js.subscribe(
                f"tele.{VEHICLE}.>",
                stream=PIT_STREAM,
                ordered_consumer=True,
                deliver_policy=api.DeliverPolicy.NEW,
            )
            await qualified.unsubscribe()

            info = await js.stream_info(PIT_STREAM)
            # The absent subjects are the cause, and are worth asserting
            # outright: a stream with both `sources` and `subjects` captures
            # every message twice (measured at exactly 2x).
            assert not info.config.subjects
            assert info.config.sources[0].name == "TELE"
            assert info.config.sources[0].external.api == "$JS.veh.API"
        finally:
            await client.close()

    asyncio.run(exercise())


def test_sourcing_captures_each_message_once(leafnode_pair):
    """Ten published once grow the pit stream by ten, not by twenty."""

    async def exercise() -> tuple[int, int]:
        await _create_vehicle_streams(leafnode_pair)
        await _provision_pit(leafnode_pair)
        client = await nats.connect(leafnode_pair.vehicle_url)
        try:
            js = client.jetstream()
            for index in range(10):
                await js.publish(f"tele.{VEHICLE}.can0", f"m{index}".encode())
        finally:
            await client.close()
        sourced = await _await_sourced(leafnode_pair, 10)
        # Give a double-capturing configuration time to reveal itself
        # rather than racing the assertion.
        await asyncio.sleep(2.0)
        return sourced, await _stream_messages(leafnode_pair.pit_url, PIT_STREAM)

    sourced, settled = asyncio.run(exercise())
    assert sourced == 10
    assert settled == 10


def test_sourcing_resumes_by_sequence_across_a_severed_link(leafnode_pair):
    """ADR 0002's whole point: a dropout is a catch-up, not a gap.

    Payloads are distinguishable raw bytes rather than encoded batches
    because the assertion here is about the *set and order* of messages
    surviving the cut, not about decoding — and an exact payload comparison
    catches the one-dropped-one-duplicated case that a count cannot.
    """

    async def exercise() -> list[bytes]:
        await _create_vehicle_streams(leafnode_pair)
        await _provision_pit(leafnode_pair)

        async def publish(start: int, count: int) -> None:
            client = await nats.connect(leafnode_pair.vehicle_url)
            try:
                js = client.jetstream()
                for index in range(start, start + count):
                    await js.publish(f"tele.{VEHICLE}.can0", f"msg-{index:04d}".encode())
            finally:
                await client.close()

        await publish(0, 20)
        assert await _await_sourced(leafnode_pair, 20) == 20

        await asyncio.to_thread(leafnode_pair.sever)

        # The vehicle keeps recording while the pit cannot hear it — this is
        # the car driving out of range, and TELE is the only durability it
        # has until the pit catches up.
        await publish(20, 200)
        assert await _stream_messages(leafnode_pair.pit_url, PIT_STREAM) == 20

        await asyncio.to_thread(leafnode_pair.restore)
        assert await _await_sourced(leafnode_pair, 220, timeout_s=120.0) == 220

        return await _read_all(leafnode_pair.pit_url, PIT_STREAM, f"tele.{VEHICLE}.>", expected=220)

    payloads = asyncio.run(exercise())
    expected = [f"msg-{index:04d}".encode() for index in range(220)]
    assert len(payloads) == len(expected), "message count changed across the dropout"
    assert len(set(payloads)) == len(payloads), "the pit stream holds duplicates"
    assert payloads == expected, "the pit stream is missing messages or reordered them"


def test_rtcm_is_captured_by_no_stream_on_either_side(leafnode_pair):
    """Core NATS, at-most-once, no backlog to catch up on (ADR 0006).

    Asserted rather than trusted to the config: a stream that captured
    `rtcm.>` would silently turn stale corrections into replayed ones, which
    degrades a fix rather than improving it.
    """

    async def exercise() -> None:
        await _create_vehicle_streams(leafnode_pair)
        await _provision_pit(leafnode_pair)
        for url in (leafnode_pair.vehicle_url, leafnode_pair.pit_url):
            client = await nats.connect(url)
            try:
                js = client.jetstream()
                await client.publish(f"rtcm.{VEHICLE}", b"\xd3\x00\x13rtcm-payload")
                await client.flush()
                with pytest.raises(NotFoundError):
                    await js.find_stream_name_by_subject(f"rtcm.{VEHICLE}")
            finally:
                await client.close()

    asyncio.run(exercise())


def test_a_session_published_at_the_pit_reaches_the_vehicle(leafnode_pair):
    """One direction, one stream, no mirroring, no reconciliation.

    session-control publishes into the vehicle's JetStream domain from a
    connection to its *own* local server; the leafnode carries it. The
    domain string is coupled across two files — `OPENLAPS_VEHICLE_JS_DOMAIN`
    at the pit and `jetstream { domain: ... }` in `nats/vehicle.conf` — and a
    mismatch is a silent publish into a domain that does not exist.
    """
    payload = b'{"session_id":"s-1","session_type":"practice","driver":"Driver A"}'

    async def exercise() -> bytes:
        await _create_vehicle_streams(leafnode_pair)
        pit_client = await nats.connect(leafnode_pair.pit_url)
        try:
            cross_domain = pit_client.jetstream(domain="veh")
            ack = await cross_domain.publish(f"cmd.{VEHICLE}.session", payload, timeout=10.0)
            assert ack.stream == "CMD"
        finally:
            await pit_client.close()

        # Read it the way the agent does (src/agent/publisher.py): last value
        # on the subject, which is what max_msgs_per_subject = 1 retains.
        vehicle_client = await nats.connect(leafnode_pair.vehicle_url)
        try:
            subscription = await vehicle_client.jetstream().subscribe(
                f"cmd.{VEHICLE}.session",
                stream="CMD",
                ordered_consumer=True,
                deliver_policy=api.DeliverPolicy.LAST,
            )
            message = await subscription.next_msg(timeout=10.0)
            await subscription.unsubscribe()
            return message.data
        finally:
            await vehicle_client.close()

    assert asyncio.run(exercise()) == payload


def test_the_live_decoder_bridges_from_the_sourced_stream(
    leafnode_pair, mosquitto_url: str, tmp_path: Path
):
    """The other service with a registry scan *and* a live subscription.

    Both of its `js.subscribe` calls have to name the stream, and only the
    deployed topology can tell you whether they do: against a single-server
    `TELE` that declares its subjects, a service that forgot passes every
    test it has.
    """
    config = tmp_path / "live-decoder.yaml"
    config.write_text(
        f"vehicle: {VEHICLE}\n"
        "defaults:\n"
        "  max_hz: 0\n"
        "  total_max_hz: 0\n"
        "channels:\n"
        "  - match: car.coolant_temp\n",
        encoding="utf-8",
    )
    mqtt = urlparse(mosquitto_url)

    async def exercise() -> list[dict]:
        await _provision_pit(leafnode_pair)
        catalog = _catalog(registry_seq=1)
        # Published *before* the decoder starts: the registry has to be
        # recovered from the sourced stream by the warm-up scan, which is
        # the call site that fails first when `stream=` is missing.
        await asyncio.to_thread(
            _publish,
            leafnode_pair.vehicle_url,
            catalog,
            [_tick(catalog, [("car.coolant_temp", 355.0)], epoch_unix_ms=BASE_EPOCH_MS)],
        )
        assert await _await_sourced(leafnode_pair, 2) == 2

        settings = LiveDecoderSettings(
            config_path=config,
            nats_url=leafnode_pair.pit_url,
            stream=PIT_STREAM,
            mqtt_host=mqtt.hostname or "127.0.0.1",
            mqtt_port=mqtt.port or 1883,
            health_port=0,
            config_poll_s=0.05,
        )
        decoder = LiveDecoder(settings)
        stop = asyncio.Event()
        received: list[dict] = []
        try:
            async with aiomqtt.Client(settings.mqtt_host, settings.mqtt_port) as subscriber:
                await subscriber.subscribe(f"openlaps/{VEHICLE}/#")
                task = asyncio.create_task(decoder.run(stop))
                await asyncio.wait_for(decoder.ready.wait(), timeout=20.0)
                await asyncio.wait_for(decoder.sink.connected.wait(), timeout=20.0)

                # DeliverPolicy.NEW: only batches published after the
                # subscription exists are seen, which is the point of a
                # live-only service.
                await asyncio.to_thread(
                    _publish,
                    leafnode_pair.vehicle_url,
                    catalog,
                    [
                        _tick(
                            catalog,
                            [("car.coolant_temp", 356.0)],
                            epoch_unix_ms=BASE_EPOCH_MS + 100,
                        )
                    ],
                )
                message = await asyncio.wait_for(anext(subscriber.messages), timeout=30.0)
                assert str(message.topic) == f"openlaps/{VEHICLE}/car.coolant_temp"
                received.append(json.loads(bytes(message.payload)))
        finally:
            stop.set()
            if "task" in locals():
                await asyncio.wait_for(task, timeout=15.0)
        return received

    payloads = asyncio.run(exercise())
    # Decoded against a generation recovered across the leafnode, with the
    # catalog's scale applied: the wire carried 3560, the gauge shows 356.0.
    assert payloads[0]["value"] == pytest.approx(356.0)
    assert payloads[0]["time"] == BASE_EPOCH_MS + 100
