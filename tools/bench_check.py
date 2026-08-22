#!/usr/bin/env python3
"""Verify the bench is offering the signal mix the link budget assumes.

Phase 4 measures bandwidth. If the bench sources are producing the wrong
*signal set* -- GPS silently absent, the IMU log not replaying, canplayer
stalled at the loop seam -- then every kbit/s figure downstream is a
correct measurement of the wrong thing, and nothing about a bandwidth
number looks wrong when that happens. This tool exists so that fails on day
one instead of during the write-up (``docs/plan/PHASE4.md`` P4.1).

**Two comparisons, and they are not the same comparison.**

1. *Measured vs. predicted* -- what the bench actually offers against what
   these fixtures, this catalog and this GPS rate say it should offer. The
   prediction comes from the fixtures themselves: each candump log is
   decoded through the real `CanCollector` and divided by its own recorded
   span, so it is a statement about the bench being wired up correctly.
   **This is the gate**; it fails the run, with ``--tolerance``.

2. *Predicted vs. modelled* -- the same bench against
   ``docs/LINK_BUDGET.md`` §2's 4,087 samples/s. This one is reported, not
   gated, because the two differ for reasons no bench wiring can fix: §2
   counts every signal in each known CAN message where the catalog maps a
   subset; it models GPS at 6 doubles where the catalog has 5 channels; and
   it models the IMU at a flat 100 Hz x 10 where the recorded frame rates
   are 100.2/100.2/50.1/1.0 Hz across four messages. Gating on that would
   be a tool that can never pass and therefore never gets run -- so it
   prints the delta loudly instead, and ``--model-tolerance`` makes it
   fatal for anyone who wants it to be. Closing the gap properly is P4.3's
   signal-mix ground truth; this output is where that starts.

**What is real here.** The same profile loader, the same `RuntimeCatalog`
and derived-channel set the agent builds, the same `CanCollector`,
`SerialCollector` and `HostCollector`, the same bounded queues, the same
`Pipeline` on the same tick, and the same `LapTimingApp` -- so lap/timing
derived channels are counted too, and they are not in §2's model either.
What is deliberately *absent* is the publisher: this tool needs no broker,
because it measures the sources, not the link. The agent's own
``sys.agent.*`` health channels (~13 samples/s) are likewise not produced
here and so are not in these totals.

The registry-generation counter is **not** touched: ``--state-dir``
defaults to a scratch directory, so running the check never bumps the
generation the bench run after it will publish under.

Examples:

    uv run tools/bench_check.py --predict            # no hardware needed
    uv run tools/bench_check.py                      # 30 s against the live bench
    uv run tools/bench_check.py --seconds 120 --tick-ms 10
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import can
from _bench import rmc_sentence  # noqa: E402  (also puts src/ on sys.path)
from size_batch import (  # noqa: E402
    DEFAULT_STATS_PATH,
    GPS_DOUBLES_PER_FIX,
    GPS_RATE_HZ,
    IMU_DOUBLES_PER_FRAME,
    IMU_RATE_HZ,
    NATS_FRAMING_OVERHEAD_B,
    can_summary,
)

from agent.agent import agent_derived_channels  # noqa: E402
from agent.clock import SteeredClock  # noqa: E402
from agent.pipeline import DERIVED_SOURCE_CLASS, Pipeline  # noqa: E402
from agent.queues import SampleQueue  # noqa: E402
from agent.timing_app import build_lap_timing_app  # noqa: E402
from collectors.can import CanCollector  # noqa: E402
from collectors.host import HostCollector  # noqa: E402
from collectors.serial.nmea import NmeaDecoder  # noqa: E402
from collectors.serial.transport import SerialCollector  # noqa: E402
from core.catalog import RuntimeCatalog, build_runtime_catalog  # noqa: E402
from core.config import ProfileConfig, load_profile  # noqa: E402
from core.pb import telemetry_pb2 as pb  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = REPO_ROOT / "profiles" / "example-club-racer-bench"
DEFAULT_CANDUMPS = (
    REPO_ROOT / "tests" / "fixtures" / "candump" / "candump-sample.log",
    REPO_ROOT / "tests" / "fixtures" / "candump" / "candump-imu-sample.log",
)
DEFAULT_SECONDS = 30.0
DEFAULT_TOLERANCE = 0.10
DEFAULT_GPS_RATE_HZ = 50.0

# The IMU is rolled up separately from the rest of the bus because §2 models
# it separately -- it is the stream that never crossed the predecessor's
# link at all (LINK_BUDGET.md §1), so its rate is modelled rather than taken
# from the MQTT stats file.
IMU_DEVICES = frozenset({"imu"})

CAN_CLASS = "can"
IMU_CLASS = "imu"
GPS_CLASS = "gps"
HOST_CLASS = "host"
DERIVED_CLASS = "derived"
MEASURED_CLASSES = (CAN_CLASS, IMU_CLASS, GPS_CLASS, HOST_CLASS, DERIVED_CLASS)


def classify(source_ref: str) -> tuple[str, str]:
    """``(measured class, device alias)`` for one source reference.

    Source references are ``<transport>:<device>.<MESSAGE>.<SIGNAL>`` for
    CAN and serial, ``host:<metric>`` for host metrics, and
    ``derived:<channel>`` for anything the agent produces itself
    (``docs/CATALOG.md``).
    """
    transport, _, remainder = source_ref.partition(":")
    if transport == "host":
        return HOST_CLASS, HOST_CLASS
    if transport == "derived":
        return DERIVED_CLASS, DERIVED_CLASS
    device = remainder.partition(".")[0]
    if device in IMU_DEVICES:
        return IMU_CLASS, device
    if transport.startswith("serial"):
        return GPS_CLASS, device
    return CAN_CLASS, device


@dataclass(slots=True)
class Counts:
    """Samples seen for one class, before and after the catalog decides."""

    emitted: float = 0.0
    mapped: float = 0.0


@dataclass(slots=True)
class Mix:
    """Per-class and per-device sample counts, plus the window they cover."""

    by_class: dict[str, Counts] = field(default_factory=dict)
    by_device: dict[str, float] = field(default_factory=dict)
    elapsed_s: float = 1.0

    def add(self, source_ref: str, *, mapped: bool, weight: float = 1.0) -> None:
        source_class, device = classify(source_ref)
        counts = self.by_class.setdefault(source_class, Counts())
        counts.emitted += weight
        if mapped:
            counts.mapped += weight
            self.by_device[device] = self.by_device.get(device, 0.0) + weight

    def rate(self, source_class: str) -> float:
        counts = self.by_class.get(source_class)
        if counts is None or self.elapsed_s <= 0:
            return 0.0
        return counts.mapped / self.elapsed_s

    def emitted_rate(self, source_class: str) -> float:
        counts = self.by_class.get(source_class)
        if counts is None or self.elapsed_s <= 0:
            return 0.0
        return counts.emitted / self.elapsed_s

    def total_rate(self) -> float:
        return sum(self.rate(name) for name in self.by_class)

    def device_rates(self) -> dict[str, float]:
        return {name: count / self.elapsed_s for name, count in sorted(self.by_device.items())}


# -- prediction (no hardware) -------------------------------------------------


def predict_can(profile: ProfileConfig, catalog: RuntimeCatalog, logs: list[Path]) -> Mix:
    """Mapped samples/s each candump fixture contributes while it loops.

    Decoded through the real collector against the profile's own DBCs and
    divided by the fixture's *own* recorded span, so the prediction tracks
    whatever the fixture actually contains rather than a remembered figure.
    The result is already a rate: ``elapsed_s`` stays 1.
    """
    mix = Mix()
    bus = profile.vehicle.buses[0]
    for path in logs:
        with path.open() as handle:
            frames = list(can.CanutilsLogReader(handle))
        if len(frames) < 2:
            raise ValueError(f"{path}: need at least two frames to infer a rate")
        span_s = frames[-1].timestamp - frames[0].timestamp
        if span_s <= 0:
            raise ValueError(f"{path}: frame timestamps do not advance")
        weight = 1.0 / span_s
        collector = CanCollector(
            bus,
            profile.path,
            lambda sample, weight=weight: mix.add(
                sample.source_ref, mapped=sample.source_ref in catalog.source_map, weight=weight
            ),
        )
        collector.replay(frames)
    return mix


def predict_gps(profile: ProfileConfig, catalog: RuntimeCatalog, rate_hz: float) -> float:
    """Mapped samples/s from the pty feeder, one real RMC decoded for real."""
    source = profile.vehicle.serial[0]
    device = source.driver.name if source.driver is not None else source.decoder
    decoder = NmeaDecoder(source.name, device)
    values = decoder.decode(rmc_sentence(-31.6725, 115.7815, 120.0, 90.0))
    if not values:
        raise ValueError("the NMEA decoder rejected a freshly built RMC sentence")
    mapped = sum(1 for source_ref, _ in values if source_ref in catalog.source_map)
    return mapped * rate_hz


def predict_host(profile: ProfileConfig, catalog: RuntimeCatalog) -> float:
    """Mapped samples/s from the host collector's poll interval."""
    interval_s = profile.vehicle.host.interval_ns / 1e9
    if not profile.vehicle.host.enabled or interval_s <= 0:
        return 0.0
    mapped = sum(1 for source_ref in catalog.source_map if source_ref.startswith("host:"))
    return mapped / interval_s


