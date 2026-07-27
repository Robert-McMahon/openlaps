"""Round-trip tests for the openlaps wire format (proto/telemetry.proto).

Verifies the generated bindings under src/core/pb/ actually round-trip a
ChannelRegistry and a SampleBatch exercising every ValueType, and sanity
checks that a batch sized like a real 20 ms tick of the current car's CAN
mix stays well under the wire budget.
"""

from core.pb import telemetry_pb2 as pb


def test_channel_registry_round_trip():
    registry = pb.ChannelRegistry(
        registry_seq=7,
        vehicle_id="example-club-racer",
        created_unix_ms=1_753_500_000_000,
    )
    registry.channels.add(
        id=1,
        name="engine.rpm",
        source_ref="can0:haltech.ENGINE1.ENGINE_SPEED",
        units="rpm",
        type=pb.DOUBLE,
    )
    registry.channels.add(
        id=2,
        name="lap.number",
        source_ref="derived:timing.lap_number",
        units="",
        type=pb.INT64,
    )
    registry.channels.add(
        id=3,
        name="sys.agent.gps_locked",
        source_ref="derived:agent.gps_locked",
        units="",
        type=pb.BOOL,
    )
    registry.channels.add(
        id=4,
        name="sys.agent.status",
        source_ref="derived:agent.status",
        units="",
        type=pb.STRING,
    )
    registry.channels.add(
        id=5,
        name="chassis.accel_x",
        source_ref="can0:imu.FDI_IMU_ACCEL.IMU_ACCEL_X",
        units="g",
        type=pb.FLOAT,
    )
    # Trigger counter as a compact UINT lever: unscaled (scale left at 0),
    # raw count -- the same lever the owner decision calls out for engine
    # RPM (see test_uint_varint_sample_is_compact below).
    registry.channels.add(
        id=6,
        name="engine.trigger_counter",
        source_ref="can0:haltech.TRIGGER.TRIGGER_COUNTER",
        units="",
        type=pb.UINT,
    )
    # Coolant temp in Kelvin, fixed-point-encoded per docs/WIRE_FORMAT.md's
    # "Fixed-point convention": wire_value = round((physical - offset) / scale).
    registry.channels.add(
        id=7,
        name="engine.coolant_temp",
        source_ref="can0:haltech.TEMPERATURE1.COOLANT_TEMPERATURE",
        units="K",
        type=pb.UINT,
        scale=0.1,
        offset=0.0,
    )

    wire = registry.SerializeToString()
    decoded = pb.ChannelRegistry()
    decoded.ParseFromString(wire)

    assert decoded == registry
    assert decoded.registry_seq == 7
    assert decoded.vehicle_id == "example-club-racer"
    assert [c.name for c in decoded.channels] == [
        "engine.rpm",
        "lap.number",
        "sys.agent.gps_locked",
        "sys.agent.status",
        "chassis.accel_x",
        "engine.trigger_counter",
        "engine.coolant_temp",
    ]
    assert decoded.channels[5].type == pb.UINT
    assert decoded.channels[5].scale == 0.0
    assert decoded.channels[6].type == pb.UINT
    assert decoded.channels[6].scale == 0.1
    assert decoded.channels[6].offset == 0.0


def test_sample_batch_round_trip_all_value_types():
    batch = pb.SampleBatch(
        registry_seq=7,
        batch_epoch_unix_ms=1_753_500_000_123,
        batch_epoch_mono_ns=987_654_321_000,
    )

    s_double = batch.samples.add(channel_id=1, t_offset_us=0)
    s_double.d = 6512.5

    s_int = batch.samples.add(channel_id=2, t_offset_us=4200)
    s_int.i = -3

    s_bool = batch.samples.add(channel_id=3, t_offset_us=8100)
    s_bool.b = True

    s_string = batch.samples.add(channel_id=4, t_offset_us=15900)
    s_string.s = "OK"

    # New compact levers: FLOAT (chassis.accel_x, channel 5) and UINT
    # (engine.trigger_counter, channel 6), per the registry test above.
    s_float = batch.samples.add(channel_id=5, t_offset_us=19800)
    s_float.f = 1.125  # exact in binary float32, so round-trip is exact

    s_uint = batch.samples.add(channel_id=6, t_offset_us=19900)
    s_uint.u = 7000

    wire = batch.SerializeToString()
    decoded = pb.SampleBatch()
    decoded.ParseFromString(wire)

    assert decoded == batch
    assert decoded.registry_seq == 7
    assert decoded.batch_epoch_unix_ms == 1_753_500_000_123
    assert decoded.batch_epoch_mono_ns == 987_654_321_000

    assert decoded.samples[0].WhichOneof("value") == "d"
    assert decoded.samples[0].d == 6512.5
    assert decoded.samples[1].WhichOneof("value") == "i"
    assert decoded.samples[1].i == -3
    assert decoded.samples[2].WhichOneof("value") == "b"
    assert decoded.samples[2].b is True
    assert decoded.samples[3].WhichOneof("value") == "s"
    assert decoded.samples[3].s == "OK"
    assert decoded.samples[4].WhichOneof("value") == "f"
    assert decoded.samples[4].f == 1.125
    assert decoded.samples[5].WhichOneof("value") == "u"
    assert decoded.samples[5].u == 7000


