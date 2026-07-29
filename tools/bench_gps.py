#!/usr/bin/env python3
"""Feed synthetic 50 Hz ``$GPRMC`` over a pty so the bench has GPS at all.

**Why this exists.** Indoors the UM980 reports a *void* fix, and the NMEA
decoder emits ``position.*`` only on an active one -- ``NmeaDecoder.decode``
rejects any RMC whose status field is not ``A``
(``src/collectors/serial/nmea.py``). So a bench built around a real receiver
with no sky view does not produce a degraded GPS stream; it produces *no*
GPS stream, silently, while every other source looks healthy. That is 250
samples/s of the offered load -- the five ``position.*`` channels at 50 Hz,
against the 300/s ``docs/LINK_BUDGET.md`` §2 models -- and a bandwidth
figure measured without them is wrong in the direction that flatters the
result.

This feeder replaces the receiver, not the decoder: it writes real ``$GPRMC``
sentences (built by ``tools/_bench.py``'s ``rmc_sentence``, the same encoder
``tools/replay.py`` uses) into the master side of a pty, and publishes a
stable symlink to the slave side so a profile can name a fixed path. The
agent under test then opens an ordinary serial device and runs its real
``SerialCollector`` and real NMEA decoder over it -- the injection point is
*below* the agent, which is Phase 4's locked decision 4.

Positions come from ``tests/fixtures/gps/wanneroo-trace.csv`` (a ~110 s,
~20 Hz slice of real on-track driving -- see that directory's README) and
are **interpolated onto the requested cadence** rather than replayed
row-for-row, so ``--rate-hz`` sets the offered load without also changing
how fast the car appears to move. Lap timing therefore sees a realistic
trajectory and produces ``lap.*``/``timing.*`` derived channels at a
realistic rate, which is part of the mix being measured.

``--loop`` wraps back to the start of the trace. The slice is not a closed
lap, so the wrap is a position discontinuity -- the GPS analogue of
``canplayer -l i``'s timestamp seam. Harmless for offered load; do not read
per-lap timing across a seam.

Examples:

    uv run tools/bench_gps.py --loop
    uv run tools/bench_gps.py --loop --rate-hz 10 --link /tmp/gps-slow
    uv run tools/bench_gps.py --seconds 30        # one bounded burst
"""

from __future__ import annotations

import argparse
import bisect
import csv
import errno
import os
import pty
import signal
import sys
import threading
import time
import tty
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from _bench import rmc_sentence  # noqa: E402  (also puts src/ on sys.path)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRACE = REPO_ROOT / "tests" / "fixtures" / "gps" / "wanneroo-trace.csv"
DEFAULT_LINK = "/tmp/openlaps-bench-gps"
DEFAULT_RATE_HZ = 50.0
DEFAULT_STATUS_INTERVAL_S = 10.0

# One RMC sentence is ~70 B, so a pty's buffer holds many. Reading the far
# side in chunks this size drains RTCM write-back (the pit ntrip-client's
# corrections reach the agent, which writes them at the receiver that isn't
# there) faster than it can arrive.
DRAIN_CHUNK_B = 4096

Fix = tuple[float, float, float, float]
"""``(lat, lon, speed_kmh, heading_deg)`` -- the four fields an RMC carries."""


@dataclass(frozen=True, slots=True)
class Trace:
    """A time-stamped position trace, ready to interpolate."""

    times: list[float]
    fixes: list[Fix]

    @property
    def duration_s(self) -> float:
        """Span from the first fix to the last; never zero (see ``load_trace``)."""
        return self.times[-1] - self.times[0]


def load_trace(path: str | Path) -> Trace:
    """Read a ``t_s,lat,lon,speed_kmh,heading_deg`` CSV into a `Trace`.

    The same shape ``tools/replay.py --gps-trace`` takes, so one extraction
    serves both tools.
    """
    times: list[float] = []
    fixes: list[Fix] = []
    with Path(path).open(newline="") as handle:
        for row in csv.DictReader(handle):
            t_s = float(row["t_s"])
            if times and t_s <= times[-1]:
                raise ValueError(f"{path}: t_s must strictly increase (at row {len(times) + 1})")
            times.append(t_s)
            fixes.append(
                (
                    float(row["lat"]),
                    float(row["lon"]),
                    float(row["speed_kmh"]),
                    float(row["heading_deg"]),
                )
            )
    if len(times) < 2:
        raise ValueError(f"{path}: need at least two fixes to interpolate between")
    return Trace(times, fixes)


