"""Ingest-writer unit tests: decode, row conversion, lap materialisation.

Batches are built with the real `Batcher` and decoded with the real
`RegistryCache`, so the encoding these tests assert against is the encoding
the vehicle actually produces — no hand-rolled protobuf.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from conftest import EXAMPLE_PROFILE

from core.batcher import Batcher
from core.catalog import (
    DEFAULT_DERIVED_CHANNELS,
    ChannelPolicy,
    DerivedChannel,
    build_runtime_catalog,
)
from core.pb import telemetry_pb2 as pb
from core.samples import Sample
from pit.ingest_writer.health import HealthState, serve_health
from pit.ingest_writer.laps import LapMaterialiser, LapRow, PitStatusUpdate
from pit.ingest_writer.store import TimescaleStore
from pit.ingest_writer.writer import IngestWriter, WriterSettings, rows_from_batch
from pit.registry_cache import REJECT_BAD_VERSION, REJECT_UNKNOWN_SEQ, RegistryCache

VEHICLE = "example-club-racer"
MS = 1_000_000  # ns
EPOCH_MS = 1_780_000_000_000


@pytest.fixture
def catalog(tmp_path: Path):
    """The example profile, plus the agent's own status channel."""
    return build_runtime_catalog(
        EXAMPLE_PROFILE,
        state_path=tmp_path / "registry-state.json",
        created_unix_ms=1,
        derived_channels=DEFAULT_DERIVED_CHANNELS
        + (DerivedChannel("sys.agent.status", pb.STRING),),
    )


def _batch(catalog, samples: list[tuple[str, str, object]], *, epoch_mono_ns: int = 0) -> bytes:
    """Serialize one SampleBatch through the real batcher."""
    batcher = Batcher(catalog.registry.registry_seq, catalog.policies_by_id, tick_ms=20)
    for source_class, source_ref, value in samples:
        channel_id, _ = catalog.source_map[source_ref]
        batcher.add(
            source_class,
            channel_id,
            Sample(source_ref, epoch_mono_ns, float(EPOCH_MS), value),
        )
    emitted = batcher.tick(EPOCH_MS, epoch_mono_ns)
    assert len(emitted) == 1
    return next(iter(emitted.values()))


def _keys(catalog) -> dict[tuple[int, int], int]:
    """A stand-in for what `channel_map` resolves: wire id -> channel_key."""
    seq = catalog.registry.registry_seq
    return {(seq, channel.id): 1000 + channel.id for channel in catalog.registry.channels}


def _named(catalog, rows) -> dict[str, tuple[float | None, str | None]]:
    by_key = {1000 + channel.id: channel.name for channel in catalog.registry.channels}
    return {by_key[row[1]]: (row[2], row[3]) for row in rows}


# --- decode + row conversion ------------------------------------------------


def test_numeric_bool_and_string_values_land_in_the_right_column(catalog):
    payload = _batch(
        catalog,
        [
            ("can0", "can0:haltech.ENGINE1.ENGINE_SPEED", 4500.0),
            ("can0", "can0:haltech.MISC4.BATTERY_VOLTAGE", 13.8),
            ("can0", "derived:sys.agent.status", '{"state":"running"}'),
        ],
    )
    cache = RegistryCache()
    cache.add_registry(catalog.registry)
    converted = rows_from_batch(cache.decode(payload), _keys(catalog))

    values = _named(catalog, converted.rows)
    assert values["car.rpm"] == (4500.0, None)
    assert values["car.battery_v"] == (13.8, None)
    assert values["sys.agent.status"] == (None, '{"state":"running"}')
    assert converted.agent_status is True
    assert converted.dropped == 0


def test_capture_time_survives_batching(catalog):
    payload = _batch(
        catalog,
        [("can0", "can0:haltech.ENGINE1.ENGINE_SPEED", 4500.0)],
        epoch_mono_ns=7 * MS,
    )
    cache = RegistryCache()
    cache.add_registry(catalog.registry)
    rows = rows_from_batch(cache.decode(payload), _keys(catalog)).rows
    assert rows[0][0] == datetime.fromtimestamp(EPOCH_MS / 1000.0, tz=UTC)


