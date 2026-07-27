"""Tests for caller-driven protobuf telemetry batching."""

import pytest

from core.batcher import Batcher
from core.catalog import ChannelPolicy
from core.pb import telemetry_pb2 as pb
from core.samples import Sample


def _policy(
    name: str, value_type: int, *, scale: float = 0.0, offset: float = 0.0
) -> ChannelPolicy:
    return ChannelPolicy(
        name=name,
        value_type=value_type,
        rbe=None,
        live_hz=None,
        scale=scale,
        offset=offset,
    )


def test_tick_emits_one_serialized_batch_per_nonempty_source_class():
    batcher = Batcher(
        registry_seq=7,
        policies_by_id={
            1: _policy("car.rpm", pb.DOUBLE),
            2: _policy("position.speed", pb.DOUBLE),
        },
    )
    epoch_ms = 1_753_500_000_000
    epoch_mono_ns = 500_000_000_000
    batcher.add(
        "can0",
        1,
        Sample("can0:ecu.ENGINE.RPM", epoch_mono_ns + 5_250_000, epoch_ms + 5.25, 7000.0),
    )
    batcher.add(
        "serial0",
        2,
        Sample("serial0:gps.RMC.speed", epoch_mono_ns + 9_000_000, epoch_ms + 9.0, 123.5),
    )

    emitted = batcher.tick(epoch_ms, epoch_mono_ns)

    assert set(emitted) == {"can0", "serial0"}
    can_batch = pb.SampleBatch.FromString(emitted["can0"])
    assert can_batch.registry_seq == 7
    assert can_batch.batch_epoch_unix_ms == epoch_ms
    assert can_batch.batch_epoch_mono_ns == epoch_mono_ns
    assert can_batch.format_version == 1
    assert len(can_batch.samples) == 1
    assert can_batch.samples[0].channel_id == 1
    assert can_batch.samples[0].t_offset_us == 5_250
    assert epoch_ms + can_batch.samples[0].t_offset_us / 1000 == epoch_ms + 5.25


def test_empty_tick_emits_nothing():
    batcher = Batcher(registry_seq=7, policies_by_id={})

    assert batcher.tick(1_753_500_000_000, 500_000_000_000) == {}


@pytest.mark.parametrize("tick_ms", [10, 50])
def test_tick_length_accepts_documented_boundaries(tick_ms: int):
    Batcher(registry_seq=7, policies_by_id={}, tick_ms=tick_ms)


@pytest.mark.parametrize("tick_ms", [9, 51])
def test_tick_length_rejects_values_outside_documented_range(tick_ms: int):
    with pytest.raises(ValueError, match="tick_ms must be between 10 and 50 ms"):
        Batcher(registry_seq=7, policies_by_id={}, tick_ms=tick_ms)


def test_default_tick_accepts_sample_at_window_start():
    batcher = Batcher(registry_seq=7, policies_by_id={1: _policy("car.rpm", pb.DOUBLE)})
    batcher.add("can0", 1, Sample("source", 0, 1_000.0, 7_000.0))

    batch = pb.SampleBatch.FromString(batcher.tick(1_000, 0)["can0"])

    assert batch.samples[0].t_offset_us == 0


def test_tick_uses_monotonic_timestamp_for_membership_and_offset_when_wall_clock_slews():
    batcher = Batcher(registry_seq=7, policies_by_id={1: _policy("car.rpm", pb.DOUBLE)})
    epoch_mono_ns = 500_000_000_000
    batcher.add(
        "can0",
        1,
        Sample("source", epoch_mono_ns + 5_250_999, 900.0, 7_000.0),
    )

    batch = pb.SampleBatch.FromString(batcher.tick(1_000, epoch_mono_ns)["can0"])

    assert batch.samples[0].t_offset_us == 5_250


def test_tick_rejects_sample_exactly_at_next_tick_epoch():
    batcher = Batcher(registry_seq=7, policies_by_id={1: _policy("car.rpm", pb.DOUBLE)})
    batcher.add("can0", 1, Sample("source", 20_000_000, 1_000.0, 7_000.0))

    with pytest.raises(ValueError, match="falls outside tick window"):
        batcher.tick(1_000, 0)