# -- the live run -------------------------------------------------------------


@dataclass(slots=True)
class RunResult:
    """One live pass: the mix, the batches it produced, and collector health."""

    mix: Mix
    batches: int = 0
    payload_bytes: int = 0
    unmapped_refs: int = 0
    rbe_suppressed: int = 0
    encode_failures: int = 0
    queue_drops: dict[str, int] = field(default_factory=dict)
    transport_notes: dict[str, str] = field(default_factory=dict)


def run_live(
    profile: ProfileConfig,
    catalog: RuntimeCatalog,
    *,
    seconds: float,
    tick_ms: int,
    stop: threading.Event,
    bus_factory=None,
    serial_factory=None,
) -> RunResult:
    """Drive the real collectors and pipeline for ``seconds``, counting everything.

    Wired the way ``VehicleAgent`` wires itself, minus the publisher: same
    queues, same single pipeline thread, same tick, same timing app. Only
    the batches go to a counter instead of to JetStream. ``bus_factory`` and
    ``serial_factory`` are the same injection points ``VehicleAgent`` takes,
    and exist for the same reason: the counting arithmetic has to be
    testable on a host with no vcan and no pty.
    """
    mix = Mix()
    result = RunResult(mix=mix)
    clock = SteeredClock()

    timing_app = None
    lap_timing = profile.catalog.apps.lap_timing
    if lap_timing is not None:
        timing_app = build_lap_timing_app(
            catalog, lap_timing.position, lap_timing.track, str(profile.path / "tracks")
        )
    pipeline = Pipeline(catalog, tick_ms=tick_ms, timing_app=timing_app)

    collectors: list[tuple[CanCollector | SerialCollector | HostCollector, SampleQueue]] = []
    for bus in profile.vehicle.buses:
        queue = SampleQueue(bus.name)
        collectors.append(
            (
                CanCollector(
                    bus, profile.path, queue.put, wall_clock=clock, bus_factory=bus_factory
                ),
                queue,
            )
        )
    for source in profile.vehicle.serial:
        queue = SampleQueue(source.name)
        collectors.append(
            (
                SerialCollector(source, queue.put, wall_clock=clock, serial_factory=serial_factory),
                queue,
            )
        )
    if profile.vehicle.host.enabled:
        queue = SampleQueue(HOST_CLASS)
        collectors.append((HostCollector(profile.vehicle.host, queue.put, wall_clock=clock), queue))

    for collector, _ in collectors:
        collector.start()
    started = time.monotonic()
    tick_s = tick_ms / 1000.0
    next_tick = started + tick_s
    try:
        while not stop.is_set() and time.monotonic() - started < seconds:
            delay = next_tick - time.monotonic()
            if delay > 0:
                stop.wait(delay)
            next_tick = max(next_tick + tick_s, time.monotonic())
            _drain_and_flush(collectors, pipeline, catalog, clock, mix, result)
    finally:
        for collector, _ in collectors:
            collector.stop()
        _drain_and_flush(collectors, pipeline, catalog, clock, mix, result)
        mix.elapsed_s = time.monotonic() - started

    result.unmapped_refs = pipeline.unmapped_refs
    result.rbe_suppressed = pipeline.rbe_suppressed
    result.encode_failures = pipeline.encode_failures
    for collector, queue in collectors:
        result.queue_drops[queue.source_class] = queue.dropped
        note = _transport_note(collector)
        if note:
            result.transport_notes[queue.source_class] = note
    return result


