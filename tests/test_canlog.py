"""canlog relay, restart, and stop behaviour driven by fake candump commands."""

import json
import threading
import time
from pathlib import Path

import pytest

from collectors.canlog import CanLogService, main


def _run_lines(base_dir: Path, interface: str) -> bytes:
    """Concatenated capture bytes, empty while the run has not appeared yet."""
    source_dir = base_dir / interface
    if not source_dir.is_dir():
        return b""
    runs = sorted(source_dir.iterdir())
    assert len(runs) <= 1, "one service run means at most one capture run directory"
    if not runs:
        return b""
    return b"".join(path.read_bytes() for path in sorted(runs[0].glob("*.log")))


def _wait_for(predicate, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached in time")


def test_child_exit_is_retried_into_the_same_run(tmp_path: Path):
    service = CanLogService(
        "can0",
        tmp_path,
        command=["sh", "-c", "printf '(1.0) can0 123#00\\n(2.0) can0 123#01\\n'"],
        backoff_start_s=0.01,
        backoff_max_s=0.01,
        fsync_interval_s=0.0,
    )
    thread = threading.Thread(target=service.run)
    thread.start()
    try:
        _wait_for(lambda: _run_lines(tmp_path, "can0").count(b"\n") >= 4)
    finally:
        service.stop()
        thread.join(timeout=5.0)
    assert not thread.is_alive()

    lines = _run_lines(tmp_path, "can0").splitlines()
    assert lines[0] == b"(1.0) can0 123#00"
    assert lines[1] == b"(2.0) can0 123#01"
    assert len(lines) >= 4, "the exited command was respawned into the same run"

    runs = sorted((tmp_path / "can0").iterdir())
    manifest = json.loads((runs[0] / "manifest.json").read_text())
    assert manifest["source"] == "can0"
    assert manifest["format"] == "candump-log"


def test_stop_terminates_a_running_child_and_keeps_its_output(tmp_path: Path):
    service = CanLogService(
        "can0",
        tmp_path,
        # exec so SIGTERM reaches the process holding stdout, like candump.
        command=["sh", "-c", "printf '(1.0) can0 1F5#FF\\n'; exec sleep 30"],
        backoff_start_s=0.01,
        backoff_max_s=0.01,
        fsync_interval_s=0.0,
    )
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(service.run()))
    thread.start()
    try:
        _wait_for(lambda: b"1F5#FF" in _run_lines(tmp_path, "can0"))
    finally:
        service.stop()
        thread.join(timeout=10.0)

    assert not thread.is_alive(), "stop() must not wait out the child's sleep"
    assert result == [0]
    assert _run_lines(tmp_path, "can0") == b"(1.0) can0 1F5#FF\n"


def test_unspawnable_command_returns_after_stop_without_raising(tmp_path: Path):
    service = CanLogService(
        "can0",
        tmp_path,
        command=["/nonexistent/candump", "-L", "can0"],
        backoff_start_s=0.01,
        backoff_max_s=0.01,
        fsync_interval_s=0.0,
    )
    thread = threading.Thread(target=service.run)
    thread.start()
    time.sleep(0.05)
    service.stop()
    thread.join(timeout=5.0)
    assert not thread.is_alive()


def test_main_requires_a_capture_directory(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENLAPS_RAW_CAPTURE_DIR", raising=False)
    with pytest.raises(SystemExit):
        main(["can0"])
