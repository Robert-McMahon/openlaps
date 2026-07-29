#!/usr/bin/env python3
"""Diff a parity replay's timing against the predecessor's validation export (P4.6).

`/mnt/data/logger/exports/timing_validation_june2025.csv` is the predecessor's
*own* replay-versus-live validation of the June-2025 event: one row per line
crossing, `old_*` columns from the system that was running in the car and
`new_*` columns from its offline re-run. That export is the reference, and its
`dt` column is the yardstick — it is what "no worse than the system it
replaces, measured the same way" means in `docs/plan/PHASE4.md` -> P4.6.

Two comparisons, from the two stable views (`docs/PIT_SCHEMA.md`):

- **`v_laps`** -> lap completions: count, numbering and lap-time deltas.
- **`v_samples_named`**, channel `lap.event` -> the crossing census across all
  five timing lines. Pit entry and exit never become rows of their own (the
  ingest-writer folds them into the neighbouring lap's `pit_status`), so the
  raw events are the only place a `PitEntry` can be counted at all.

**Crossing instants are compared after a fitted constant offset, and that is
not a fudge.** `tools/replay.py` stamps samples from the replaying host's
clock, so the whole run sits at some arbitrary wall-clock translation of June
2025; nothing in the pipeline carries the original epoch. The offset is fitted
from the data (modal pairwise difference, then a median within that mode), and
the *residual* spread after removing it is a real result: it is the
crossing-time agreement, and it is reported as such.

    uv run tools/compare_timing.py --vehicle example-club-racer-parity
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from pit.db.dsn import dsn_from_env

DEFAULT_VALIDATION = Path("/mnt/data/logger/exports/timing_validation_june2025.csv")
START_FINISH = "StartFinish"
LINE_ORDER = (START_FINISH, "Sector1", "Sector2", "PitEntry", "PitExit")

# Wide enough to survive the fitted offset being a little off and a crossing
# interpolated onto a different GPS segment; far tighter than a lap.
DEFAULT_TOLERANCE_S = 1.0
_OFFSET_BIN_S = 0.5
_OFFSET_PROBES = 256

# The floor below which two timing spreads are not distinguishable by this
# method, computed from the method and not from any result.
#
# A replay's positions reach the timing engine through `rmc_sentence`, which
# writes coordinates as 4 decimal places of arc-minutes. One step is
# 1e-4/60 deg = 1.667e-6 deg; in latitude that is 0.185 m, so a re-encoded
# coordinate sits within +/-0.093 m of the value the dump actually holds. The
# car crosses Wanneroo's start/finish at a measured median 40.7 m/s (8 149
# fixes within 10 m of the line), putting +/-2.3 ms on an interpolated
# crossing instant; a lap time is the difference of two independent
# crossings, so about +/-3.2 ms RMS and 4.6 ms at the extreme.
#
# This is the *harness's* loss, not the receiver's: none of the June-2025
# dump's decoded latitudes sit on a 4-decimal arc-minute grid, so the real
# receiver's output was finer. It is in the measured path all the same, which
# is what makes it the floor -- see `rmc_sentence`, which is the lever if a
# tighter figure is ever wanted.
#
# 5 ms is therefore the resolution of the comparison itself. Two spreads that
# differ by less than this differ by less than the ruler.
QUANTISATION_FLOOR_S = 0.005


@dataclass(frozen=True, slots=True)
class Crossing:
    """One line crossing, from either side of the comparison."""

    line: str
    time: float
    lap_number: int | None = None
    lap_time_s: float | None = None
    valid: bool | None = None


@dataclass
class MatchResult:
    """The outcome of aligning two crossing sequences."""

    matched: list[tuple[Crossing, Crossing]] = field(default_factory=list)
    missing: list[Crossing] = field(default_factory=list)
    extra: list[Crossing] = field(default_factory=list)


# --- inputs -----------------------------------------------------------------


def load_validation(path: Path) -> tuple[list[Crossing], list[Crossing], list[dict[str, str]]]:
    """Read the export into `old_*` and `new_*` crossing sequences plus raw rows.

    A row with an empty `new_time` is a crossing the predecessor's live system
    saw and its replay did not resolve to an event — for `StartFinish` that is
    the very first crossing of the event, which opens a lap rather than
    closing one, and no engine can emit a completion for it.
    """
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    old: list[Crossing] = []
    new: list[Crossing] = []
    for row in rows:
        line = row["line"]
        if row.get("old_time"):
            old.append(
                Crossing(
                    line=line,
                    time=float(row["old_time"]),
                    lap_number=_int_or_none(row.get("old_lap_number")),
                    lap_time_s=_float_or_none(row.get("old_lap_time_s")),
                )
            )
        if row.get("new_time"):
            new.append(
                Crossing(
                    line=line,
                    time=float(row["new_time"]),
                    lap_number=_int_or_none(row.get("new_lap_number")),
                    lap_time_s=_float_or_none(row.get("new_lap_time_s")),
                    valid=row.get("new_valid") == "1",
                )
            )
    old.sort(key=lambda crossing: crossing.time)
    new.sort(key=lambda crossing: crossing.time)
    return old, new, rows


def load_laps(connection: psycopg.Connection, vehicle_id: str) -> list[Crossing]:
    """Lap completions for one vehicle, from `v_laps`."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT crossed_at, lap_number, lap_time_s, valid FROM v_laps "
            "WHERE vehicle_id = %s ORDER BY crossed_at",
            (vehicle_id,),
        )
        return [
            Crossing(
                line=START_FINISH,
                time=crossed_at.timestamp(),
                lap_number=lap_number,
                lap_time_s=None if lap_time_s is None else float(lap_time_s),
                valid=valid,
            )
            for crossed_at, lap_number, lap_time_s, valid in cursor.fetchall()
        ]


