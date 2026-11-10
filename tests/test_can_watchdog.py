"""tools/can_watchdog.py: a stalled CAN controller gets the box rebooted.

The tool exists because the only recovery from the Luckfox's 2026-11-08 CAN
hang was a reboot, and because the obvious alternative -- bouncing the
link -- is what locked the board up. So what matters: it watches the buses
the profile names, it acts only on a *persistent* stall, it never reboots
more often than its budget, and in `log` mode it reboots nothing at all.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import can_watchdog  # noqa: E402
from can_watchdog import FrameCounter, RebootBudget, Watchdog, WatchedBus  # noqa: E402

ROOT = Path(__file__).parents[1]
EXAMPLE_PROFILE = ROOT / "profiles" / "example-club-racer"
LUCKFOX = ROOT / "deploy" / "targets" / "luckfox-omni3576" / "hardware.yaml"


class _Rig:
    """A fake board: kernel and delivered counters under the test's control."""

    def __init__(self, tmp_path: Path, action: str = "reboot", **kwargs):
        self.kernel = 1000
        self.delivered = 1000
        self.now = 100.0
        self.wall = 1_700_000_000.0
        self.reboots = 0
        self.budget = RebootBudget(tmp_path / "budget.json", max_reboots=2, window_s=3600.0)
        self.watchdog = Watchdog(
            [WatchedBus("can0", "can0")],
            action=action,
            stall_s=kwargs.pop("stall_s", 10.0),
            budget=self.budget,
            delivered={"can0": lambda: self.delivered},
            read_rx=lambda interface: self.kernel,
            describe=lambda interface: f"{interface}: state ERROR-WARNING",
            reboot=self._reboot,
            clock=lambda: self.now,
            wall=lambda: self.wall,
            **kwargs,
        )

    def _reboot(self) -> None:
        self.reboots += 1

    def tick(self, *, kernel: int = 0, delivered: int = 0, seconds: float = 1.0) -> float:
        self.now += seconds
        self.wall += seconds
        self.kernel += kernel
        self.delivered += delivered
        return self.watchdog.poll()["can0"]


def test_a_healthy_bus_is_never_acted_on(tmp_path):
    rig = _Rig(tmp_path)
    rig.tick()
    for _ in range(120):
        assert rig.tick(kernel=1500, delivered=1500) == 0.0
    assert rig.reboots == 0


def test_a_quiet_bus_is_never_acted_on(tmp_path):
    rig = _Rig(tmp_path)
    for _ in range(120):
        assert rig.tick() == 0.0
    assert rig.reboots == 0


def test_a_persistent_stall_reboots_once_the_threshold_is_reached(tmp_path, caplog):
    rig = _Rig(tmp_path)
    rig.tick()
    for second in range(1, 10):
        assert rig.tick(kernel=100) == float(second)
        assert rig.reboots == 0, "rebooted before the stall threshold"
    assert rig.tick(kernel=100) == 10.0
    assert rig.reboots == 1
    assert "STALLED" in caplog.text
    assert "ERROR-WARNING" in caplog.text  # the link state made it to the journal
    # Still polling while the reboot takes effect: no second reboot.
    rig.tick(kernel=100)
    assert rig.reboots == 1
    assert json.loads((tmp_path / "budget.json").read_text())["reboots"] == [rig.wall - 1.0]


def test_a_stall_that_clears_before_the_threshold_does_nothing(tmp_path, caplog):
    rig = _Rig(tmp_path)
    rig.tick()
    for _ in range(8):
        rig.tick(kernel=100)
    assert rig.tick(kernel=100, delivered=1) == 0.0
    for _ in range(8):
        rig.tick(kernel=100)
    assert rig.reboots == 0
    assert "STALLED" not in caplog.text