def test_tick_truncates_in_window_nanoseconds_without_rounding_into_next_tick():
    batcher = Batcher(registry_seq=7, policies_by_id={1: _policy("car.rpm", pb.DOUBLE)})
    batcher.add("can0", 1, Sample("source", 19_999_999, 1_020.0, 7_000.0))

    batch = pb.SampleBatch.FromString(batcher.tick(1_000, 0)["can0"])

    assert batch.samples[0].t_offset_us == 19_999


def test_tick_rejects_sample_outside_configured_window():
    batcher = Batcher(
        registry_seq=7,
        policies_by_id={1: _policy("car.rpm", pb.DOUBLE)},
        tick_ms=10,
    )
    batcher.add("can0", 1, Sample("source", 10_001_000, 1_000.0, 7_000.0))

    with pytest.raises(
        ValueError,
        match=r"sample timestamp offset 10001 us falls outside tick window \[0, 10000\) us",
    ):
        batcher.tick(1_000, 0)


def test_failed_tick_discards_current_data_so_next_valid_tick_succeeds():
    batcher = Batcher(registry_seq=7, policies_by_id={1: _policy("car.rpm", pb.DOUBLE)})
    batcher.add("can0", 1, Sample("source", 0, 1_000.0, "malformed"))

    with pytest.raises(TypeError, match="car.rpm requires a numeric value"):
        batcher.tick(1_000, 0)

    batcher.add("can0", 1, Sample("source", 0, 1_000.0, 7_000.0))
    emitted = batcher.tick(1_000, 0)

    batch = pb.SampleBatch.FromString(emitted["can0"])
    assert len(batch.samples) == 1
    assert batch.samples[0].d == 7_000.0


def test_catalog_value_types_select_the_matching_protobuf_arms():
    policies = {
        1: _policy("car.analog", pb.DOUBLE),
        2: _policy("car.accel_x", pb.FLOAT),
        3: _policy("car.gear", pb.INT64),
        4: _policy("car.enabled", pb.BOOL),
        5: _policy("position.fix_quality", pb.STRING),
        6: _policy("car.counter", pb.UINT),
    }
    values = [1.25, 2.5, -3, True, "R", 42]
    batcher = Batcher(registry_seq=1, policies_by_id=policies)
    for channel_id, value in enumerate(values, start=1):
        batcher.add("can0", channel_id, Sample("source", 0, 1_000.0, value))

    batch = pb.SampleBatch.FromString(batcher.tick(1_000, 0)["can0"])

    assert [sample.WhichOneof("value") for sample in batch.samples] == [
        "d",
        "f",
        "i",
        "b",
        "s",
        "u",
    ]
    assert batch.samples[0].d == 1.25
    assert batch.samples[1].f == 2.5
    assert batch.samples[2].i == -3
    assert batch.samples[3].b is True
    assert batch.samples[4].s == "R"
    assert batch.samples[5].u == 42


def test_uint_fixed_point_encoding_round_trips_physical_value():
    policy = _policy("car.coolant_temp", pb.UINT, scale=0.1, offset=250.0)
    batcher = Batcher(registry_seq=1, policies_by_id={1: policy})
    batcher.add("can0", 1, Sample("source", 0, 1_000.0, 300.0))

    batch = pb.SampleBatch.FromString(batcher.tick(1_000, 0)["can0"])
    wire_value = batch.samples[0].u

    assert wire_value == 500
    assert wire_value * policy.scale + policy.offset == 300.0


@pytest.mark.parametrize(
    ("value", "scale", "offset"),
    [(-1, 0.0, 0.0), (249.9, 0.1, 250.0)],
)
def test_uint_encoding_rejects_negative_wire_values(
    value: float | int, scale: float, offset: float
):
    policy = _policy("car.counter", pb.UINT, scale=scale, offset=offset)
    batcher = Batcher(registry_seq=1, policies_by_id={1: policy})
    batcher.add("can0", 1, Sample("source", 0, 1_000.0, value))

    with pytest.raises(ValueError, match="car.counter encoded to a negative UINT value"):
        batcher.tick(1_000, 0)