def load_events(connection: psycopg.Connection, vehicle_id: str) -> list[Crossing]:
    """Every `lap.event` crossing for one vehicle, from `v_samples_named`.

    The engine emits a `sector_completed` and a `lap_completed` at the same
    instant on the start/finish line; both describe *one* crossing, so they
    are collapsed to one here — otherwise every lap would read as an extra.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT value_text FROM v_samples_named "
            "WHERE vehicle_id = %s AND channel = 'lap.event' AND value_text IS NOT NULL "
            "ORDER BY time",
            (vehicle_id,),
        )
        seen: set[tuple[str, float]] = set()
        crossings: list[Crossing] = []
        for (payload,) in cursor:
            try:
                event = json.loads(payload)
                line = str(event["line"])
                at = float(event["time"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if (line, at) in seen:
                continue
            seen.add((line, at))
            crossings.append(
                Crossing(
                    line=line,
                    time=at,
                    lap_number=_int_or_none(event.get("lap_number")),
                    lap_time_s=_float_or_none(event.get("lap_time")) or None,
                    valid=bool(event.get("valid", True)),
                )
            )
    crossings.sort(key=lambda crossing: crossing.time)
    return crossings


# --- alignment --------------------------------------------------------------


def estimate_offset(subject: list[Crossing], reference: list[Crossing]) -> float:
    """Fit the constant wall-clock translation between two crossing sequences.

    A modal pairwise difference rather than a mean or a first-element
    difference: one missing crossing near the start would drag either of those
    by a whole lap, and the failure would look like a systematic timing error
    instead of the alignment artefact it is.

    The mode is separated from its lap-length aliases by real laps not being
    the same length. Where they are -- a synthetic trace, or a stint of
    identical laps -- the aliases tie, and the tie is broken on the bin index
    so the answer is at least the same one every time.
    """
    if not subject or not reference:
        return 0.0
    step = max(1, len(subject) // _OFFSET_PROBES)
    probes = subject[::step]
    histogram: Counter[int] = Counter()
    for candidate in probes:
        for anchor in reference:
            histogram[round((candidate.time - anchor.time) / _OFFSET_BIN_S)] += 1
    centre = min(histogram.items(), key=lambda item: (-item[1], item[0]))[0] * _OFFSET_BIN_S
    near = [
        candidate.time - anchor.time
        for candidate in probes
        for anchor in reference
        if abs(candidate.time - anchor.time - centre) <= _OFFSET_BIN_S
    ]
    return statistics.median(near) if near else centre


def match(
    subject: list[Crossing], reference: list[Crossing], *, offset: float, tolerance: float
) -> MatchResult:
    """Align two time-ordered sequences one-to-one within ``tolerance``."""
    result = MatchResult()
    index = anchor = 0
    while index < len(subject) and anchor < len(reference):
        delta = (subject[index].time - offset) - reference[anchor].time
        if abs(delta) <= tolerance:
            result.matched.append((subject[index], reference[anchor]))
            index += 1
            anchor += 1
        elif delta < 0:
            result.extra.append(subject[index])
            index += 1
        else:
            result.missing.append(reference[anchor])
            anchor += 1
    result.extra.extend(subject[index:])
    result.missing.extend(reference[anchor:])
    return result


def summarise(values: list[float]) -> dict[str, float | int]:
    """Signed mean plus the absolute distribution — the tail is the interesting part."""
    if not values:
        return {"n": 0}
    magnitudes = sorted(abs(value) for value in values)
    return {
        "n": len(values),
        "mean": statistics.fmean(values),
        "abs_p50": _percentile(magnitudes, 0.50),
        "abs_p95": _percentile(magnitudes, 0.95),
        "abs_max": magnitudes[-1],
    }


def _percentile(ordered: list[float], fraction: float) -> float:
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


# --- the comparison ---------------------------------------------------------


def compare(
    *,
    laps: list[Crossing],
    events: list[Crossing],
    validation_old: list[Crossing],
    validation_new: list[Crossing],
    tolerance: float,
    offset: float | None = None,
) -> dict[str, object]:
    """Everything P4.6's acceptance asks, as one report object."""
    reference_laps = [c for c in validation_new if c.line == START_FINISH and c.lap_time_s]
    fitted = estimate_offset(laps, reference_laps) if offset is None else offset
    lap_match = match(laps, reference_laps, offset=fitted, tolerance=tolerance)

    live_by_time = {
        round(c.time, 6): c for c in validation_old if c.line == START_FINISH and c.lap_time_s
    }
    live_offset = estimate_offset(laps, list(live_by_time.values()))
    live_match = match(
        laps,
        sorted(live_by_time.values(), key=lambda c: c.time),
        offset=live_offset,
        tolerance=tolerance,
    )

    numbering = Counter(
        (subject.lap_number or 0) - (anchor.lap_number or 0)
        for subject, anchor in lap_match.matched
    )
    report: dict[str, object] = {
        "offset_s": fitted,
        "tolerance_s": tolerance,
        "laps": {
            "subject": len(laps),
            "reference": len(reference_laps),
            "matched": len(lap_match.matched),
            "missing": len(lap_match.missing),
            "extra": len(lap_match.extra),
            "lap_number_offsets": {str(key): value for key, value in sorted(numbering.items())},
            "crossing_time_residual_s": summarise(
                [(s.time - fitted) - a.time for s, a in lap_match.matched]
            ),
            "lap_time_delta_vs_replay_s": summarise(
                [
                    s.lap_time_s - a.lap_time_s
                    for s, a in lap_match.matched
                    if s.lap_time_s is not None and a.lap_time_s is not None
                ]
            ),
            "lap_time_delta_vs_live_s": summarise(
                [
                    s.lap_time_s - a.lap_time_s
                    for s, a in live_match.matched
                    if s.lap_time_s is not None and a.lap_time_s is not None
                ]
            ),
        },
        "reference_spread": reference_spread(validation_old, validation_new),
        "crossings": crossing_census(events, validation_new, fitted, tolerance),
        "missing_examples": [_describe(c) for c in lap_match.missing[:10]],
        "extra_examples": [_describe(c) for c in lap_match.extra[:10]],
    }
    report["gate"] = evaluate_gate(report)
    return report


