#!/usr/bin/env python3
"""Replay recorded telemetry through the real agent pipeline into JetStream.

Feeds recorded inputs through the **real** `CanCollector` / `SerialCollector`,
the **real** `Pipeline` (mapper, RBE, the real `LapTimingApp` when the profile
configures one, and the batcher), and publishes with the **real**
`JetStreamPublisher` -- this is the same code the car runs, not a parallel
path that happens to look similar. Built so Phase 4's bench validation (and
anyone else who wants to see the pit stack move) doesn't need a car
(`docs/plan/PHASE3.md` P3.7).

Three source types compose freely and interleave naturally through the
pipeline's own tick-window batching:

- ``--candump``: a candump-format CAN log, decoded against the profile's
  first bus and its DBCs.
- ``--nmea``: a file of raw ``$..RMC`` sentences, fed straight into the
  profile's first serial source's NMEA decoder.
- ``--gps-trace``: a ``t_s,lat,lon,speed_kmh,heading_deg`` CSV (a slice
  extracted from the predecessor's ``gps.lp`` dump -- see
  ``tests/fixtures/gps/README.md``), encoded to synthetic RMC sentences and
  fed through the same serial source.

Batch *contents* (which channels land in which tick) are correct regardless
of how fast this tool runs, because `Pipeline.flush` partitions purely by
each sample's own capture time. What ``--rate`` controls is *publish*
pacing: batches are handed to the publisher spaced to reproduce the
source's recorded cadence divided by ``--rate``, so a consumer downstream
sees traffic shaped like the real link (1.0 = wall clock, higher =
accelerated, 0 = unpaced). Unpaced still means *bounded*: publishing waits
on the publisher's own lag, because `JetStreamPublisher.submit` sheds
oldest-first past its byte budget and a replay that outran the local
server would otherwise lose batches silently.

Batches stream out of the pipeline rather than accumulating, so the input
may be an entire event: P4.6 replays 24.7 h and 1.77 M GPS fixes through
this tool (`docs/bench/timing-parity.md`).

Examples:

    uv run tools/replay.py --candump tests/fixtures/candump/candump-sample.log
    uv run tools/replay.py --rate 50 --loop
    uv run tools/replay.py --nmea '' --gps-trace ''   # CAN only
"""

from __future__ import annotations

import argparse
import csv
import itertools
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import can
from _bench import (
    DEFAULT_PROFILE,
    DEFAULT_SERVER,
    build_timing_app,
    load_catalog,
    make_publisher,
    publish_paced,
    rmc_sentence,
    wait_connected,
)

from agent.clock import SteeredClock
from agent.pipeline import Pipeline, TickBatch
from collectors.can import CanCollector
from collectors.clock import WallClock
from collectors.serial.transport import SerialCollector
from core.catalog import RuntimeCatalog
from core.config import ProfileConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CANDUMP = REPO_ROOT / "tests" / "fixtures" / "candump" / "candump-sample.log"
DEFAULT_NMEA = REPO_ROOT / "tests" / "fixtures" / "nmea" / "rmc-samples.nmea"
DEFAULT_GPS_TRACE = REPO_ROOT / "tests" / "fixtures" / "gps" / "wanneroo-trace.csv"

# The NMEA-file fixture carries no timing of its own (it's a handful of
# decode-format smoke-test sentences, not a recorded session), so lines are
# spaced at a plausible fixed cadence rather than instantaneously.
NMEA_LINE_INTERVAL_S = 1.0

TICK_MS = 20
FLUSH_EVERY_FIXES = 5_000


def _emit(pipeline: Pipeline, source_class: str):
    def emit(sample) -> None:
        pipeline.ingest(source_class, sample)

    return emit


def load_candump(path: Path, *, limit: int | None = None) -> list[can.Message]:
    """Read a candump log; ``limit`` bounds how many frames are read."""
    with path.open() as log:
        reader = can.CanutilsLogReader(log)
        frames = itertools.islice(reader, limit) if limit else reader
        return list(frames)


def load_nmea_lines(path: Path) -> list[tuple[float, bytes]]:
    """Read raw NMEA sentences, spaced at a fixed synthetic cadence."""
    lines = []
    for index, raw in enumerate(path.read_text().splitlines()):
        text = raw.strip()
        if not text:
            continue
        lines.append((index * NMEA_LINE_INTERVAL_S, (text + "\r\n").encode("ascii")))
    return lines


def load_gps_trace(path: Path) -> list[tuple[float, float, float, float, float]]:
    """Read the extracted ``t_s,lat,lon,speed_kmh,heading_deg`` fixture."""
    rows = []
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append(
                (
                    float(row["t_s"]),
                    float(row["lat"]),
                    float(row["lon"]),
                    float(row["speed_kmh"]),
                    float(row["heading_deg"]),
                )
            )
    return rows


