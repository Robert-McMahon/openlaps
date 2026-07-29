"""tools/compare_timing.py: the parity diff, and the ways it could lie.

The tool's job is to say whether a replay reproduced the June-2025 event's
timing. The two ways it could say "yes" wrongly are a fitted clock offset that
absorbs a real error, and an alignment that pairs the wrong crossings; the two
ways it could say "no" wrongly are the same faults in the other direction.
Both are exercised here against synthetic sequences whose answer is known.

The real export lives outside the repository, so nothing here reads it.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import compare_timing as compare  # noqa: E402

EPOCH = 1_749_786_245.0
LAP_TIME = 80.0
REPLAY_OFFSET = 1_234_567.891


def _lap_time(index: int) -> float:
    """Laps of slightly different lengths, as real ones are.

    Perfectly periodic crossings make the clock offset genuinely ambiguous by
    a whole lap -- no estimator can tell D from D±lap on such a sequence -- so
    a fixture with identical laps would be testing an impossible problem.
    """
    return LAP_TIME + (index % 5) * 0.7 - (index % 3) * 0.4


def _reference(count: int = 12) -> list[compare.Crossing]:
    """A run of start/finish crossings roughly one lap apart, numbered from 1."""
    crossings = []
    at = EPOCH
    for index in range(count):
        lap_time = _lap_time(index)
        at += lap_time
        crossings.append(
            compare.Crossing(
                line="StartFinish",
                time=at,
                lap_number=index + 1,
                lap_time_s=lap_time,
                valid=True,
            )
        )
    return crossings


def _subject(
    reference: list[compare.Crossing], *, offset: float = REPLAY_OFFSET, jitter: float = 0.0
) -> list[compare.Crossing]:
    return [
        compare.Crossing(
            line=crossing.line,
            time=crossing.time + offset + jitter * (1 if index % 2 else -1),
            lap_number=crossing.lap_number,
            lap_time_s=crossing.lap_time_s,
            valid=crossing.valid,
        )
        for index, crossing in enumerate(reference)
    ]


# --- offset fitting ---------------------------------------------------------


def test_offset_is_fitted_from_the_data():
    reference = _reference()

    fitted = compare.estimate_offset(_subject(reference), reference)

    assert abs(fitted - REPLAY_OFFSET) < 1e-6


def test_a_missing_first_crossing_does_not_drag_the_offset_by_a_lap():
    """The reason the fit is modal rather than a mean or a first-element diff."""
    reference = _reference()
    subject = _subject(reference)[1:]

    fitted = compare.estimate_offset(subject, reference)

    assert abs(fitted - REPLAY_OFFSET) < 1e-6


def test_offset_survives_a_run_with_no_overlap():
    assert compare.estimate_offset([], _reference()) == 0.0


# --- alignment --------------------------------------------------------------


def test_matching_pairs_every_crossing_when_the_sequences_agree():
    reference = _reference()

    result = compare.match(_subject(reference), reference, offset=REPLAY_OFFSET, tolerance=1.0)

    assert len(result.matched) == len(reference)
    assert not result.missing and not result.extra


def test_a_dropped_crossing_reads_as_missing_not_as_a_shifted_run():
    reference = _reference()
    subject = _subject(reference)
    del subject[5]

    result = compare.match(subject, reference, offset=REPLAY_OFFSET, tolerance=1.0)

    assert len(result.matched) == len(reference) - 1
    assert [crossing.lap_number for crossing in result.missing] == [6]
    assert not result.extra


def test_a_spurious_crossing_reads_as_extra():
    reference = _reference()
    subject = _subject(reference)
    subject.insert(
        4, compare.Crossing(line="StartFinish", time=subject[3].time + 30.0, lap_number=99)
    )

    result = compare.match(subject, reference, offset=REPLAY_OFFSET, tolerance=1.0)

    assert len(result.matched) == len(reference)
    assert [crossing.lap_number for crossing in result.extra] == [99]
    assert not result.missing


# --- the report -------------------------------------------------------------


def _validation_rows(reference: list[compare.Crossing], *, live_error: float = 0.02):
    """The export's shape: `old_*` from the live system, `new_*` from its replay."""
    rows = [
        {
            "line": "StartFinish",
            "old_time": f"{crossing.time - live_error:.6f}",
            "new_time": f"{crossing.time:.6f}",
            "dt": f"{live_error:.6f}",
            "old_lap_number": str(crossing.lap_number),
            "new_lap_number": str(crossing.lap_number),
            "old_lap_time_s": f"{crossing.lap_time_s - live_error:.6f}",
            "new_lap_time_s": f"{crossing.lap_time_s:.6f}",
            "old_sector_time_s": "",
            "new_split_s": "0.0",
            "old_pit_status": "track",
            "new_pit_status": "track",
            "new_valid": "1",
        }
        for crossing in reference
    ]
    # The event's very first crossing opens a lap and closes none, so the
    # export carries it with `old_*` only. It must not read as a missing lap.
    rows.append(
        {
            **rows[0],
            "old_time": f"{EPOCH:.6f}",
            "new_time": "",
            "dt": "",
            "new_lap_number": "",
            "old_lap_time_s": "",
            "new_lap_time_s": "",
            "new_valid": "",
        }
    )
    return rows