def interpolate(trace: Trace, offset_s: float) -> Fix:
    """The fix ``offset_s`` seconds after the trace's first, linearly blended.

    ``offset_s`` is clamped to the trace rather than wrapped -- looping is
    the caller's business, because only the caller knows whether a wrap is
    wanted or the run has simply ended.
    """
    target = trace.times[0] + offset_s
    index = bisect.bisect_right(trace.times, target)
    if index <= 0:
        return trace.fixes[0]
    if index >= len(trace.times):
        return trace.fixes[-1]
    before_t, after_t = trace.times[index - 1], trace.times[index]
    before, after = trace.fixes[index - 1], trace.fixes[index]
    fraction = (target - before_t) / (after_t - before_t)
    return (
        before[0] + fraction * (after[0] - before[0]),
        before[1] + fraction * (after[1] - before[1]),
        before[2] + fraction * (after[2] - before[2]),
        _blend_heading(before[3], after[3], fraction),
    )


def _blend_heading(before: float, after: float, fraction: float) -> float:
    """Interpolate a compass bearing the short way round the circle.

    Blending 359 deg and 1 deg linearly would sweep the long way and put the
    car briefly pointing south; RMC heading is modular, so the shortest arc
    is the only correct reading.
    """
    delta = ((after - before + 180.0) % 360.0) - 180.0
    return (before + delta * fraction) % 360.0


def fixes_at(trace: Trace, rate_hz: float, *, loop: bool) -> Iterator[Fix]:
    """Yield fixes spaced ``1/rate_hz`` apart along the trace's own timeline."""
    if rate_hz <= 0:
        raise ValueError("rate_hz must be greater than zero")
    step_s = 1.0 / rate_hz
    duration_s = trace.duration_s
    offset_s = 0.0
    while True:
        yield interpolate(trace, offset_s)
        offset_s += step_s
        if offset_s > duration_s:
            if not loop:
                return
            # Carry the remainder across the seam so cadence is preserved
            # exactly: a wrap must not cost or gain a fraction of a tick.
            offset_s -= duration_s


@dataclass(slots=True)
class FeederStats:
    """What the run did, printed periodically and once at the end."""

    sentences: int = 0
    bytes_written: int = 0
    dropped: int = 0
    drained_bytes: int = 0
    late_ticks: int = 0


class PtyFeeder:
    """Owns the pty pair, the published symlink, and the non-blocking writes.

    Sentences are queued whole or not at all: a pty whose reader has gone
    away fills up, and a half-written sentence would reach the decoder as a
    malformed line -- an artefact of the instrument, counted against the
    thing being measured. Dropping the sentence and counting it is honest;
    splitting it is not.
    """

    def __init__(self, link: str | Path | None) -> None:
        self.stats = FeederStats()
        self._master_fd, slave_fd = pty.openpty()
        self.slave_path = os.ttyname(slave_fd)
        # Raw both sides: no echo, and no CR/NL translation to mangle the
        # "\r\n" an NMEA sentence ends with. pyserial re-applies raw mode
        # when the agent opens the slave, but anything looking at the link
        # before that (a bare `cat`, say) should see the same bytes.
        tty.setraw(self._master_fd)
        tty.setraw(slave_fd)
        os.set_blocking(self._master_fd, False)
        # The slave fd is closed immediately: holding it open would make this
        # process a second reader competing with the agent for every
        # sentence. The pty survives on the master alone.
        os.close(slave_fd)
        self._pending = b""
        self.link = _publish_link(link, self.slave_path) if link else None

    def offer(self, sentence: bytes) -> None:
        """Queue one sentence if the last one has fully drained, else drop it."""
        self.flush()
        if self._pending:
            self.stats.dropped += 1
            return
        self._pending = sentence
        self.flush()

    def flush(self) -> None:
        """Push as much of the pending sentence out as the pty will take."""
        while self._pending:
            try:
                written = os.write(self._master_fd, self._pending)
            except BlockingIOError:
                return
            except OSError as exc:
                # EIO: no process has the slave open. Normal before the agent
                # starts and during an agent restart -- not a reason to die.
                if exc.errno in (errno.EIO, errno.EAGAIN):
                    return
                raise
            if written <= 0:
                return
            self._pending = self._pending[written:]
            self.stats.bytes_written += written
            if not self._pending:
                self.stats.sentences += 1

    def drain(self) -> None:
        """Discard anything the agent wrote back, so the pty never wedges.

        With the ``um980`` driver attached, the agent forwards the pit's
        RTCM corrections to this port. Nothing here can use them, but they
        must be consumed: an unread pty fills, and a full pty stops the
        sentences going the other way.
        """
        while True:
            try:
                chunk = os.read(self._master_fd, DRAIN_CHUNK_B)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EAGAIN):
                    return
                raise
            if not chunk:
                return
            self.stats.drained_bytes += len(chunk)

    def close(self) -> None:
        """Drop the symlink first, then the pty, so no stale path is left."""
        if self.link is not None:
            try:
                if os.path.islink(self.link) and os.path.realpath(self.link) == self.slave_path:
                    os.unlink(self.link)
            except OSError:
                pass
            self.link = None
        try:
            os.close(self._master_fd)
        except OSError:
            pass


