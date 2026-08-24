#!/usr/bin/env python3
"""Stitch time-shifted copies of a candump log into one longer capture.

`tools/replay.py` feeds every source of a cycle on one shared clock, so a
CAN capture shorter than the GPS trace leaves every CAN-fed channel silent
for the rest of the cycle — on a bench that presents as live gauges that
freeze for most of each lap. Until a full-length capture from a real event
exists (docs/RAW_CAPTURE.md is that plan), the honest stopgap is to repeat
the capture we have: this tool concatenates N copies of a candump log with
each copy's timestamps shifted so the frames stay monotonic and keep their
original cadence.

Only the timestamp field is rewritten; the rest of each line — interface,
identifier, payload, any trailing flags — is preserved verbatim, so the
output stays a valid `canplayer`/`python-can` log of whatever dialect the
input was.

Every copy boundary is the same discontinuity as `--loop`'s seam, multiplied:
totalizing channels (fuel used, trip distance, trigger counters) reset at
each seam, and per-signal rates must not be read across one — the same
caveat docs/BENCH_RUNBOOK.md attaches to looped fixtures. Data stitched by
this tool is for exercising dashboards and pipelines, not for measurement.

Usage:
    uv run tools/stitch_candump.py tests/fixtures/candump/candump-sample.log \
        --target-seconds 92 --output /tmp/candump-stitched.log
    uv run tools/stitch_candump.py capture.log --copies 3 --output stitched.log
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

# The timestamp prefix of a candump -L / -l line: "(1779526305.126390) ...".
# Everything after it is carried through untouched.
_LINE = re.compile(r"^\((\d+\.\d+)\) (.*)$")

DEFAULT_GAP_S = 0.2


def parse_log(path: Path) -> list[tuple[float, str]]:
    """The log's frames as (timestamp, rest-of-line), in file order.

    Raises ValueError on the first line that does not carry a candump
    timestamp prefix — a malformed input should fail here, loudly, not
    become a silently shorter output.
    """
    frames: list[tuple[float, str]] = []
    with path.open() as handle:
        for number, line in enumerate(handle, start=1):
            line = line.rstrip("\n")
            if not line:
                continue
            match = _LINE.match(line)
            if match is None:
                raise ValueError(f"{path}:{number}: not a candump log line: {line!r}")
            frames.append((float(match.group(1)), match.group(2)))
    if not frames:
        raise ValueError(f"{path}: no frames")
    return frames


def copies_for_target(span_s: float, gap_s: float, target_s: float) -> int:
    """How many copies cover ``target_s`` seconds, never fewer than one."""
    return max(1, math.ceil(target_s / (span_s + gap_s)))


def stitch(frames: list[tuple[float, str]], copies: int, gap_s: float, out) -> float:
    """Write ``copies`` passes of ``frames`` to ``out``; returns the total span.

    Copy k is shifted by k * (span + gap): timestamps stay monotonic across
    the whole output and keep the source's cadence within each copy.
    """
    span_s = frames[-1][0] - frames[0][0]
    for copy in range(copies):
        offset = copy * (span_s + gap_s)
        for timestamp, rest in frames:
            out.write(f"({timestamp + offset:.6f}) {rest}\n")
    return copies * span_s + (copies - 1) * gap_s


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log", help="candump log to repeat")
    parser.add_argument(
        "--output", required=True, help="where the stitched log is written (never in-place)"
    )
    count = parser.add_mutually_exclusive_group(required=True)
    count.add_argument("--copies", type=int, help="repeat the capture exactly this many times")
    count.add_argument(
        "--target-seconds",
        type=float,
        help="repeat until the output spans at least this long "
        "(e.g. the GPS trace's duration for a replay cycle)",
    )
    parser.add_argument(
        "--gap",
        type=float,
        default=DEFAULT_GAP_S,
        help="seconds of silence between copies (default: %(default)s)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.copies is not None and args.copies < 1:
        print("stitch-candump: --copies must be at least 1", file=sys.stderr)
        return 2

    source = Path(args.log)
    destination = Path(args.output)
    if destination.resolve() == source.resolve():
        print("stitch-candump: refusing to overwrite the input", file=sys.stderr)
        return 2

    try:
        frames = parse_log(source)
    except ValueError as exc:
        print(f"stitch-candump: {exc}", file=sys.stderr)
        return 2

    span_s = frames[-1][0] - frames[0][0]
    copies = (
        args.copies
        if args.copies is not None
        else copies_for_target(span_s, args.gap, args.target_seconds)
    )
    with destination.open("w") as out:
        total_s = stitch(frames, copies, args.gap, out)

    print(
        f"stitch-candump: {len(frames)} frames / {span_s:.1f}s x {copies} "
        f"-> {len(frames) * copies} frames / {total_s:.1f}s: {destination}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
