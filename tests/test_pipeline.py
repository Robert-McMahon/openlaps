"""Pipeline tests: mapping, pre-RBE tap, RBE, tick windowing, batch decode."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from agent.pipeline import DERIVED_SOURCE_CLASS, Pipeline
from core.catalog import build_runtime_catalog
from core.pb import telemetry_pb2 as pb
from core.samples import Sample

MS = 1_000_000  # ns
T0 = 10_000 * MS


def _write_profile(root: Path) -> Path:
    (root / "dbcs").mkdir(parents=True)
    (root / "dbcs" / "ecu.dbc").write_text('VERSION ""\n', encoding="utf-8")
    (root / "vehicle.yaml").write_text(
        """vehicle: {id: pipecar}
buses:
  - name: can0
    interface: can0
    bitrate: 500000
    dbcs: [{device: ecu, file: dbcs/ecu.dbc}]
serial:
  - name: serial0
    port: /dev/ttyUSB0
    baud: 115200
    decoder: nmea
    driver:
      name: um980
      config: {rate_hz: 10, sentences: [RMC], configure_on_start: false}
host: {enabled: false, interval: 5s}
""",
        encoding="utf-8",
    )
    (root / "catalog.yaml").write_text(
        """channels:
  car.rpm: { from: "can0:ecu.ENGINE.RPM", units: rpm }
  car.temp: { from: "can0:ecu.ENGINE.TEMP", units: K, rbe: {deadband: 0.5, max_interval: 100ms} }
  car.count: { from: "can0:ecu.ENGINE.COUNT", type: int }
  position.lat: { from: "serial0:um980.RMC.lat", units: deg }
  position.lon: { from: "serial0:um980.RMC.lon", units: deg }
  position.time_unix_ms: { from: "serial0:um980.RMC.time_unix_ms", units: ms }