def test_log_mode_reports_the_stall_and_reboots_nothing(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    rig = _Rig(tmp_path, action="log")
    rig.tick()
    for _ in range(30):
        rig.tick(kernel=100)
    assert rig.reboots == 0
    assert caplog.text.count("STALLED") == 1, "the stall is announced once, not every second"
    rig.tick(kernel=100, delivered=5)
    assert "stall over" in caplog.text


def test_the_reboot_budget_stops_a_reboot_loop(tmp_path, caplog):
    first = _Rig(tmp_path)
    first.tick()
    for _ in range(10):
        first.tick(kernel=100)
    assert first.reboots == 1

    # The board comes back, the bus is still broken: second reboot allowed...
    second = _Rig(tmp_path)
    second.wall = first.wall + 120.0
    second.tick()
    for _ in range(10):
        second.tick(kernel=100)
    assert second.reboots == 1
    assert len(json.loads((tmp_path / "budget.json").read_text())["reboots"]) == 2

    # ...the third is not: the budget is 2 per hour.
    third = _Rig(tmp_path)
    third.wall = second.wall + 120.0
    third.tick()
    for _ in range(30):
        third.tick(kernel=100)
    assert third.reboots == 0
    assert "not rebooting" in caplog.text

    # An hour later the window has moved on and a reboot is allowed again.
    fourth = _Rig(tmp_path)
    fourth.wall = first.wall + 4000.0
    fourth.tick()
    for _ in range(10):
        fourth.tick(kernel=100)
    assert fourth.reboots == 1


def test_a_corrupt_budget_file_is_treated_as_empty(tmp_path):
    path = tmp_path / "budget.json"
    path.write_text("{not json")
    assert RebootBudget(path).allows(0.0)
    path.write_text('{"reboots": "nope"}')
    assert RebootBudget(path).allows(0.0)


def test_a_reopened_socket_does_not_inherit_a_stall(tmp_path):
    """Frames missed while our own socket was closed are not the controller's fault."""
    generation = {"n": 1}
    rig = _Rig(tmp_path, generation={"can0": lambda: generation["n"]})
    rig.tick()
    for _ in range(8):
        rig.tick(kernel=100)
    generation["n"] += 1
    assert rig.tick(kernel=100) == 0.0
    assert rig.tick(kernel=100) == 1.0


def test_an_unknown_action_is_refused(tmp_path):
    with pytest.raises(ValueError):
        _Rig(tmp_path, action="bounce")


def test_the_frame_counter_counts_and_reopens(monkeypatch):
    class _Bus:
        opens = 0

        def __init__(self):
            _Bus.opens += 1
            self.frames = iter([object(), object(), None, object()])

        def recv(self, timeout):
            try:
                return next(self.frames)
            except StopIteration:
                raise OSError("Network is down") from None

        def shutdown(self):
            pass

    monkeypatch.setattr(can_watchdog, "_OPEN_RETRY_S", 0.01)
    counter = FrameCounter("can0", lambda interface: _Bus())
    counter.start()
    try:
        deadline = time.monotonic() + 5.0
        while counter.generation < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        counter.stop()
    assert counter.count >= 3
    assert counter.generation >= 2


# -- the command line ------------------------------------------------------------


def test_dry_run_names_the_profile_s_buses_and_the_action(tmp_path, capsys):
    assert (
        can_watchdog.main(
            [
                "--profile",
                str(EXAMPLE_PROFILE),
                "--hardware",
                str(LUCKFOX),
                "--state-dir",
                str(tmp_path),
                "--dry-run",
            ]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "watch can0 on can0" in printed
    assert "-> log" in printed  # the default: alert-only until told otherwise
    assert str(tmp_path / can_watchdog.BUDGET_FILE) in printed


def test_the_action_defaults_from_the_environment(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("OPENLAPS_CAN_WATCHDOG_ACTION", "reboot")
    assert (
        can_watchdog.main(
            ["--profile", str(EXAMPLE_PROFILE), "--state-dir", str(tmp_path), "--dry-run"]
        )
        == 0
    )
    assert "-> reboot" in capsys.readouterr().out


def test_the_overlay_decides_which_interface_is_watched(tmp_path, capsys):
    overlay = tmp_path / "hardware.yaml"
    overlay.write_text(
        "target: bench-box\nbuses:\n  can0: { interface: vcan7, bitrate: 500000 }\n",
        encoding="utf-8",
    )
    assert (
        can_watchdog.main(
            [
                "--profile",
                str(EXAMPLE_PROFILE),
                "--hardware",
                str(overlay),
                "--state-dir",
                str(tmp_path),
                "--dry-run",
            ]
        )
        == 0
    )
    assert "watch can0 on vcan7" in capsys.readouterr().out


def test_a_missing_profile_fails_loudly(capsys, monkeypatch):
    monkeypatch.delenv("OPENLAPS_PROFILE", raising=False)
    assert can_watchdog.main(["--dry-run"]) == 2
    assert "no profile" in capsys.readouterr().err


def test_a_bad_overlay_fails_before_opening_a_socket(tmp_path, capsys):
    overlay = tmp_path / "hardware.yaml"
    overlay.write_text("target: t\nbuses:\n  can9: { interface: can9 }\n", encoding="utf-8")
    assert (
        can_watchdog.main(
            ["--profile", str(EXAMPLE_PROFILE), "--hardware", str(overlay), "--dry-run"]
        )
        == 2
    )
    assert "can9" in capsys.readouterr().err
