#!/usr/bin/env python3
"""Bring every socketCAN interface the profile names up, at its own bitrate.

The agent configures no bitrate and brings no link up -- that is host state,
and `docs/AGENT_DESIGN.md` keeps it that way deliberately. But the bitrate is
*not* host state: it is a property of the car's bus, it is already written in
`vehicle.yaml`, and until this tool existed it was also written by hand into
`deploy/systemd/openlaps-agent.service`, into `deploy/README.md`, and into
whatever the operator typed at the track. Three copies of one number, and the
two that are not the profile are the ones nobody re-reads when a bus changes.

So: same resolved configuration the agent uses -- profile plus the target's
host-wiring overlay (`core.hardware`) -- and one `ip link` per bus.

    sudo ./.venv/bin/python tools/can_up.py --profile profiles/example-club-racer \\
        --hardware deploy/targets/luckfox-omni3576/hardware.yaml

Named through the venv's interpreter rather than the shebang or `uv run`,
because this has to run as root: the shebang finds the system python, which
does not have this project's dependencies, and `uv run` under sudo resolves
against root's environment rather than the operator's.

`--dry-run` prints the commands instead of running them, which is also what
the tests assert against. Interfaces already up are left alone rather than
bounced: re-running this must never drop a bus mid-session, and `ip link set
<dev> type can bitrate ...` on a running interface fails anyway.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.config import ConfigError, load_profile  # noqa: E402
from core.hardware import CanLinkConfig, load_hardware  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
IP_BINARY_CANDIDATES = ("/sbin/ip", "/usr/sbin/ip", "ip")
_OPERSTATE = Path("/sys/class/net")


def ip_binary() -> str:
    """The `ip` to call: systemd units run with a minimal PATH, so look."""
    for candidate in IP_BINARY_CANDIDATES:
        resolved = shutil.which(candidate)
        if resolved is not None:
            return resolved
    return "ip"


def is_up(interface: str) -> bool:
    """Whether `interface` is already administratively up.

    Read from sysfs rather than parsed out of `ip link show`: a CAN interface
    with no transceiver attached reports `state DOWN` in the operstate sense
    while being perfectly `UP` administratively, and it is the administrative
    flag that decides whether reconfiguring it will fail.
    """
    try:
        flags = (_OPERSTATE / interface / "flags").read_text().strip()
    except OSError:
        return False
    return bool(int(flags, 16) & 0x1)  # IFF_UP


@dataclass(frozen=True, slots=True)
class BringUp:
    """One interface, its arbitration bitrate, and the board's link options."""

    interface: str
    bitrate: int
    link: CanLinkConfig

    def command(self, binary: str) -> list[str]:
        """The `ip link` invocation this interface needs on this board."""
        argv = [binary, "link", "set", self.interface, "up", "type", "can"]
        argv += ["bitrate", str(self.bitrate)]
        # Order matters to nothing here, but `fd on` before `dbitrate` reads
        # the way the failure does: a data bitrate without FD is refused.
        if self.link.fd:
            argv += ["fd", "on"]
        if self.link.dbitrate is not None:
            argv += ["dbitrate", str(self.link.dbitrate)]
        return argv


def main(argv: list[str] | None = None) -> int:
    """Bring up the profile's CAN interfaces; return 0 when all are up."""
    parser = argparse.ArgumentParser(prog="openlaps-can-up", description=__doc__)
    parser.add_argument(
        "--profile",
        default=os.environ.get("OPENLAPS_PROFILE"),
        help="profile directory (default: $OPENLAPS_PROFILE)",
    )
    parser.add_argument(
        "--hardware",
        default=os.environ.get("OPENLAPS_HARDWARE") or None,
        help="host-wiring overlay for this board (default: $OPENLAPS_HARDWARE)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the ip commands instead of running them",
    )
    args = parser.parse_args(argv)

    if not args.profile:
        print(
            "openlaps-can-up: no profile: pass --profile or set OPENLAPS_PROFILE", file=sys.stderr
        )
        return 2
    try:
        profile = load_profile(args.profile, args.hardware)
    except (ConfigError, ValueError) as exc:
        print(f"openlaps-can-up: {exc}", file=sys.stderr)
        return 2

    # The overlay is read a second time here, on purpose: `link` options are
    # arguments to `ip`, not fields of the profile, so `load_profile` has
    # deliberately not carried them through.
    overlay = load_hardware(args.hardware) if args.hardware else None
    pending = [
        BringUp(
            bus.interface,
            bus.bitrate,
            overlay.link(bus.name) if overlay is not None else CanLinkConfig(),
        )
        for bus in profile.vehicle.buses
        if args.dry_run or not is_up(bus.interface)
    ]
    for bus in profile.vehicle.buses:
        if not args.dry_run and is_up(bus.interface):
            print(f"{bus.interface}: already up, left alone")

    binary = ip_binary()
    failures = 0
    for bring_up in pending:
        command = bring_up.command(binary)
        if args.dry_run:
            print(" ".join(command))
            continue
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"{bring_up.interface}: up at {bring_up.bitrate} bit/s")
        else:
            failures += 1
            detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
            print(f"{bring_up.interface}: {detail}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
