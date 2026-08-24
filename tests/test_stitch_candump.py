"""tools/stitch_candump.py: repeated copies stay a monotonic, cadence-true log.

The tool exists so a short CAN capture can span a full replay cycle; what
matters is that the output is still a log `tools/replay.py` will accept —
timestamps strictly increasing across every copy boundary, everything after
the timestamp byte-identical to the source — and that a malformed input
fails loudly instead of producing a silently shorter capture.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import stitch_candump  # noqa: E402

FRAMES = [
    "(1000.000000) can0 360#0000040300510000",
    "(1000.050000) can0 361#041B03F500000000",
    "(1002.000000) can0 3E0#DEADBEEF",
]


def write_log(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n")
    return path


def read_stamps(path: Path) -> list[float]:
    return [float(line.split(")")[0][1:]) for line in path.read_text().splitlines()]


def test_stitch_repeats_with_monotonic_timestamps(tmp_path):
    source = write_log(tmp_path / "in.log", FRAMES)
    destination = tmp_path / "out.log"
    assert stitch_candump.main([str(source), "--copies", "3", "--output", str(destination)]) == 0

    stamps = read_stamps(destination)
    assert len(stamps) == 3 * len(FRAMES)
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)

    # Each copy keeps the source cadence, and each seam is exactly the gap:
    # copy k starts at k * (span + gap), one gap after the previous copy ends.
    deltas = [round(b - a, 6) for a, b in zip(stamps, stamps[1:], strict=False)]
    intra = [0.05, 1.95]
    seam = [pytest.approx(0.2)]
    assert deltas == intra + seam + intra + seam + intra


def test_payloads_survive_verbatim(tmp_path):
    source = write_log(tmp_path / "in.log", FRAMES)
    destination = tmp_path / "out.log"
    assert stitch_candump.main([str(source), "--copies", "2", "--output", str(destination)]) == 0

    rests = [line.split(") ", 1)[1] for line in destination.read_text().splitlines()]
    expected = [line.split(") ", 1)[1] for line in FRAMES]
    assert rests == expected * 2


def test_target_seconds_picks_enough_copies(tmp_path):
    source = write_log(tmp_path / "in.log", FRAMES)  # span 2.0s, +0.2 gap
    destination = tmp_path / "out.log"
    code = stitch_candump.main([str(source), "--target-seconds", "9", "--output", str(destination)])
    assert code == 0
    # ceil(9 / 2.2) = 5 copies -> 5 * 2.0 + 4 * 0.2 = 10.8s total span.
    stamps = read_stamps(destination)
    assert len(stamps) == 5 * len(FRAMES)
    assert stamps[-1] - stamps[0] == pytest.approx(10.8)


def test_malformed_line_fails_loudly(tmp_path):
    source = write_log(tmp_path / "in.log", FRAMES + ["not a candump line"])
    destination = tmp_path / "out.log"
    code = stitch_candump.main([str(source), "--copies", "2", "--output", str(destination)])
    assert code == 2
    assert not destination.exists()


def test_refuses_to_overwrite_input(tmp_path):
    source = write_log(tmp_path / "in.log", FRAMES)
    assert stitch_candump.main([str(source), "--copies", "2", "--output", str(source)]) == 2
    assert source.read_text().splitlines() == FRAMES