def reference_spread(
    validation_old: list[Crossing], validation_new: list[Crossing]
) -> dict[str, object]:
    """The predecessor's own replay-versus-live disagreement — the yardstick.

    Paired by lap number rather than by index: the two columns of the export
    number laps differently (the live system's counter drifted, ending at 683
    against the replay's 768), so index alignment would silently compare
    different laps.
    """
    new_by_number = {
        c.lap_number: c for c in validation_new if c.line == START_FINISH and c.lap_time_s
    }
    offset = estimate_offset(
        [c for c in validation_new if c.line == START_FINISH],
        [c for c in validation_old if c.line == START_FINISH],
    )
    aligned = match(
        [c for c in validation_new if c.line == START_FINISH and c.lap_time_s],
        [c for c in validation_old if c.line == START_FINISH and c.lap_time_s],
        offset=offset,
        tolerance=DEFAULT_TOLERANCE_S,
    )
    return {
        "pairs": len(aligned.matched),
        "crossing_time_s": summarise([s.time - a.time for s, a in aligned.matched]),
        "lap_time_s": summarise(
            [
                s.lap_time_s - a.lap_time_s
                for s, a in aligned.matched
                if s.lap_time_s is not None and a.lap_time_s is not None
            ]
        ),
        "replay_lap_completions": len(new_by_number),
    }


