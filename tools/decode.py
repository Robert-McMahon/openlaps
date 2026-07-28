#!/usr/bin/env python3
"""Registry-aware live decoder for openlaps telemetry.

Debug/bench tool for the pit (or the vehicle itself): subscribes to
``tele.<vehicle>.>``, resolves every ``SampleBatch`` against the exact
``ChannelRegistry`` generation it was encoded with (recovered from the
stream per docs/WIRE_FORMAT.md), and prints human-readable channel values.

Two output modes:

- line mode (default): one line per sample —
  ``capture-time  source-class  channel  value unit``
- ``--watch``: a repainting snapshot table of the latest value per channel
  plus stream totals (msgs/s, KiB/s) — handy for eyeballing the radio
  budget during a bench test.

Examples (pit machine):

    uv run tools/decode.py --server nats://192.168.12.176:4222 --watch
    uv run tools/decode.py --server nats://192.168.12.176:4222 \\
        --channels 'position.*' --channels 'car.rpm'
    uv run tools/decode.py --all            # replay the whole retained stream

This is a forerunner of the Phase 3 live-decoder service; it shares its
consumer-side obligations: hard registry_seq equality, scale/offset
application, and loud rejection of unknown format versions. Those live in
``pit.registry_cache.RegistryCache``, which was promoted out of this file
and which the pit services import — there is one copy, and this tool uses
the same one they do.
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import nats  # noqa: E402
from nats.js import api  # noqa: E402

from core.pb import telemetry_pb2 as pb  # noqa: E402
from pit.registry_cache import RegistryCache  # noqa: E402

MSG_TYPE_HEADER = "Openlaps-Msg-Type"
DEFAULT_SERVER = os.environ.get("OPENLAPS_NATS_URL", "nats://127.0.0.1:4222")
DEFAULT_VEHICLE = "example-club-racer"


def _format_value(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _matches(name: str, globs: list[str]) -> bool:
    return not globs or any(fnmatch.fnmatchcase(name, glob) for glob in globs)


class Totals:
    """Windowed message/byte rates for the whole subscription."""

    def __init__(self) -> None:
        self.messages = 0
        self.payload_bytes = 0
        self._window_start = time.monotonic()
        self._window_messages = 0
        self._window_bytes = 0
        self.msgs_per_s = 0.0
        self.kib_per_s = 0.0

    def observe(self, size: int) -> None:
        self.messages += 1
        self.payload_bytes += size
        self._window_messages += 1
        self._window_bytes += size

    def roll(self) -> None:
        elapsed = time.monotonic() - self._window_start
        if elapsed <= 0:
            return
        self.msgs_per_s = self._window_messages / elapsed
        self.kib_per_s = self._window_bytes / 1024.0 / elapsed
        self._window_start = time.monotonic()
        self._window_messages = 0
        self._window_bytes = 0


def _print_line(capture_ms: float, source_class: str, channel: pb.Channel, value: object) -> None:
    stamp = datetime.fromtimestamp(capture_ms / 1000.0, tz=UTC).strftime("%H:%M:%S.%f")[:-3]
    unit = f" {channel.units}" if channel.units else ""
    print(f"{stamp}  {source_class:<8s} {channel.name:<36s} {_format_value(value)}{unit}")


def _print_watch(
    vehicle: str,
    totals: Totals,
    latest: dict[str, tuple[str, object, str, int]],
    cache: RegistryCache,
) -> None:
    lines = [
        f"openlaps decode — {vehicle}   "
        f"{totals.msgs_per_s:6.1f} msg/s   {totals.kib_per_s:7.2f} KiB/s   "
        f"total {totals.messages} msgs / {totals.payload_bytes / 1024:.0f} KiB",
    ]
    if cache.unknown_seqs:
        lines.append(
            f"!! undecodable batches: unknown registry seq(s) {sorted(cache.unknown_seqs)}"
        )
    lines.append(f"{'CHANNEL':<38s}{'VALUE':>16s}  {'UNIT':<8s}{'CLASS':<9s}{'N':>8s}")
    for name in sorted(latest):
        source_class, value, unit, count = latest[name]
        lines.append(
            f"{name:<38s}{_format_value(value):>16s}  {unit:<8s}{source_class:<9s}{count:>8d}"
        )
    sys.stdout.write("\x1b[2J\x1b[H" + "\n".join(lines) + "\n")
    sys.stdout.flush()


async def run(args: argparse.Namespace) -> int:
    client = await nats.connect(args.server)
    js = client.jetstream()
    cache = RegistryCache()

    # Registry recovery: read every retained registry generation first, so
    # batches are decodable from the very first message we see.
    catalog_subject = f"tele.{args.vehicle}.catalog"
    recovery = await js.subscribe(
        catalog_subject, ordered_consumer=True, deliver_policy=api.DeliverPolicy.ALL
    )
    try:
        while True:
            message = await recovery.next_msg(timeout=1.0)
            cache.add(message.data)
    except TimeoutError:
        pass
    finally:
        await recovery.unsubscribe()

    policy = api.DeliverPolicy.ALL if args.all else api.DeliverPolicy.NEW
    subscription = await js.subscribe(
        f"tele.{args.vehicle}.>", ordered_consumer=True, deliver_policy=policy
    )
    print(
        f"connected to {args.server}, watching tele.{args.vehicle}.> "
        f"({'replaying retained stream' if args.all else 'live from now'})",
        file=sys.stderr,
    )

    totals = Totals()
    latest: dict[str, tuple[str, object, str, int]] = {}
    counts: dict[str, int] = {}
    next_paint = time.monotonic() + args.interval
    try:
        while True:
            try:
                message = await subscription.next_msg(timeout=0.25)
            except TimeoutError:
                message = None
            if message is not None:
                totals.observe(len(message.data))
                source_class = message.subject.rsplit(".", 1)[-1]
                kind = (message.headers or {}).get(MSG_TYPE_HEADER, "batch")
                if kind == "registry":
                    seq = cache.add(message.data)
                    print(f"-- registry generation {seq} received", file=sys.stderr)
                    continue
                decoded = cache.decode(message.data)
                if decoded is None:
                    continue
                for sample in decoded.samples:
                    channel = sample.channel
                    if not _matches(channel.name, args.channels):
                        continue
                    if args.watch:
                        counts[channel.name] = counts.get(channel.name, 0) + 1
                        latest[channel.name] = (
                            source_class,
                            sample.value,
                            channel.units,
                            counts[channel.name],
                        )
                    else:
                        _print_line(sample.capture_unix_ms, source_class, channel, sample.value)
            if args.watch and time.monotonic() >= next_paint:
                totals.roll()
                _print_watch(args.vehicle, totals, latest, cache)
                next_paint = time.monotonic() + args.interval
    except asyncio.CancelledError:
        return 0
    finally:
        await client.close()


def main() -> int:
    # RegistryCache reports consumer-obligation violations through logging;
    # keep the tool's original "!! ..." shape on stderr.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="!! %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--server", default=DEFAULT_SERVER, help="NATS URL (default: %(default)s)")
    parser.add_argument(
        "--vehicle", default=DEFAULT_VEHICLE, help="vehicle id (default: %(default)s)"
    )
    parser.add_argument(
        "--channels",
        action="append",
        default=[],
        metavar="GLOB",
        help="only show channels matching this glob (repeatable), e.g. 'position.*'",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="replay the whole retained stream instead of starting live",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="repainting latest-value table instead of one line per sample",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="watch-mode repaint interval in seconds (default: %(default)s)",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