def _write_validation(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _report(subject: list[compare.Crossing], rows: list[dict[str, str]], tmp_path: Path):
    old, new, _ = compare.load_validation(_write_validation(tmp_path / "validation.csv", rows))
    return compare.compare(
        laps=subject,
        events=subject,
        validation_old=old,
        validation_new=new,
        tolerance=1.0,
    )


def test_a_faithful_replay_passes_the_gate(tmp_path: Path):
    reference = _reference()
    report = _report(_subject(reference, jitter=0.003), _validation_rows(reference), tmp_path)

    assert report["gate"]["pass"], report["gate"]["checks"]
    assert report["laps"]["matched"] == len(reference)
    assert report["laps"]["missing"] == 0
    assert report["laps"]["extra"] == 0
    assert report["laps"]["lap_number_offsets"] == {"0": len(reference)}


def test_the_opening_crossing_is_not_counted_as_a_missing_lap(tmp_path: Path):
    reference = _reference()
    rows = _validation_rows(reference)
    report = _report(_subject(reference), rows, tmp_path)

    assert report["laps"]["reference"] == len(reference)
    assert report["laps"]["missing"] == 0


def test_a_dropped_lap_fails_the_gate(tmp_path: Path):
    reference = _reference()
    subject = _subject(reference)
    del subject[7]

    report = _report(subject, _validation_rows(reference), tmp_path)

    assert not report["gate"]["pass"]
    assert report["gate"]["checks"]["no_missing_laps"] is False
    assert report["laps"]["missing"] == 1
    assert report["missing_examples"][0]["lap_number"] == 8


def test_a_sub_millisecond_excess_is_reported_but_does_not_fail_the_gate(tmp_path: Path):
    """The gate's floor is the RMC encoding's own resolution, not a fudge --
    but the un-allowanced comparison has to stay visible either way."""
    reference = _reference()
    excess = 0.0004
    subject = [
        compare.Crossing(
            line=crossing.line,
            time=crossing.time + REPLAY_OFFSET,
            lap_number=crossing.lap_number,
            lap_time_s=(crossing.lap_time_s or 0.0) + excess,
            valid=True,
        )
        for crossing in reference
    ]

    report = _report(subject, _validation_rows(reference), tmp_path)

    assert report["gate"]["pass"]
    assert report["gate"]["checks"]["lap_time_p50_no_worse_than_predecessor"] is True
    assert report["gate"]["strict"]["lap_time_p50"] is False
    assert abs(report["gate"]["strict"]["lap_time_p50_excess_s"] - excess) < 1e-9


def test_lap_times_worse_than_the_predecessors_own_spread_fail_the_gate(tmp_path: Path):
    reference = _reference()
    subject = [
        compare.Crossing(
            line=crossing.line,
            time=crossing.time + REPLAY_OFFSET,
            lap_number=crossing.lap_number,
            # The export's own live-vs-replay lap-time disagreement is 0.02 s;
            # half a second is unambiguously worse than the system replaced.
            lap_time_s=(crossing.lap_time_s or 0.0) + 0.5,
            valid=True,
        )
        for crossing in reference
    ]

    report = _report(subject, _validation_rows(reference), tmp_path)

    assert not report["gate"]["pass"]
    assert report["gate"]["checks"]["lap_time_p50_no_worse_than_predecessor"] is False


def test_renumbered_laps_fail_the_numbering_check(tmp_path: Path):
    reference = _reference()
    subject = _subject(reference)
    subject[6] = compare.Crossing(
        line=subject[6].line,
        time=subject[6].time,
        lap_number=99,
        lap_time_s=subject[6].lap_time_s,
    )

    report = _report(subject, _validation_rows(reference), tmp_path)

    assert report["gate"]["checks"]["consistent_lap_numbering"] is False


def test_the_crossing_census_covers_every_timing_line(tmp_path: Path):
    reference = _reference(4)
    rows = _validation_rows(reference)
    pit = {
        **rows[0],
        "line": "PitEntry",
        "old_time": f"{EPOCH + 200.0:.6f}",
        "new_time": f"{EPOCH + 200.0:.6f}",
        "old_lap_time_s": "",
        "new_lap_time_s": "",
    }
    rows.append(pit)
    events = _subject(reference) + [
        compare.Crossing(line="PitEntry", time=EPOCH + 200.0 + REPLAY_OFFSET, lap_number=3)
    ]

    old, new, _ = compare.load_validation(_write_validation(tmp_path / "validation.csv", rows))
    report = compare.compare(
        laps=_subject(reference),
        events=events,
        validation_old=old,
        validation_new=new,
        tolerance=1.0,
    )

    census = report["crossings"]
    assert census["PitEntry"]["matched"] == 1
    assert census["PitEntry"]["missing"] == 0
    assert census["PitEntry"]["extra"] == 0
    assert report["gate"]["checks"]["no_missing_or_extra_crossings"] is True


def test_the_start_finish_sector_and_lap_events_count_as_one_crossing():
    """The engine emits both at the same instant; two would read as an extra."""
    payloads = [
        '{"type":"sector_completed","line":"StartFinish","time":100.0,"lap_number":3,"sector":3}',
        '{"type":"lap_completed","line":"StartFinish","time":100.0,"lap_number":3,"lap_time":80.0}',
    ]
    crossings = _crossings_from(payloads)

    assert len(crossings) == 1
    assert crossings[0].line == "StartFinish"


def _crossings_from(payloads: list[str]) -> list[compare.Crossing]:
    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, *_args):
            return None

        def __iter__(self):
            return iter((payload,) for payload in payloads)

    class _Connection:
        def cursor(self):
            return _Cursor()

    return compare.load_events(_Connection(), "example-club-racer-parity")


def test_summarise_reports_the_tail_not_just_the_mean():
    stats = compare.summarise([0.01, -0.01, 0.02, -0.5])

    assert stats["n"] == 4
    assert stats["abs_max"] == 0.5
    assert stats["abs_p50"] < 0.05