def _publish_link(link: str | Path, target: str) -> str:
    """Point ``link`` at ``target``, replacing an existing symlink atomically.

    Refuses to touch anything that is not already a symlink: the default
    lives in ``/tmp`` and clobbering a real file there would be a surprising
    way for a bench tool to behave.
    """
    path = Path(link)
    if path.exists() and not path.is_symlink():
        raise ValueError(f"{path} exists and is not a symlink; refusing to replace it")
    staging = path.with_name(f".{path.name}.{os.getpid()}")
    os.symlink(target, staging)
    os.replace(staging, path)
    return str(path)


def feed(
    feeder: PtyFeeder,
    fixes: Iterator[Fix],
    *,
    rate_hz: float,
    seconds: float | None,
    stop: threading.Event,
    status_interval_s: float,
    on_status=None,
) -> FeederStats:
    """Write ``fixes`` at ``rate_hz`` until they run out, time runs out, or stop.

    The schedule is absolute rather than sleep-per-iteration so a slow tick
    does not accumulate into drift; a tick that is already late is counted
    and the schedule re-anchored, because silently catching up would burst
    sentences at a rate the bench is not supposed to be offering.
    """
    period_s = 1.0 / rate_hz
    started = time.monotonic()
    deadline = None if seconds is None else started + seconds
    next_due = started
    next_status = started + status_interval_s
    for fix in fixes:
        now = time.monotonic()
        if stop.is_set() or (deadline is not None and now >= deadline):
            break
        feeder.drain()
        feeder.offer(rmc_sentence(*fix))
        if on_status is not None and now >= next_status:
            on_status(feeder.stats, now - started)
            next_status = now + status_interval_s
        next_due += period_s
        remaining = next_due - time.monotonic()
        if remaining > 0:
            stop.wait(remaining)
        else:
            feeder.stats.late_ticks += 1
            next_due = time.monotonic()
    feeder.flush()
    feeder.drain()
    return feeder.stats


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--link",
        default=DEFAULT_LINK,
        help="stable symlink to the pty a profile can name (default: %(default)s)",
    )
    parser.add_argument(
        "--rate-hz",
        type=float,
        default=DEFAULT_RATE_HZ,
        help="sentences per second (default: %(default)s, the UM980's configured rate)",
    )
    parser.add_argument(
        "--trace",
        default=str(DEFAULT_TRACE),
        help="t_s,lat,lon,speed_kmh,heading_deg CSV to drive positions from (default: %(default)s)",
    )
    parser.add_argument(
        "--loop", action="store_true", help="wrap to the start of the trace and keep going"
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="stop after this long (default: run until "
        "interrupted, or until the trace ends when --loop is not given)",
    )
    parser.add_argument(
        "--status-interval",
        type=float,
        default=DEFAULT_STATUS_INTERVAL_S,
        help="seconds between progress lines on stderr, 0 to silence (default: %(default)s)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.rate_hz <= 0:
        print("bench_gps: --rate-hz must be greater than zero", file=sys.stderr)
        return 2
    try:
        trace = load_trace(args.trace)
    except (OSError, ValueError, KeyError) as exc:
        print(f"bench_gps: {exc}", file=sys.stderr)
        return 2

    stop = threading.Event()
    for signal_name in ("SIGINT", "SIGTERM"):
        signal.signal(getattr(signal, signal_name), lambda *_: stop.set())

    try:
        feeder = PtyFeeder(args.link)
    except (OSError, ValueError) as exc:
        print(f"bench_gps: {exc}", file=sys.stderr)
        return 2

    def status(stats: FeederStats, elapsed_s: float) -> None:
        print(
            f"bench_gps: {elapsed_s:6.1f}s  {stats.sentences} sentences "
            f"({stats.sentences / max(elapsed_s, 1e-9):.1f}/s)  "
            f"dropped={stats.dropped}  late={stats.late_ticks}  "
            f"rtcm_in={stats.drained_bytes} B",
            file=sys.stderr,
        )

    print(
        f"bench_gps: {feeder.slave_path}"
        + (f" -> {feeder.link}" if feeder.link else "")
        + f"  {args.rate_hz:g} Hz  trace={Path(args.trace).name} "
        f"({trace.duration_s:.1f}s, {len(trace.times)} fixes)" + ("  looping" if args.loop else ""),
        file=sys.stderr,
    )
    started = time.monotonic()
    try:
        stats = feed(
            feeder,
            fixes_at(trace, args.rate_hz, loop=args.loop),
            rate_hz=args.rate_hz,
            seconds=args.seconds,
            stop=stop,
            status_interval_s=args.status_interval,
            on_status=status if args.status_interval > 0 else None,
        )
    finally:
        feeder.close()
    elapsed_s = time.monotonic() - started
    print(
        f"bench_gps: done -- {stats.sentences} sentences in {elapsed_s:.1f}s "
        f"({stats.sentences / max(elapsed_s, 1e-9):.1f}/s), {stats.bytes_written} B out, "
        f"{stats.dropped} dropped, {stats.late_ticks} late tick(s), "
        f"{stats.drained_bytes} B of write-back discarded",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