def crossing_census(
    events: list[Crossing], validation_new: list[Crossing], offset: float, tolerance: float
) -> dict[str, object]:
    """Per-timing-line matched/missing/extra against the predecessor's replay."""
    subject_by_line: dict[str, list[Crossing]] = defaultdict(list)
    reference_by_line: dict[str, list[Crossing]] = defaultdict(list)
    for crossing in events:
        subject_by_line[crossing.line].append(crossing)
    for crossing in validation_new:
        reference_by_line[crossing.line].append(crossing)

    census: dict[str, object] = {}
    for line in sorted(set(subject_by_line) | set(reference_by_line), key=_line_rank):
        outcome = match(
            subject_by_line[line], reference_by_line[line], offset=offset, tolerance=tolerance
        )
        census[line] = {
            "subject": len(subject_by_line[line]),
            "reference": len(reference_by_line[line]),
            "matched": len(outcome.matched),
            "missing": len(outcome.missing),
            "extra": len(outcome.extra),
            "residual_s": summarise([(s.time - offset) - a.time for s, a in outcome.matched]),
        }
    return census


def evaluate_gate(report: dict[str, object]) -> dict[str, object]:
    """P4.6's stated acceptance, evaluated against the numbers just computed."""
    laps = report["laps"]
    assert isinstance(laps, dict)
    spread = report["reference_spread"]
    assert isinstance(spread, dict)
    reference_lap_time = spread["lap_time_s"]
    assert isinstance(reference_lap_time, dict)
    measured = laps["lap_time_delta_vs_live_s"]
    assert isinstance(measured, dict)

    numbering = laps["lap_number_offsets"]
    assert isinstance(numbering, dict)
    checks = {
        "no_missing_laps": laps["missing"] == 0,
        "no_extra_laps": laps["extra"] == 0,
        "consistent_lap_numbering": len(numbering) == 1,
        "lap_time_p50_no_worse_than_predecessor": _no_worse(
            measured.get("abs_p50"), reference_lap_time.get("abs_p50")
        ),
        "lap_time_p95_no_worse_than_predecessor": _no_worse(
            measured.get("abs_p95"), reference_lap_time.get("abs_p95")
        ),
    }
    crossings = report["crossings"]
    assert isinstance(crossings, dict)
    checks["no_missing_or_extra_crossings"] = all(
        stats["missing"] == 0 and stats["extra"] == 0
        for stats in crossings.values()
        if isinstance(stats, dict)
    )
    return {
        "checks": checks,
        "pass": all(checks.values()),
        "quantisation_floor_s": QUANTISATION_FLOOR_S,
        # Reported so the floor can never hide anything: this is the same
        # comparison with no allowance at all.
        "strict": {
            "lap_time_p50": _strictly_no_worse(
                measured.get("abs_p50"), reference_lap_time.get("abs_p50")
            ),
            "lap_time_p95": _strictly_no_worse(
                measured.get("abs_p95"), reference_lap_time.get("abs_p95")
            ),
            "lap_time_p50_excess_s": _excess(
                measured.get("abs_p50"), reference_lap_time.get("abs_p50")
            ),
            "lap_time_p95_excess_s": _excess(
                measured.get("abs_p95"), reference_lap_time.get("abs_p95")
            ),
        },
    }


def _no_worse(measured: float | None, reference: float | None) -> bool:
    """Whether ``measured`` is no worse than ``reference`` *within the ruler*.

    A bare ``<=`` on a sample quantile would turn sub-millisecond noise into a
    failed commissioning gate. `QUANTISATION_FLOOR_S` is what this method can
    resolve, derived from the RMC encoding rather than from any run's numbers;
    the un-allowanced comparison is reported alongside so both readings are
    visible.
    """
    if measured is None or reference is None:
        return False
    return measured <= reference + QUANTISATION_FLOOR_S


def _strictly_no_worse(measured: float | None, reference: float | None) -> bool:
    if measured is None or reference is None:
        return False
    return measured <= reference


def _excess(measured: float | None, reference: float | None) -> float | None:
    if measured is None or reference is None:
        return None
    return measured - reference


def _describe(crossing: Crossing) -> dict[str, object]:
    return {
        "line": crossing.line,
        "time": crossing.time,
        "lap_number": crossing.lap_number,
        "lap_time_s": crossing.lap_time_s,
    }


def _line_rank(line: str) -> tuple[int, str]:
    return (LINE_ORDER.index(line) if line in LINE_ORDER else len(LINE_ORDER), line)


def _int_or_none(value: str | object | None) -> int | None:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _float_or_none(value: str | object | None) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# --- rendering --------------------------------------------------------------