def _drain_and_flush(collectors, pipeline, catalog, clock, mix: Mix, result: RunResult) -> None:
    for _, queue in collectors:
        for sample in queue.drain():
            mix.add(sample.source_ref, mapped=sample.source_ref in catalog.source_map)
            pipeline.ingest(queue.source_class, sample)
    for batch in pipeline.flush(clock):
        result.batches += 1
        result.payload_bytes += len(batch.payload)
        if batch.source_class == DERIVED_SOURCE_CLASS:
            # Derived samples never cross a queue -- the timing app emits
            # them from inside `Pipeline.ingest` -- so they are counted off
            # the batch that carries them instead of at the queue boundary.
            decoded = pb.SampleBatch.FromString(batch.payload)
            for _ in decoded.samples:
                mix.add(f"{DERIVED_SOURCE_CLASS}:", mapped=True)


def _transport_note(collector) -> str:
    """A one-line reason when a collector had trouble reaching its source."""
    stats = getattr(collector, "stats", None)
    if stats is None:
        return ""
    parts = [
        f"{name}={value}"
        for name in ("open_failures", "bus_errors", "read_errors", "reconnects", "probe_failures")
        if (value := getattr(stats, name, 0))
    ]
    return ", ".join(parts)


# -- reporting ----------------------------------------------------------------


