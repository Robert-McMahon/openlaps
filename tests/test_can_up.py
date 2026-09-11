"""tools/can_up.py: the bitrate is the profile's, and nothing else's.

The tool exists to delete three hand-copied `1000000`s (ADR 0010), so what
matters is that the commands it issues come from the resolved configuration
-- profile plus the target's host-wiring overlay -- and that re-running it
never bounces a bus that is already up.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import can_up  # noqa: E402

ROOT = Path(__file__).parents[1]
EXAMPLE_PROFILE = ROOT / "profiles" / "example-club-racer"
TARGETS = ROOT / "deploy" / "targets"


def test_the_command_carries_the_profile_s_bitrate(capsys):
    assert can_up.main(["--profile", str(EXAMPLE_PROFILE), "--dry-run"]) == 0

    printed = capsys.readouterr().out.strip().splitlines()
    assert len(printed) == 1
    assert printed[0].split()[1:] == [
        "link",
        "set",
        "can0",
        "up",
        "type",
        "can",
        "bitrate",
        "1000000",
    ]


def test_the_overlay_decides_which_interface_is_brought_up(tmp_path, capsys):
    overlay = tmp_path / "hardware.yaml"
    overlay.write_text(
        "target: bench-box\nbuses:\n  can0: { interface: vcan7, bitrate: 500000 }\n",
        encoding="utf-8",
    )

    assert (
        can_up.main(["--profile", str(EXAMPLE_PROFILE), "--hardware", str(overlay), "--dry-run"])
        == 0
    )

    printed = capsys.readouterr().out
    assert "vcan7" in printed
    assert "bitrate 500000" in printed
    assert "can0 up" not in printed


@pytest.mark.parametrize(
    "target",
    sorted(path for path in TARGETS.iterdir() if path.is_dir()),
    ids=lambda path: path.name,
)
def test_every_shipped_target_resolves_to_a_bring_up_command(target, capsys):
    assert (
        can_up.main(
            [
                "--profile",
                str(EXAMPLE_PROFILE),
                "--hardware",
                str(target / "hardware.yaml"),
                "--dry-run",
            ]
        )
        == 0
    )

    assert "link set" in capsys.readouterr().out


def test_an_interface_already_up_is_left_alone(monkeypatch, capsys):
    """Re-running this mid-session must never drop a bus."""
    monkeypatch.setattr(can_up, "is_up", lambda interface: True)

    def fail(*args, **kwargs):  # pragma: no cover - the point is it is not called
        raise AssertionError("ip was invoked against an interface that is already up")

    monkeypatch.setattr(can_up.subprocess, "run", fail)

    assert can_up.main(["--profile", str(EXAMPLE_PROFILE)]) == 0
    assert "already up" in capsys.readouterr().out


def test_a_failed_bring_up_is_reported_and_exits_non_zero(monkeypatch, capsys):
    class _Result:
        returncode = 1
        stdout = ""
        stderr = "RTNETLINK answers: Operation not permitted"

    monkeypatch.setattr(can_up, "is_up", lambda interface: False)
    monkeypatch.setattr(can_up.subprocess, "run", lambda *a, **k: _Result())

    assert can_up.main(["--profile", str(EXAMPLE_PROFILE)]) == 1
    assert "not permitted" in capsys.readouterr().err


def test_a_missing_profile_fails_loudly(capsys):
    assert can_up.main([]) == 2
    assert "no profile" in capsys.readouterr().err


def test_a_bad_overlay_fails_before_touching_an_interface(tmp_path, capsys):
    overlay = tmp_path / "hardware.yaml"
    overlay.write_text("target: t\nbuses:\n  can9: { interface: can9 }\n", encoding="utf-8")

    assert can_up.main(["--profile", str(EXAMPLE_PROFILE), "--hardware", str(overlay)]) == 2
    assert "can9" in capsys.readouterr().err