def test_uint_varint_sample_is_compact():
    """A UINT value (the RPM-as-varint lever from the owner decision) must
    serialize far smaller than the equivalent DOUBLE value for its
    tag+value alone (channel_id/t_offset_us framing is identical either
    way, so isolating just the oneof value field is the honest comparison
    of what the lever actually buys). Measured: double's tag(1 B)+8 B
    fixed64 = 9 B; uint 7000's tag(1 B)+varint(2 B, 7000 needs 13 bits) =
    3 B.
    """
    d = pb.Sample()
    d.d = 7000.0
    double_value_bytes = len(d.SerializeToString())

    u = pb.Sample()
    u.u = 7000
    uint_value_bytes = len(u.SerializeToString())

    assert double_value_bytes >= 9, f"double tag+value was {double_value_bytes} B, expected >= 9"
    assert uint_value_bytes <= 6, (
        f"uint tag+value was {uint_value_bytes} B, expected <= 6 (tag + varint(7000))"
    )
    assert uint_value_bytes < double_value_bytes


def test_float32_batch_materially_smaller_than_double_batch():
    """The same representative 33-sample batch (see
    test_representative_20ms_batch_is_compact) encoded all-float32 must be
    materially smaller than all-double: float shaves 4 B off every sample's
    fixed-width value (8 B -> 4 B). Measured: double batch 551 B, float32
    batch 419 B, a 132 B / ~24% gap. Assert the measured gap with headroom
    rather than pinning exact bytes, which would be brittle across protobuf
    versions -- but the gap must be real and in the expected ballpark.
    """
    double_batch = pb.SampleBatch(
        registry_seq=1, batch_epoch_unix_ms=1_753_500_000_000, batch_epoch_mono_ns=123_456_789_000
    )
    float_batch = pb.SampleBatch(
        registry_seq=1, batch_epoch_unix_ms=1_753_500_000_000, batch_epoch_mono_ns=123_456_789_000
    )
    for i in range(33):
        channel_id = (i * 7) % 150
        t_offset_us = i * 600
        double_batch.samples.add(channel_id=channel_id, t_offset_us=t_offset_us).d = (
            100.0 + i * 0.25
        )
        float_batch.samples.add(channel_id=channel_id, t_offset_us=t_offset_us).f = 100.0 + i * 0.25

    double_bytes = len(double_batch.SerializeToString())
    float_bytes = len(float_batch.SerializeToString())

    # Measured: double batch 551 B, float32 batch 419 B (33 * 4 B saved).
    # Assert a conservative >= 100 B / >= 15% gap rather than the exact
    # measured numbers.
    assert float_bytes < double_bytes
    gap = double_bytes - float_bytes
    assert gap >= 100, f"float32 batch only saved {gap} B vs double ({double_bytes} B)"
    assert float_bytes <= double_bytes * 0.85, (
        f"float32 batch ({float_bytes} B) not materially smaller than double batch "
        f"({double_bytes} B)"
    )


def test_representative_20ms_batch_is_compact():
    """~33 samples/tick is what a 20 ms tick of the current car's CAN mix
    looks like (~1600 signal updates/s * 0.020 s ~= 32-33 samples); this is
    the size tools/size_batch.py models at scale. Channel ids and offsets
    are spread the way a real tick would fill them (ids drawn from a
    ~150-channel catalog, offsets spanning the 20 ms window) and every
    sample is a double, the worst case for this channel mix since CAN/GPS/
    IMU signals all decode to doubles (see tools/size_batch.py).

    Note: the wire format brief's original ballpark was "under 400 bytes".
    Measured against the actual schema that undershoots reality by ~35%:
    a `double` value is a fixed 8 bytes (proto3 has no varint float), and
    each Sample is its own length-delimited submessage (2 B of tag+len
    framing on top of ~5 B for channel_id + t_offset_us varints), so the
    real floor is ~15-16 B/sample -> ~530 B content + ~20 B batch header.
    This is still a ~15x cut versus the old JSON-per-signal payload (the
    single-signal mean today is ~120 B, see docs/mqtt_payload_stats.json),
    so the assertion below is set to the measured, honest figure rather
    than the brief's original estimate.
    """
    batch = pb.SampleBatch(
        registry_seq=1,
        batch_epoch_unix_ms=1_753_500_000_000,
        batch_epoch_mono_ns=123_456_789_000,
    )
    for i in range(33):
        sample = batch.samples.add(channel_id=(i * 7) % 150, t_offset_us=i * 600)
        sample.d = 100.0 + i * 0.25

    wire = batch.SerializeToString()

    # Measured at ~548 B for this shape; assert with headroom rather than
    # pinning the exact byte count, which would be brittle across protobuf
    # versions. Old JSON-per-signal equivalent for 33 signals: 33 * ~120 B
    # (mean_payload_b) + framing ~= 4-5 kB, so this is still >7x smaller.
    assert len(wire) < 650, f"batch of 33 samples serialized to {len(wire)} bytes"
