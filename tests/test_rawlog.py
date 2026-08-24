"""RawLogWriter run layout, rotation, and close semantics."""

import json
from pathlib import Path

import pytest

from collectors.rawlog import RawLogWriter


class FakeMonotonic:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _writer(tmp_path: Path, clock: FakeMonotonic, **kwargs) -> RawLogWriter:
    return RawLogWriter(
        tmp_path,
        "can0",
        manifest={"format": "candump-log"},
        rotate_interval_s=600.0,
        fsync_interval_s=1.0,
        monotonic=clock,
        **kwargs,
    )


def test_run_directory_holds_manifest_and_first_segment(tmp_path: Path):
    clock = FakeMonotonic()
    writer = _writer(tmp_path, clock)
    writer.write_line(b"(1.000000) can0 123#00\n")
    writer.close()

    manifest = json.loads((writer.run_dir / "manifest.json").read_text())
    assert manifest["source"] == "can0"
    assert manifest["format"] == "candump-log"
    assert manifest["started_at"].endswith("+00:00")
    assert manifest["t_mono_ns"] > 0
    assert (writer.run_dir / "0001.log").read_bytes() == b"(1.000000) can0 123#00\n"
    assert writer.run_dir.parent == tmp_path / "can0"


def test_segments_rotate_on_the_interval_and_keep_every_line(tmp_path: Path):
    clock = FakeMonotonic()
    writer = _writer(tmp_path, clock)
    writer.write_line(b"a\n")
    clock.now += 601.0
    writer.write_line(b"b\n")  # crosses the rotation deadline
    writer.write_line(b"c\n")  # lands in the fresh segment
    writer.close()

    assert (writer.run_dir / "0001.log").read_bytes() == b"a\nb\n"
    assert (writer.run_dir / "0002.log").read_bytes() == b"c\n"


def test_same_second_restarts_get_distinct_run_directories(tmp_path: Path):
    clock = FakeMonotonic()
    first = _writer(tmp_path, clock)
    second = _writer(tmp_path, clock)
    first.close()
    second.close()

    assert first.run_dir != second.run_dir
    assert first.run_dir.parent == second.run_dir.parent


def test_close_is_idempotent_and_write_after_close_raises(tmp_path: Path):
    clock = FakeMonotonic()
    writer = _writer(tmp_path, clock)
    writer.close()
    writer.close()

    with pytest.raises(OSError):
        writer.write_line(b"late\n")