apps: {}
""",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def catalog(tmp_path: Path):
    return build_runtime_catalog(
        _write_profile(tmp_path / "profile"),
        state_path=tmp_path / "registry-state.json",
        created_unix_ms=1,
    )


def _wall(t_mono_ns: int) -> float:
    return 1_780_000_000_000.0 + t_mono_ns / 1e6


def _decode(payload: bytes) -> pb.SampleBatch:
    batch = pb.SampleBatch()
    batch.ParseFromString(payload)
    return batch


def test_mapped_sample_round_trips_with_exact_capture_time(catalog):
    pipeline = Pipeline(catalog, tick_ms=20)
    sample = Sample("can0:ecu.ENGINE.RPM", T0 + 7 * MS, _wall(T0 + 7 * MS), 4321.5)
    pipeline.ingest("can0", sample)
    batches = pipeline.flush(_wall)

    assert [batch.source_class for batch in batches] == ["can0"]
    tick = batches[0]
    assert tick.msg_id == f"can0:{tick.epoch_unix_ms}"
    decoded = _decode(tick.payload)
    assert decoded.registry_seq == catalog.registry.registry_seq
    assert decoded.format_version == 1
    assert decoded.batch_epoch_mono_ns == sample.t_mono_ns
    assert len(decoded.samples) == 1
    wire_sample = decoded.samples[0]
    assert wire_sample.channel_id == catalog.channel_ids["car.rpm"]
    assert wire_sample.d == 4321.5
    capture_ms = decoded.batch_epoch_unix_ms + wire_sample.t_offset_us / 1000.0
    assert capture_ms == pytest.approx(sample.t_wall_ms, abs=0.5)


def test_unmapped_refs_are_counted_dropped_and_logged_once(catalog, caplog):
    pipeline = Pipeline(catalog, tick_ms=20)
    with caplog.at_level(logging.INFO, logger="agent.pipeline"):
        for _ in range(3):
            pipeline.ingest("can0", Sample("can0:ecu.ENGINE.IGNORED", T0, _wall(T0), 1.0))
    assert pipeline.unmapped_refs == 3
    assert pipeline.flush(_wall) == []
    mentions = [record for record in caplog.records if "IGNORED" in record.message]
    assert len(mentions) == 1


def test_rbe_deadband_suppresses_and_heartbeat_forces(catalog):
    pipeline = Pipeline(catalog, tick_ms=20)
    ref = "can0:ecu.ENGINE.TEMP"
    pipeline.ingest("can0", Sample(ref, T0, _wall(T0), 300.0))
    pipeline.ingest("can0", Sample(ref, T0 + 10 * MS, _wall(T0 + 10 * MS), 300.2))  # in deadband
    assert pipeline.rbe_suppressed == 1
    # max_interval 100ms forces a heartbeat even though the value is frozen.
    pipeline.ingest("can0", Sample(ref, T0 + 120 * MS, _wall(T0 + 120 * MS), 300.2))
    batches = pipeline.flush(_wall)
    values = [
        sample.d
        for batch in batches
        for sample in _decode(batch.payload).samples
        if sample.channel_id == catalog.channel_ids["car.temp"]
    ]
    assert values == [300.0, 300.2]


def test_backlog_is_partitioned_into_tick_windows(catalog):
    pipeline = Pipeline(catalog, tick_ms=20)
    ref = "can0:ecu.ENGINE.RPM"
    stamps = [T0, T0 + 5 * MS, T0 + 25 * MS, T0 + 60 * MS]
    for t in stamps:
        pipeline.ingest("can0", Sample(ref, t, _wall(t), float(t)))
    batches = pipeline.flush(_wall)

    assert [batch.epoch_mono_ns for batch in batches] == [T0, T0 + 25 * MS, T0 + 60 * MS]
    first = _decode(batches[0].payload)
    assert [sample.t_offset_us for sample in first.samples] == [0, 5000]
    for batch in batches:
        decoded = _decode(batch.payload)
        for sample in decoded.samples:
            assert 0 <= sample.t_offset_us < 20_000
    # Same source-class ticks get distinct dedupe ids.
    assert len({batch.msg_id for batch in batches}) == 3


def test_empty_flush_emits_nothing(catalog):
    assert Pipeline(catalog, tick_ms=20).flush(_wall) == []


class _StubTimingApp:
    """Duck-typed stand-in: subscribes to one channel, emits one derived sample."""

    def __init__(self, subscribed_id: int):
        self.subscribed_channel_ids = frozenset({subscribed_id})
        self.seen: list[Sample] = []

    def observe(self, channel_id: int, sample: Sample) -> list[Sample]:
        self.seen.append(sample)
        return [Sample("derived:lap.number", sample.t_mono_ns, sample.t_wall_ms, 3)]


def test_timing_tap_is_pre_rbe_and_derived_reenters_at_the_mapper(catalog, tmp_path):
    # Rebuild the same profile but with an aggressive RBE policy on lat so
    # the wire path suppresses what the timing tap must still see.
    profile = tmp_path / "profile2"
    _write_profile(profile)
    text = (profile / "catalog.yaml").read_text(encoding="utf-8")
    text = text.replace(
        'position.lat: { from: "serial0:um980.RMC.lat", units: deg }',
        'position.lat: { from: "serial0:um980.RMC.lat", units: deg, rbe: {min_interval: 10s} }',
    )
    (profile / "catalog.yaml").write_text(text, encoding="utf-8")
    rebuilt = build_runtime_catalog(
        profile, state_path=tmp_path / "registry-state2.json", created_unix_ms=1
    )

    stub = _StubTimingApp(rebuilt.channel_ids["position.lat"])
    pipeline = Pipeline(rebuilt, tick_ms=20, timing_app=stub)
    for index in range(3):
        t = T0 + index * 10 * MS
        pipeline.ingest("serial0", Sample("serial0:um980.RMC.lat", t, _wall(t), -31.0))
    batches = pipeline.flush(_wall)

    # The tap saw every fix despite the 10 s rate cap on the wire path...
    assert len(stub.seen) == 3
    by_class = {batch.source_class: batch for batch in batches}
    assert set(by_class) >= {DERIVED_SOURCE_CLASS}
    # ...the wire path kept only the first fix...
    lat_id = rebuilt.channel_ids["position.lat"]
    lat_samples = [
        sample
        for batch in batches
        for sample in _decode(batch.payload).samples
        if sample.channel_id == lat_id
    ]
    assert len(lat_samples) == 1
    # ...and derived samples re-entered the pipeline in the derived class.
    derived_ids = {
        sample.channel_id for sample in _decode(by_class[DERIVED_SOURCE_CLASS].payload).samples
    }
    assert rebuilt.channel_ids["lap.number"] in derived_ids


def test_encode_failure_discards_one_window_and_recovers(catalog):
    pipeline = Pipeline(catalog, tick_ms=20)
    # car.count is INT64 unscaled: a fractional float cannot encode.
    pipeline.ingest("can0", Sample("can0:ecu.ENGINE.COUNT", T0, _wall(T0), 3.7))
    pipeline.ingest("can0", Sample("can0:ecu.ENGINE.RPM", T0 + MS, _wall(T0 + MS), 900.0))
    assert pipeline.flush(_wall) == []
    assert pipeline.encode_failures == 1
    # The pipeline thread survives and the next tick is clean.
    pipeline.ingest("can0", Sample("can0:ecu.ENGINE.RPM", T0 + 30 * MS, _wall(T0 + 30 * MS), 901.0))
    assert len(pipeline.flush(_wall)) == 1