def modelled_rates(stats_path: Path) -> dict[str, float]:
    """§2's modelled samples/s per class, read from the same stats file."""
    _, _, can_updates_per_s = can_summary(stats_path)
    return {
        CAN_CLASS: can_updates_per_s,
        IMU_CLASS: IMU_RATE_HZ * IMU_DOUBLES_PER_FRAME,
        GPS_CLASS: GPS_RATE_HZ * GPS_DOUBLES_PER_FIX,
    }


def _delta(actual: float, reference: float | None) -> str:
    if not reference:
        return "n/a".rjust(8)
    return f"{(actual - reference) / reference * 100:+7.1f}%"


def _cell(value: float | None, width: int = 12) -> str:
    return "-".rjust(width) if value is None else f"{value:{width}.1f}"


def report_prediction(predicted: dict[str, float], modelled: dict[str, float], out) -> None:
    """Print the bench's predicted mix beside §2's model, per class."""
    print("\nPredicted bench mix vs. LINK_BUDGET.md §2 model", file=out)
    print(f"  {'class':<9}{'predicted/s':>13}{'modelled/s':>13}{'delta':>9}", file=out)
    for name in MEASURED_CLASSES:
        if name not in predicted:
            continue
        reference = modelled.get(name)
        print(
            f"  {name:<9}{predicted[name]:13.1f}{_cell(reference, 13)}"
            f"{_delta(predicted[name], reference):>9}",
            file=out,
        )
    predicted_total = sum(predicted.values())
    modelled_total = sum(modelled.values())
    print(
        f"  {'TOTAL':<9}{predicted_total:13.1f}{modelled_total:13.1f}"
        f"{_delta(predicted_total, modelled_total):>9}",
        file=out,
    )