def replay_cycle(
    profile: ProfileConfig,
    catalog: RuntimeCatalog,
    clock: WallClock,
    *,
    candump_frames: list[can.Message],
    nmea_lines: list[tuple[float, bytes]],
    gps_rows: list[tuple[float, float, float, float, float]],
) -> Iterator[TickBatch]:
    """One full pass over every loaded source, yielded as ordered batches.

    A fresh `Pipeline` (and `LapTimingApp`, if the profile has one) per
    cycle keeps ``--loop`` simple: each cycle is a clean replay of the same
    fixtures from lap/tick zero, the same way restarting the agent would be.

    Batches are *yielded* rather than accumulated. A 110 s fixture would fit
    in memory either way; P4.6's parity run is the whole 24.7 h June-2025
    event -- 1.77 M fixes and ~3.5 M batches -- and building that list before
    publishing a byte of it would need several gigabytes for no reason.
    """
    timing_app = build_timing_app(profile, catalog)
    pipeline = Pipeline(catalog, tick_ms=TICK_MS, timing_app=timing_app)
    base_mono_ns = time.monotonic_ns()

    if candump_frames and profile.vehicle.buses:
        bus = profile.vehicle.buses[0]
        collector = CanCollector(bus, profile.path, _emit(pipeline, bus.name), wall_clock=clock)
        collector.replay(candump_frames)

    if (nmea_lines or gps_rows) and profile.vehicle.serial:
        serial = profile.vehicle.serial[0]
        collector = SerialCollector(serial, _emit(pipeline, serial.name), wall_clock=clock)
        for offset_s, line in nmea_lines:
            collector.handle_line(line, t_mono_ns=base_mono_ns + int(offset_s * 1e9))
        for index, (t_s, lat, lon, speed_kmh, heading) in enumerate(gps_rows):
            sentence = rmc_sentence(lat, lon, speed_kmh, heading)
            collector.handle_line(sentence, t_mono_ns=base_mono_ns + int(t_s * 1e9))
            if _flush_due(gps_rows, index, staged=index % FLUSH_EVERY_FIXES == 0):
                yield from pipeline.flush(clock)

    yield from pipeline.flush(clock)


def _flush_due(
    gps_rows: list[tuple[float, float, float, float, float]], index: int, *, staged: bool
) -> bool:
    """Whether the pipeline may be flushed after feeding ``gps_rows[index]``.

    Only at a gap of at least one tick. `Pipeline.flush` anchors tick windows
    on the earliest sample it holds and the publisher's `msg_id` is
    ``<source-class>:<epoch-ms>``, so a tick window split across two flushes
    would produce two batches carrying the same id -- and JetStream's
    deduplication would silently discard the second. The GPS trace is the only
    source long enough to need incremental flushing; the CAN and NMEA fixtures
    are bounded and fed in one go.
    """
    if not staged or index + 1 >= len(gps_rows):
        return False
    return (gps_rows[index + 1][0] - gps_rows[index][0]) * 1000.0 >= TICK_MS


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--server", default=DEFAULT_SERVER, help="NATS URL (default: %(default)s)")
    parser.add_argument(
        "--vehicle", default=None, help="vehicle id override (default: the profile's)"
    )
    parser.add_argument(
        "--profile", default=str(DEFAULT_PROFILE), help="profile directory (default: %(default)s)"
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=1.0,
        help="publish speed: 1.0 = wall clock, higher = accelerated, "
        "0 = unpaced (default: %(default)s)",
    )
    parser.add_argument("--loop", action="store_true", help="replay continuously until interrupted")
    parser.add_argument(
        "--state-dir", default=None, help="where the registry-generation counter persists"
    )
    parser.add_argument(
        "--candump",
        default=str(DEFAULT_CANDUMP),
        help="candump log for the profile's first CAN bus (empty string to skip)",
    )
    parser.add_argument(
        "--nmea",
        default=str(DEFAULT_NMEA),
        help="raw NMEA sentence file for the profile's first serial source (empty string to skip)",
    )
    parser.add_argument(
        "--gps-trace",
        default=str(DEFAULT_GPS_TRACE),
        help="extracted lat/lon/speed/heading CSV, replayed as RMC (empty string to skip)",
    )
    parser.add_argument(
        "--can-frames", type=int, default=None, help="limit the candump replay to this many frames"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    profile, catalog, vehicle_id = load_catalog(
        args.profile, vehicle=args.vehicle, state_dir=args.state_dir
    )
    candump_frames = load_candump(Path(args.candump), limit=args.can_frames) if args.candump else []
    nmea_lines = load_nmea_lines(Path(args.nmea)) if args.nmea else []
    gps_rows = load_gps_trace(Path(args.gps_trace)) if args.gps_trace else []
    if not (candump_frames or nmea_lines or gps_rows):
        print("replay: no sources selected", file=sys.stderr)
        return 2

    publisher = make_publisher(args.server, vehicle_id, catalog)
    publisher.start()
    try:
        wait_connected(publisher)
    except TimeoutError as exc:
        print(f"replay: {exc}", file=sys.stderr)
        publisher.stop()
        return 1

    clock = SteeredClock()
    cycle = 0
    try:
        while True:
            cycle += 1
            print(
                f"replay: cycle {cycle}: publishing to tele.{vehicle_id}.* at rate={args.rate}",
                file=sys.stderr,
            )
            batches = replay_cycle(
                profile,
                catalog,
                clock,
                candump_frames=candump_frames,
                nmea_lines=nmea_lines,
                gps_rows=gps_rows,
            )
            submitted = publish_paced(publisher, batches, args.rate)
            print(
                f"replay: cycle {cycle}: {submitted} batch(es) submitted, "
                f"{publisher.publish_drops} dropped",
                file=sys.stderr,
            )
            if not args.loop:
                break
    except KeyboardInterrupt:
        print("replay: interrupted", file=sys.stderr)
    finally:
        publisher.drain(timeout_s=20.0)
        publisher.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