def test_scaled_uint_channels_are_stored_as_physical_values():
    """The fixed-point convention is the consumer's job, not the producer's.

    The example profile ships no `encode:` channel, so this builds the
    coolant-temperature example from docs/CATALOG.md directly — still
    encoded by the real `Batcher`, so the wire bytes are honest.
    """
    registry = pb.ChannelRegistry(registry_seq=9, vehicle_id=VEHICLE)
    registry.channels.add(
        id=1, name="car.coolant_temp", units="K", type=pb.UINT, scale=0.1, offset=0.0
    )
    policies = {1: ChannelPolicy(name="car.coolant_temp", value_type=pb.UINT, rbe=None, scale=0.1)}

    batcher = Batcher(9, policies, tick_ms=20)
    batcher.add("can0", 1, Sample("can0:test", 0, float(EPOCH_MS), 355.0))
    payload = batcher.tick(EPOCH_MS, 0)["can0"]

    decoded = pb.SampleBatch()
    decoded.ParseFromString(payload)
    # On the wire it really is a small varint, not an 8-byte double.
    assert decoded.samples[0].WhichOneof("value") == "u"
    assert decoded.samples[0].u == 3550

    cache = RegistryCache()
    cache.add_registry(registry)
    rows = rows_from_batch(cache.decode(payload), {(9, 1): 42}).rows
    assert rows[0][1] == 42
    assert rows[0][2] == pytest.approx(355.0)


def test_an_unknown_registry_seq_is_never_decoded(catalog):
    payload = _batch(catalog, [("can0", "can0:haltech.ENGINE1.ENGINE_SPEED", 4500.0)])
    cache = RegistryCache()  # deliberately empty
    batch, reason = cache.decode_or_reason(payload)
    assert batch is None
    assert reason == REJECT_UNKNOWN_SEQ
    assert cache.unknown_seq_batches == 1


def test_an_unknown_format_version_is_rejected_loudly(catalog, caplog):
    parsed = pb.SampleBatch()
    parsed.ParseFromString(_batch(catalog, [("can0", "can0:haltech.ENGINE1.ENGINE_SPEED", 1.0)]))
    parsed.format_version = 99
    cache = RegistryCache()
    cache.add_registry(catalog.registry)

    with caplog.at_level("ERROR"):
        batch, reason = cache.decode_or_reason(parsed.SerializeToString())
        # Logged once per version, however many batches arrive.
        cache.decode_or_reason(parsed.SerializeToString())
    assert (batch, reason) == (None, REJECT_BAD_VERSION)
    assert cache.bad_version_batches == 2
    assert sum("format_version=99" in record.message for record in caplog.records) == 1


def test_samples_for_channels_with_no_key_are_counted_not_written(catalog):
    payload = _batch(catalog, [("can0", "can0:haltech.ENGINE1.ENGINE_SPEED", 4500.0)])
    cache = RegistryCache()
    cache.add_registry(catalog.registry)
    converted = rows_from_batch(cache.decode(payload), {})
    assert converted.rows == []
    assert converted.unresolved == 1


# --- lap materialisation ----------------------------------------------------


def _event(event_type: str, *, time_s: float, lap: int, **extra: object) -> str:
    payload = {
        "type": event_type,
        "time": time_s,
        "line": "StartFinish",
        "lap_number": lap,
        "sector": 0,
        "split_time": 0.0,
        "lap_time": 0.0,
        "valid": True,
        "direction": "counterclockwise",
        "pit_status": "track",
        "lat": -31.6,
        "lon": 115.8,
    }
    payload.update(extra)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def test_sectors_buffer_until_their_lap_completes():
    laps = LapMaterialiser(VEHICLE)
    assert laps.observe(_event("sector_completed", time_s=100.0, lap=4, sector=1)) is None
    assert laps.observe(_event("sector_completed", time_s=140.0, lap=4, sector=2)) is None
    row = laps.observe(_event("sector_completed", time_s=208.8, lap=4, sector=3, split_time=68.8))
    assert row is None

    lap = laps.observe(_event("lap_completed", time_s=208.8, lap=4, lap_time=108.842))
    assert isinstance(lap, LapRow)
    assert lap.lap_number == 4
    assert lap.lap_time_s == pytest.approx(108.842)
    assert [sector.sector for sector in lap.sectors] == [1, 2, 3]
    assert lap.sectors[2].split_time_s == pytest.approx(68.8)
    # The buffer is emptied by the lap that claimed it.
    assert laps.observe(_event("lap_completed", time_s=320.0, lap=5)).sectors == ()


def test_session_stamps_are_carried_when_present_and_absent_when_not():
    laps = LapMaterialiser(VEHICLE)
    bare = laps.observe(_event("lap_completed", time_s=100.0, lap=1))
    assert (bare.session_id, bare.stint_number, bare.track_name) == (None, None, None)

    stamped = laps.observe(
        _event(
            "lap_completed",
            time_s=210.0,
            lap=2,
            session_id="s-1",
            driver="Driver A",
            stint_number=2,
            track_name="Wanneroo",
            session_type="practice",
        )
    )
    assert (stamped.session_id, stamped.stint_number, stamped.track_name) == (
        "s-1",
        2,
        "Wanneroo",
    )