def report_run(
    result: RunResult, predicted: dict[str, float], tolerance: float, tick_ms: int, out
) -> list[str]:
    """Print the measured mix; return one failure line per class off target."""
    mix = result.mix
    print(f"\nMeasured over {mix.elapsed_s:.1f}s at a {tick_ms} ms tick", file=out)
    header = f"  {'class':<9}{'emitted/s':>12}{'mapped/s':>12}{'predicted/s':>13}{'delta':>9}"
    print(header, file=out)
    failures: list[str] = []
    for name in MEASURED_CLASSES:
        expected = predicted.get(name)
        measured = mix.rate(name)
        print(
            f"  {name:<9}{mix.emitted_rate(name):12.1f}{measured:12.1f}"
            f"{_cell(expected, 13)}{_delta(measured, expected):>9}",
            file=out,
        )
        if not expected:
            continue
        if abs(measured - expected) > tolerance * expected:
            failures.append(
                f"{name}: measured {measured:.1f}/s against {expected:.1f}/s predicted "
                f"(tolerance {tolerance:.0%}){_hint(name, measured)}"
            )
    print(f"  {'TOTAL':<9}{'':12}{mix.total_rate():12.1f}", file=out)

    if mix.elapsed_s > 0 and result.batches:
        batches_per_s = result.batches / mix.elapsed_s
        framed_bytes = result.payload_bytes + result.batches * NATS_FRAMING_OVERHEAD_B
        mean_batch = result.payload_bytes / result.batches
        print(
            f"\n  offered load: {batches_per_s:.1f} batch/s, mean {mean_batch:.0f} B protobuf, "
            f"{framed_bytes / mix.elapsed_s * 8 / 1000:.1f} kbit/s with NATS framing",
            file=out,
        )
    drops = {name: count for name, count in result.queue_drops.items() if count}
    print(
        f"  health: unmapped_refs={result.unmapped_refs} "
        f"rbe_suppressed={result.rbe_suppressed} encode_failures={result.encode_failures} "
        f"queue_drops={drops or 'none'}",
        file=out,
    )
    if result.encode_failures:
        # One mis-typed catalog channel discards whole tick windows across
        # every source class; the measured mix above just looks quiet, so the
        # gate has to name it (docs/AGENT_DESIGN.md -> Health and status).
        failures.append(
            f"encode: {result.encode_failures} tick window(s) discarded by an encode error "
            "(a catalog channel's type does not match the value feeding it)"
        )
    for name, note in sorted(result.transport_notes.items()):
        print(f"  transport {name}: {note}", file=out)
    return failures


def _hint(source_class: str, measured: float) -> str:
    if measured > 0:
        return ""
    if source_class == GPS_CLASS:
        return " -- is tools/bench_gps.py running, and is the profile's port its --link?"
    if source_class in (CAN_CLASS, IMU_CLASS):
        return " -- is canplayer replaying onto the profile's interface?"
    return ""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--profile", default=str(DEFAULT_PROFILE), help="profile directory (default: %(default)s)"
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=DEFAULT_SECONDS,
        help="how long to sample the live bench (default: %(default)s)",
    )
    parser.add_argument(
        "--tick-ms",
        type=int,
        default=int(os.environ.get("OPENLAPS_TICK_MS") or 20),
        help="batching tick, as the agent will run it (default: OPENLAPS_TICK_MS or 20)",
    )
    parser.add_argument(
        "--candump",
        action="append",
        default=None,
        help="candump fixture the bench is looping; repeatable (default: both fixtures)",
    )
    parser.add_argument(
        "--gps-rate-hz",
        type=float,
        default=DEFAULT_GPS_RATE_HZ,
        help="the rate tools/bench_gps.py is feeding at (default: %(default)s)",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        help="fractional tolerance on measured vs. predicted (default: %(default)s)",
    )
    parser.add_argument(
        "--model-tolerance",
        type=float,
        default=None,
        help="also fail when the predicted total is further than this from the §2 model "
        "(default: report the delta without failing -- see the module docstring)",
    )
    parser.add_argument(
        "--stats",
        default=str(DEFAULT_STATS_PATH),
        help="the signal-mix stats behind LINK_BUDGET.md §2 (default: %(default)s)",
    )
    parser.add_argument(
        "--predict",
        action="store_true",
        help="print the prediction and the model comparison, then stop -- no hardware needed",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="where the registry-generation counter persists (default: a scratch directory, so "
        "the check never bumps the generation of the run that follows it)",
    )
    return parser