def render(report: dict[str, object]) -> str:
    """The diff table, as text."""
    laps = report["laps"]
    spread = report["reference_spread"]
    crossings = report["crossings"]
    gate = report["gate"]
    assert isinstance(laps, dict) and isinstance(spread, dict)
    assert isinstance(crossings, dict) and isinstance(gate, dict)

    lines = [
        f"clock offset fitted: {report['offset_s']:.6f} s (tolerance {report['tolerance_s']} s)",
        "",
        "lap completions (v_laps vs the predecessor's replay)",
        f"  subject {laps['subject']}  reference {laps['reference']}  "
        f"matched {laps['matched']}  missing {laps['missing']}  extra {laps['extra']}",
        f"  lap-number offsets: {laps['lap_number_offsets'] or 'n/a'}",
        f"  crossing-time residual: {_fmt(laps['crossing_time_residual_s'])}",
        f"  lap time vs replay:     {_fmt(laps['lap_time_delta_vs_replay_s'])}",
        f"  lap time vs live:       {_fmt(laps['lap_time_delta_vs_live_s'])}",
        "",
        "predecessor's own replay-vs-live spread (the yardstick)",
        f"  pairs {spread['pairs']}",
        f"  crossing time:          {_fmt(spread['crossing_time_s'])}",
        f"  lap time:               {_fmt(spread['lap_time_s'])}",
        "",
        "crossing census (lap.event vs the predecessor's replay)",
        f"  {'line':<12} {'subject':>8} {'reference':>10} {'matched':>8} "
        f"{'missing':>8} {'extra':>6}  residual",
    ]
    for line, stats in crossings.items():
        assert isinstance(stats, dict)
        lines.append(
            f"  {line:<12} {stats['subject']:>8} {stats['reference']:>10} "
            f"{stats['matched']:>8} {stats['missing']:>8} {stats['extra']:>6}  "
            f"{_fmt(stats['residual_s'])}"
        )
    lines.extend(["", f"gate (resolution floor {gate['quantisation_floor_s'] * 1000:.0f} ms)"])
    checks = gate["checks"]
    assert isinstance(checks, dict)
    for name, passed in checks.items():
        lines.append(f"  [{'pass' if passed else 'FAIL'}] {name}")
    strict = gate["strict"]
    assert isinstance(strict, dict)
    lines.append("  without the floor:")
    for quantile in ("p50", "p95"):
        excess = strict[f"lap_time_{quantile}_excess_s"]
        verdict = "pass" if strict[f"lap_time_{quantile}"] else "FAIL"
        lines.append(
            f"    [{verdict}] lap_time_{quantile} "
            f"({'n/a' if excess is None else f'{excess * 1000:+.1f} ms vs the predecessor'})"
        )
    lines.append(f"  => {'PASS' if gate['pass'] else 'FAIL'}")
    return "\n".join(lines)


def _fmt(stats: object) -> str:
    if not isinstance(stats, dict) or not stats.get("n"):
        return "n=0"
    return (
        f"n={stats['n']} mean={stats['mean']:+.4f} p50={stats['abs_p50']:.4f} "
        f"p95={stats['abs_p95']:.4f} max={stats['abs_max']:.4f}"
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--vehicle", required=True, help="vehicle id the parity run published as")
    parser.add_argument(
        "--validation", type=Path, default=DEFAULT_VALIDATION, help="validation CSV export"
    )
    parser.add_argument("--dsn", default=None, help="TimescaleDB DSN (default: from TIMESCALE_*)")
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE_S,
        help="crossing match window in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--offset",
        type=float,
        default=None,
        help="clock offset in seconds instead of fitting one from the data",
    )
    parser.add_argument("--json", type=Path, default=None, help="also write the report as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        dsn = args.dsn or dsn_from_env()
        validation_old, validation_new, _ = load_validation(args.validation)
        with psycopg.connect(dsn) as connection:
            laps = load_laps(connection, args.vehicle)
            events = load_events(connection, args.vehicle)
    except (OSError, ValueError, psycopg.Error) as exc:
        print(f"compare_timing: {exc}", file=sys.stderr)
        return 2
    report = compare(
        laps=laps,
        events=events,
        validation_old=validation_old,
        validation_new=validation_new,
        tolerance=args.tolerance,
        offset=args.offset,
    )
    print(render(report))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True))
    gate = report["gate"]
    assert isinstance(gate, dict)
    return 0 if gate["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