def test_a_lap_number_regression_discards_the_stale_sector_buffer():
    """An agent restart or track switch rebuilds the engine, resetting numbering."""
    laps = LapMaterialiser(VEHICLE)
    laps.observe(_event("sector_completed", time_s=100.0, lap=7, sector=1))
    laps.observe(_event("sector_completed", time_s=140.0, lap=7, sector=2))

    lap = laps.observe(_event("lap_completed", time_s=400.0, lap=1, lap_time=107.0))
    assert lap.sectors == ()
    assert laps.engine_restarts == 1


def test_the_run_after_a_regression_keeps_its_own_sectors():
    """The regression fires once, not on every event below the old peak."""
    laps = LapMaterialiser(VEHICLE)
    for lap_number in (5, 6, 7):
        laps.observe(
            _event("sector_completed", time_s=lap_number * 100.0, lap=lap_number, sector=1)
        )
        laps.observe(_event("lap_completed", time_s=lap_number * 100.0 + 50, lap=lap_number))

    # Agent restarts: numbering returns to 1, well below the previous peak.
    laps.observe(_event("sector_completed", time_s=900.0, lap=1, sector=1, split_time=40.0))
    laps.observe(_event("sector_completed", time_s=940.0, lap=1, sector=2, split_time=40.0))
    lap = laps.observe(_event("lap_completed", time_s=1000.0, lap=1, lap_time=107.0))

    assert laps.engine_restarts == 1, "the restart was counted once per event, not once"
    assert [sector.sector for sector in lap.sectors] == [1, 2], (
        "sectors buffered after the restart were discarded by a re-fired regression"
    )


def test_pit_events_become_status_updates():
    laps = LapMaterialiser(VEHICLE)
    update = laps.observe(_event("pit_entry", time_s=500.0, lap=9, pit_status="pit"))
    assert isinstance(update, PitStatusUpdate)
    assert update.pit_status == "pit"
    assert update.at == datetime.fromtimestamp(500.0, tz=UTC)


def test_malformed_and_unknown_events_are_counted_not_raised(caplog):
    laps = LapMaterialiser(VEHICLE)
    with caplog.at_level("WARNING"):
        assert laps.observe("not json at all") is None
        assert laps.observe('{"type":"lap_completed"}') is None  # no time/lap_number
        assert laps.observe("[1,2,3]") is None
    assert laps.malformed_events == 3

    # An unknown type is tolerated: the schema is expected to grow.
    assert laps.observe(_event("safety_car_deployed", time_s=10.0, lap=1)) is None
    assert laps.unknown_event_types == {"safety_car_deployed"}


def test_unknown_keys_are_tolerated():
    laps = LapMaterialiser(VEHICLE)
    lap = laps.observe(_event("lap_completed", time_s=100.0, lap=1, weather="wet", tyre_set=3))
    assert isinstance(lap, LapRow)
    assert lap.lap_number == 1


def test_the_sector_buffer_is_bounded():
    """A lap that never completes must not grow the buffer without limit."""
    laps = LapMaterialiser(VEHICLE)
    for index in range(500):
        laps.observe(_event("sector_completed", time_s=float(index), lap=1, sector=index))
    lap = laps.observe(_event("lap_completed", time_s=600.0, lap=1))
    assert 0 < len(lap.sectors) <= 64


# --- flush failure handling -------------------------------------------------


class _StubStore(TimescaleStore):
    """A store whose flushes fail on demand, for the paths docker can't reach."""

    def __init__(self, *failures: Exception | None) -> None:
        super().__init__("postgresql://unused")
        self.failures = list(failures)
        self.writes: list[int] = []

    async def flush(self, **kwargs) -> None:
        self.writes.append(len(kwargs["laps"]))
        failure = self.failures.pop(0) if self.failures else None
        if failure is not None:
            raise failure

    async def connect(self) -> None:
        self.connects += 1

    async def close(self) -> None:
        return None


def _loaded_writer(store: TimescaleStore) -> object:
    """A writer holding one flush's worth of work, wired to ``store``."""
    settings = WriterSettings(
        nats_url="nats://unused", vehicle_id=VEHICLE, dsn="postgresql://unused", health_port=0
    )
    writer = IngestWriter(settings, store=store)
    writer._rows = [(datetime.now(tz=UTC), 1, 4500.0, None)]
    writer._lap_rows = [
        LapRow(
            vehicle_id=VEHICLE,
            session_id=None,
            stint_number=None,
            track_name="Wanneroo",
            lap_number=4,
            crossed_at=datetime.now(tz=UTC),
            lap_time_s=108.8,
            valid=True,
            pit_status="track",
            direction="counterclockwise",
            sectors=(),
        )
    ]
    writer._max_seq = 99
    # Shut down immediately if a reconnect is attempted: these tests assert
    # what the flush does, not how long it waits.
    writer._stop.set()
    return writer