def main(argv: list[str] | None = None, out=sys.stdout) -> int:
    args = build_arg_parser().parse_args(argv)
    logs = [Path(path) for path in (args.candump or DEFAULT_CANDUMPS)]
    with tempfile.TemporaryDirectory(prefix="openlaps-bench-check-") as scratch:
        state_dir = Path(args.state_dir) if args.state_dir else Path(scratch)
        try:
            profile, catalog, prediction = _prepare(args, logs, state_dir)
        except (OSError, ValueError, KeyError) as exc:
            print(f"bench_check: {exc}", file=sys.stderr)
            return 2
        predicted, can_prediction = prediction

        _report_profile(profile, catalog, logs, can_prediction, out)
        modelled = modelled_rates(Path(args.stats))
        report_prediction(predicted, modelled, out)
        status = _check_model(predicted, modelled, args.model_tolerance, out)
        if args.predict:
            return status

        stop = threading.Event()
        try:
            result = run_live(
                profile, catalog, seconds=args.seconds, tick_ms=args.tick_ms, stop=stop
            )
        except KeyboardInterrupt:
            print("bench_check: interrupted", file=sys.stderr)
            return 130

    failures = report_run(result, predicted, args.tolerance, args.tick_ms, out)
    if failures:
        print("\nFAIL: the bench is not offering the predicted mix", file=out)
        for line in failures:
            print(f"  - {line}", file=out)
        return 1
    print("\nOK: every source is within tolerance of its prediction", file=out)
    return status


def _prepare(args, logs: list[Path], state_dir: Path):
    """Load the profile, build the agent's own catalog, and predict the mix."""
    profile = load_profile(args.profile)
    collector_names = [bus.name for bus in profile.vehicle.buses]
    collector_names += [source.name for source in profile.vehicle.serial]
    if profile.vehicle.host.enabled:
        collector_names.append(HOST_CLASS)
    catalog = build_runtime_catalog(
        profile,
        state_path=state_dir / ".registry-state.json",
        derived_channels=agent_derived_channels(collector_names),
    )
    can_prediction = predict_can(profile, catalog, logs)
    predicted = {
        CAN_CLASS: can_prediction.rate(CAN_CLASS),
        IMU_CLASS: can_prediction.rate(IMU_CLASS),
        GPS_CLASS: predict_gps(profile, catalog, args.gps_rate_hz),
        HOST_CLASS: predict_host(profile, catalog),
    }
    return profile, catalog, (predicted, can_prediction)


def _report_profile(
    profile: ProfileConfig, catalog: RuntimeCatalog, logs: list[Path], can_prediction: Mix, out
) -> None:
    bus = profile.vehicle.buses[0]
    serial = profile.vehicle.serial[0]
    print(f"profile: {profile.path}", file=out)
    print(f"  bus {bus.name} -> {bus.interface}", file=out)
    print(f"  serial {serial.name} -> {serial.port}", file=out)
    print(
        f"  catalog sha256 {catalog.catalog_hash[:12]}, {len(catalog.channel_ids)} channels",
        file=out,
    )
    print(f"  candump fixtures: {', '.join(path.name for path in logs)}", file=out)
    for device, rate in can_prediction.device_rates().items():
        print(f"    {device:<12}{rate:9.1f} mapped/s", file=out)


def _check_model(
    predicted: dict[str, float], modelled: dict[str, float], model_tolerance: float | None, out
) -> int:
    if model_tolerance is None:
        return 0
    predicted_total, modelled_total = sum(predicted.values()), sum(modelled.values())
    if abs(predicted_total - modelled_total) > model_tolerance * modelled_total:
        print(
            f"\nFAIL: predicted {predicted_total:.1f}/s is further than "
            f"{model_tolerance:.0%} from the modelled {modelled_total:.1f}/s",
            file=out,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
