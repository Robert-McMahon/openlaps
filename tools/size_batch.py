#!/usr/bin/env python3
"""Size the openlaps wire format against the current car's real signal mix.

Builds actual `SampleBatch` protobuf messages (see proto/telemetry.proto)
for a given batching tick length and prints on-wire size figures: samples/s,
batches/s, mean batch bytes, bytes/s, kbit/s — with and without an
approximation of NATS framing overhead.

This replaces the hand-rolled protobuf byte-math in the old repo's
`docs/mqtt_bandwidth.py` (section 7) with real serialized messages: instead
of computing tag/varint lengths by hand, we build one SampleBatch per
source-class per tick using the generated bindings and just measure
`len(batch.SerializeToString())`.

Signal mix modelled (see --stats):
  - CAN: every signal update in the stats file's `can_rows` becomes one
    double Sample (rate = frame_rate_hz * signals_per_frame per message).
  - GPS: 50 Hz, 6 doubles per fix (lat, lon, speed, heading, + 2 status
    doubles).
  - IMU: 100 Hz, 10 doubles per frame.

Usage:
    uv run python tools/size_batch.py
    uv run python tools/size_batch.py --tick-ms 10
    uv run python tools/size_batch.py --stats path/to/mqtt_payload_stats.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.pb import telemetry_pb2 as pb  # noqa: E402

DEFAULT_STATS_PATH = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "mqtt_payload_stats.json"
)

GPS_RATE_HZ = 50.0
GPS_DOUBLES_PER_FIX = 6  # lat, lon, speed, heading + 2 status doubles

IMU_RATE_HZ = 100.0
IMU_DOUBLES_PER_FRAME = 10

# Old JSON-over-MQTT offered-load figure this replaces (docs/mqtt_bandwidth.py,
# "As configured today" scenario: individual per-signal JSON topics, QoS 1,
# GPS 50 Hz, IMU live 10 Hz, current CAN mix).
OLD_JSON_MQTT_MBIT_S = 2.4

# Rough NATS-over-the-wire overhead per published message: subject string
# (tele.<vehicle>.<source-class>, typically 20-30 B) plus NATS protocol
# framing (PUB <subject> <sid> <#bytes>\r\n ... \r\n) and JetStream ack
# machinery amortized per message. 20 B is a conservative planning figure,
# not a byte-exact measurement (unlike the protobuf sizes above, which are
# exact).
NATS_FRAMING_OVERHEAD_B = 20


@dataclass
class ChannelStream:
    """One kind of channel-producing source folded into the tick model."""

    label: str
    updates_per_s: float
    doubles_per_update: int = 1


def load_can_streams(stats_path: Path) -> list[ChannelStream]:
    stats = json.loads(stats_path.read_text())
    streams = []
    for row in stats["can_rows"]:
        updates_per_s = row["frame_rate_hz"] * row["signals_per_frame"]
        streams.append(ChannelStream(label=f"can:{row['message']}", updates_per_s=updates_per_s))
    return streams


def can_summary(stats_path: Path) -> tuple[int, float, float]:
    """(message count, total frames/s, total signal updates/s) straight from
    the stats file, for the human-readable summary line."""
    stats = json.loads(stats_path.read_text())
    rows = stats["can_rows"]
    frames_per_s = sum(r["frame_rate_hz"] for r in rows)
    updates_per_s = sum(r["frame_rate_hz"] * r["signals_per_frame"] for r in rows)
    return len(rows), frames_per_s, updates_per_s


def build_streams(stats_path: Path) -> list[ChannelStream]:
    streams = load_can_streams(stats_path)
    streams.append(
        ChannelStream(
            label="gps", updates_per_s=GPS_RATE_HZ, doubles_per_update=GPS_DOUBLES_PER_FIX
        )
    )
    streams.append(
        ChannelStream(
            label="imu", updates_per_s=IMU_RATE_HZ, doubles_per_update=IMU_DOUBLES_PER_FRAME
        )
    )
    return streams


def samples_in_tick(streams: list[ChannelStream], tick_s: float) -> int:
    """Expected sample count per tick, summed across every channel stream."""
    total = 0.0
    for s in streams:
        total += s.updates_per_s * s.doubles_per_update * tick_s
    return round(total)


def build_sample_batch(n_samples: int, tick_us: int, registry_seq: int = 1) -> bytes:
    """Build one representative SampleBatch of n_samples doubles and return
    its serialized bytes. Channel ids and offsets are spread the way a real
    tick would fill them, matching tests/test_wire_format.py's model."""
    batch = pb.SampleBatch(
        registry_seq=registry_seq,
        batch_epoch_unix_ms=1_753_500_000_000,
        batch_epoch_mono_ns=123_456_789_000,
    )
    n_channels = max(1, min(n_samples, 400))
    for i in range(n_samples):
        offset_us = 0 if n_samples <= 1 else round(i * tick_us / n_samples)
        sample = batch.samples.add(channel_id=i % n_channels, t_offset_us=offset_us)
        sample.d = 100.0 + (i % 997) * 0.25
    return batch.SerializeToString()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--stats",
        type=Path,
        default=DEFAULT_STATS_PATH,
        help=f"Path to a mqtt_payload_stats.json-style file (default: {DEFAULT_STATS_PATH})",
    )
    parser.add_argument(
        "--tick-ms",
        type=float,
        default=20.0,
        help="Batching tick length in milliseconds (default: 20)",
    )
    args = parser.parse_args()

    if not args.stats.exists():
        parser.error(f"stats file not found: {args.stats}")

    streams = build_streams(args.stats)
    tick_s = args.tick_ms / 1000.0
    tick_us = round(args.tick_ms * 1000)

    total_samples_per_s = sum(s.updates_per_s * s.doubles_per_update for s in streams)
    n_samples_per_tick = samples_in_tick(streams, tick_s)
    ticks_per_s = 1.0 / tick_s

    # One SampleBatch per source-class per tick in the real system; for
    # sizing purposes we model the whole mix as if it were one batch per
    # tick (a reasonable stand-in — per-source-class batches share the same
    # per-sample byte cost, and splitting them only adds a fixed ~20 B
    # header per extra batch, which the NATS framing overhead below already
    # approximates for a small number of source-classes).
    wire = build_sample_batch(n_samples_per_tick, tick_us)
    batch_bytes = len(wire)

    batches_per_s = ticks_per_s
    bytes_per_s = batch_bytes * batches_per_s
    kbit_per_s = bytes_per_s * 8 / 1000

    framed_batch_bytes = batch_bytes + NATS_FRAMING_OVERHEAD_B
    framed_bytes_per_s = framed_batch_bytes * batches_per_s
    framed_kbit_per_s = framed_bytes_per_s * 8 / 1000

    n_can_msgs, can_frames_per_s, can_updates_per_s = can_summary(args.stats)

    print(f"Stats file:        {args.stats}")
    print(f"Tick length:       {args.tick_ms:g} ms ({batches_per_s:.0f} batches/s)")
    print(
        f"CAN:               {n_can_msgs} messages, {can_frames_per_s:.1f} frames/s, "
        f"{can_updates_per_s:.1f} signal updates/s"
    )
    print(
        f"GPS:               {GPS_RATE_HZ:.0f} Hz x {GPS_DOUBLES_PER_FIX} doubles = "
        f"{GPS_RATE_HZ * GPS_DOUBLES_PER_FIX:.0f} samples/s"
    )
    print(
        f"IMU:               {IMU_RATE_HZ:.0f} Hz x {IMU_DOUBLES_PER_FRAME} doubles = "
        f"{IMU_RATE_HZ * IMU_DOUBLES_PER_FRAME:.0f} samples/s"
    )
    print()
    print(f"Total samples/s:   {total_samples_per_s:,.0f}")
    print(f"Samples/tick:      {n_samples_per_tick:,d}")
    print(f"Batches/s:         {batches_per_s:,.0f}")
    print(f"Mean batch bytes:  {batch_bytes:,d} B  (protobuf SampleBatch, no framing)")
    print(f"Bytes/s:           {bytes_per_s:,.0f} B/s")
    print(f"kbit/s:            {kbit_per_s:,.1f} kbit/s")
    print()
    print(f"With NATS framing (~{NATS_FRAMING_OVERHEAD_B} B/msg: subject + proto headers):")
    print(f"  Framed batch bytes: {framed_batch_bytes:,d} B")
    print(f"  Bytes/s:            {framed_bytes_per_s:,.0f} B/s")
    print(f"  kbit/s:             {framed_kbit_per_s:,.1f} kbit/s")
    print()
    mbit_s = framed_kbit_per_s / 1000
    ratio = OLD_JSON_MQTT_MBIT_S / mbit_s if mbit_s else float("inf")
    print(
        f"Old JSON-over-MQTT offered load (docs/mqtt_bandwidth.py, "
        f'"as configured today"): ~{OLD_JSON_MQTT_MBIT_S:.1f} Mbit/s'
    )
    print(
        f"openlaps protobuf/NATS offered load at {args.tick_ms:g} ms tick: "
        f"~{mbit_s:.3f} Mbit/s  ({ratio:,.0f}x smaller)"
    )


if __name__ == "__main__":
    main()