def test_a_connection_lost_while_retrying_a_rejected_flush_keeps_everything():
    """OperationalError is not a refusal, even raised from the recovery path.

    Treating it as one would ack the messages and advance the cursor over
    rows no transaction ever committed — the exact loss the cursor exists
    to prevent.
    """
    store = _StubStore(
        psycopg.errors.CheckViolation("laps violates a constraint"),
        psycopg.OperationalError("connection is closed"),
    )
    writer = _loaded_writer(store)

    assert asyncio.run(writer._flush()) is False
    assert store.writes == [1, 0], "expected the full write then the samples-only retry"
    assert writer.cursor == 0, "the cursor advanced over rows that never committed"
    assert writer._rows, "the buffer was discarded despite nothing being written"
    assert writer.health.dropped_flushes == 0
    assert writer.health.db_errors == 1


def test_laps_refused_by_the_schema_are_not_counted_as_written():
    store = _StubStore(psycopg.errors.CheckViolation("laps violates a constraint"))
    writer = _loaded_writer(store)

    assert asyncio.run(writer._flush()) is True
    assert store.writes == [1, 0]
    assert writer.health.data_errors == 1
    assert writer.health.laps_written == 0, "a lap the database refused was counted as written"
    assert writer.health.laps_dropped == 1
    # The samples themselves did land, so the cursor moves.
    assert writer.cursor == 99
    assert writer.health.rows_written == 1


def test_a_flush_the_database_will_never_accept_is_dropped_not_retried_forever():
    store = _StubStore(
        psycopg.errors.CheckViolation("laps violates a constraint"),
        psycopg.errors.DataError("timestamp out of range"),
    )
    writer = _loaded_writer(store)

    assert asyncio.run(writer._flush()) is False
    assert writer.health.dropped_flushes == 1
    assert writer.health.laps_dropped == 1
    assert writer._rows == [], "a poison flush must not stay buffered forever"
    assert writer.cursor == 99


# --- health -----------------------------------------------------------------


def test_lag_is_measured_against_the_best_transit_seen():
    """Vehicle and pit monotonic clocks share no epoch; only deltas mean anything."""
    state = HealthState()
    base = 5_000_000_000
    # First batch establishes the baseline: 5 ms of transit is "as good as it gets".
    state.observe_batch(base, EPOCH_MS, base + 5 * MS)
    assert state.lag_ms == 0.0

    # A batch 100 ms further behind reads as 100 ms of lag, whatever the
    # absolute offset between the two hosts' clocks happens to be.
    state.observe_batch(base + 20 * MS, EPOCH_MS, base + 125 * MS)
    assert state.lag_ms == pytest.approx(100.0)

    # And a better one re-baselines rather than reading as negative lag.
    state.observe_batch(base + 40 * MS, EPOCH_MS, base + 42 * MS)
    assert state.lag_ms == 0.0


def test_agent_status_staleness_is_reported():
    """The pit watching this replaces the predecessor's MQTT last will."""
    state = HealthState()
    assert state.agent_status_age_s is None
    state.note_agent_status()
    assert state.agent_status_age_s == pytest.approx(0.0, abs=0.5)


def test_health_endpoint_serves_the_snapshot():
    state = HealthState()
    state.observe_flush(rows=17, stream_seq=42)
    state.roll()
    server = serve_health(state, 0, host="127.0.0.1")
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(f"{base}/health", timeout=5) as response:
            payload = json.loads(response.read())
        assert payload["rows_written"] == 17
        assert payload["last_stream_seq"] == 42
        assert payload["stalled"] is False
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"{base}/", timeout=5)
    finally:
        server.shutdown()


# --- settings ---------------------------------------------------------------


def test_settings_from_env_reads_the_documented_variables():
    settings = WriterSettings.from_env(
        {
            "OPENLAPS_VEHICLE_ID": VEHICLE,
            "OPENLAPS_NATS_URL": "nats://pit:4222",
            "TIMESCALE_DSN": "postgresql://pit/openlaps",
            "OPENLAPS_INGEST_FLUSH_MS": "50",
            "OPENLAPS_INGEST_BATCH_ROWS": "10",
        }
    )
    assert settings.vehicle_id == VEHICLE
    assert settings.flush_interval_s == 0.05
    assert settings.batch_rows == 10
    assert settings.stream == "TELE_VEHICLE"


def test_settings_require_a_vehicle_id():
    with pytest.raises(ValueError, match="OPENLAPS_VEHICLE_ID"):
        WriterSettings.from_env({"TIMESCALE_DSN": "postgresql://x"})
