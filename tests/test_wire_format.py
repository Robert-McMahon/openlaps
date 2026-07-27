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
    ]


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
