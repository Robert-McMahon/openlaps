#!/usr/bin/env python3
"""Reboot the vehicle box when a CAN controller stalls and will not recover.

On 2026-11-08 the Luckfox Omni3576's `rk3576_canfd` controller got stuck in a
bus-error state: the kernel kept counting ~100 frames/s on `can0` and
delivered none of them to any socket, for hours, with the car's ECU still
talking. `ip link set can0 down` -- the obvious fix -- hung inside the driver
holding the rtnl lock, which blocked every new SSH login until the board was
power cycled. The only recovery is a reboot, and the only thing that must
never be tried is bouncing the link.

So this is a watchdog that **never touches the interface**. It opens its own
raw socket on every bus the profile names, counts the frames it receives,
and compares that with the kernel's `rx_packets` counter once a second
(`collectors.can_stall` holds the definition, shared with the agent's
`sys.agent.stall_s.<bus>` channel so the pit and this tool agree). When the
kernel has been counting frames that nobody receives for `--stall-s`
seconds it declares a stall and acts:

    --action log     say so in the journal and keep watching (bring-up)
    --action reboot  `systemctl reboot`, then `/proc/sysrq-trigger` if that
                     has not taken the box down within --reboot-timeout-s
                     (race weekend)

The default is `log`, or `$OPENLAPS_CAN_WATCHDOG_ACTION` when set, so the
installed unit switches mode from `/etc/openlaps/agent.env` without an edit
here. Reboots are budgeted: at most `--max-reboots` per `--window-s`,
remembered across reboots in a small file under `--state-dir`, so a bus that
is genuinely broken (a wiring fault streaming error frames) costs a few
reboots and then a journal entry, not a reboot loop that nobody can log in
to stop.

Run as root, through the venv's interpreter, exactly like `tools/can_up.py`:

    sudo ./.venv/bin/python tools/can_watchdog.py --profile profiles/example-club-racer \\
        --hardware deploy/targets/luckfox-omni3576/hardware.yaml --action log

`--dry-run` prints the buses it would watch and what it would do, and exits.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from collectors.can_stall import StallDetector, read_rx_packets  # noqa: E402
from core.config import ConfigError, load_profile  # noqa: E402

logger = logging.getLogger("openlaps-can-watchdog")

DEFAULT_INTERVAL_S = 1.0
DEFAULT_STALL_S = 10.0
DEFAULT_MAX_REBOOTS = 3
DEFAULT_WINDOW_S = 3600.0
DEFAULT_REBOOT_TIMEOUT_S = 60.0
DEFAULT_STATE_DIR = Path("/var/lib/openlaps")
BUDGET_FILE = "can-watchdog-reboots.json"
SYSRQ_TRIGGER = Path("/proc/sysrq-trigger")
ACTIONS = ("log", "reboot")
_OPEN_RETRY_S = 5.0


# -- frame counting --------------------------------------------------------------


class FrameCounter:
    """Counts frames arriving on one interface, on a socket of its own.

    socketCAN is broadcast, so this never takes a frame from the agent. The
    socket is opened with FD frames enabled: the Luckfox's controller runs
    in FD mode (see its hardware.yaml), and a frame we would not be handed is
    a frame we cannot count. A read error closes the socket and the next
    poll reopens it; the detector is reset so the gap is not read as a stall.
    """

    def __init__(self, interface: str, bus_factory: Callable[[str], object]) -> None:
        self.interface = interface
        self._bus_factory = bus_factory
        self._count = 0
        self._generation = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def count(self) -> int:
        """Frames received so far."""
        with self._lock:
            return self._count

    @property
    def generation(self) -> int:
        """Incremented every time the socket is (re)opened."""
        with self._lock:
            return self._generation

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"count-{self.interface}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                bus = self._bus_factory(self.interface)
            except Exception as exc:  # python-can raises its own and OSError
                logger.warning("%s: cannot open: %s", self.interface, exc)
                self._stop.wait(_OPEN_RETRY_S)
                continue
            with self._lock:
                self._generation += 1
            try:
                while not self._stop.is_set():
                    message = bus.recv(timeout=0.5)
                    if message is not None:
                        with self._lock:
                            self._count += 1
            except Exception as exc:
                logger.warning("%s: read failed: %s", self.interface, exc)
            finally:
                try:
                    bus.shutdown()
                except Exception:  # pragma: no cover - best effort
                    pass
            self._stop.wait(_OPEN_RETRY_S)


def _default_bus_factory(interface: str):
    import can

    return can.Bus(channel=interface, interface="socketcan", fd=True)


# -- reboot budget ---------------------------------------------------------------


@dataclass
class RebootBudget:
    """At most `max_reboots` in any `window_s`, remembered in `path`."""

    path: Path
    max_reboots: int = DEFAULT_MAX_REBOOTS
    window_s: float = DEFAULT_WINDOW_S
    _reboots: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._reboots = [float(t) for t in data.get("reboots", [])]
        except (OSError, ValueError, AttributeError):
            self._reboots = []

    def recent(self, now: float) -> list[float]:
        """Reboot times still inside the window."""
        return [t for t in self._reboots if now - t < self.window_s]

    def allows(self, now: float) -> bool:
        return len(self.recent(now)) < self.max_reboots

    def record(self, now: float) -> None:
        self._reboots = self.recent(now) + [now]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"reboots": self._reboots}), encoding="utf-8")
        except OSError as exc:
            logger.error("cannot record reboot in %s: %s", self.path, exc)


# -- the watchdog ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WatchedBus:
    name: str
    interface: str


def _systemctl() -> str:
    return shutil.which("systemctl") or "/bin/systemctl"


def _ip() -> str:
    for candidate in ("/sbin/ip", "/usr/sbin/ip", "ip"):
        resolved = shutil.which(candidate)
        if resolved is not None:
            return resolved
    return "ip"


def describe_link(interface: str) -> str:
    """`ip -details -statistics link show`, for the journal; never blocks long.

    A wedged driver can hold the rtnl lock, in which case even `show` hangs:
    five seconds, then give up -- the reboot matters more than the evidence.
    """
    try:
        result = subprocess.run(
            [_ip(), "-details", "-statistics", "link", "show", interface],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"(ip link show {interface} failed: {exc})"
    return (result.stdout or result.stderr).strip()


def reboot_host(timeout_s: float = DEFAULT_REBOOT_TIMEOUT_S) -> None:
    """Ask systemd for a clean reboot; force one through sysrq if it stalls.

    `systemctl reboot` lets the agent drain and the filesystems sync. If the
    wedged driver stops shutdown from completing, `/proc/sysrq-trigger` works
    regardless of the `kernel.sysrq` mask for root: `s` syncs, `b` reboots.
    """
    try:
        subprocess.run([_systemctl(), "reboot"], check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("systemctl reboot failed: %s", exc)
    time.sleep(timeout_s)
    logger.error("still running %.0f s after systemctl reboot; forcing via sysrq", timeout_s)
    try:
        SYSRQ_TRIGGER.write_text("s")
        time.sleep(2)
        SYSRQ_TRIGGER.write_text("b")
    except OSError as exc:
        logger.error("sysrq reboot failed: %s", exc)


class Watchdog:
    """Polls every watched bus and acts on a persistent stall."""

    def __init__(
        self,
        buses: list[WatchedBus],
        *,
        action: str,
        stall_s: float = DEFAULT_STALL_S,
        budget: RebootBudget,
        delivered: dict[str, Callable[[], int]],
        generation: dict[str, Callable[[], int]] | None = None,
        read_rx: Callable[[str], int | None] = read_rx_packets,
        describe: Callable[[str], str] = describe_link,
        reboot: Callable[[], None] = reboot_host,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        if action not in ACTIONS:
            raise ValueError(f"action must be one of {ACTIONS}, not {action!r}")
        self.buses = buses
        self.action = action
        self.stall_s = stall_s
        self.budget = budget
        self._delivered = delivered
        self._generation = generation or {}
        self._read_rx = read_rx
        self._describe = describe
        self._reboot = reboot
        self._clock = clock
        self._wall = wall
        self._detectors = {bus.name: StallDetector() for bus in buses}
        self._seen_generation: dict[str, int] = {}
        self._announced: set[str] = set()
        self.rebooting = False

    def poll(self) -> dict[str, float]:
        """One observation of every bus; returns each bus's stall seconds."""
        now = self._clock()
        stalls: dict[str, float] = {}
        for bus in self.buses:
            detector = self._detectors[bus.name]
            generation = self._generation.get(bus.name)
            if generation is not None:
                current = generation()
                if current != self._seen_generation.get(bus.name):
                    # Fresh socket: whatever it missed while closed was not
                    # the controller's fault.
                    detector.reset()
                    self._seen_generation[bus.name] = current
            stall_s = detector.observe(
                self._read_rx(bus.interface), self._delivered[bus.name](), now
            )
            stalls[bus.name] = stall_s
            self._report(bus, stall_s)
        return stalls

    def _report(self, bus: WatchedBus, stall_s: float) -> None:
        if stall_s <= 0.0:
            if bus.name in self._announced:
                logger.info("%s (%s): frames delivered again, stall over", bus.name, bus.interface)
                self._announced.discard(bus.name)
            return
        if stall_s < self.stall_s:
            return
        if bus.name not in self._announced:
            self._announced.add(bus.name)
            logger.error(
                "%s (%s): STALLED for %.0f s -- the kernel is counting frames that no socket "
                "receives; the controller is wedged in a bus-error state. A reboot resets the "
                "controller; if the stall is back within a minute of booting, the bus itself is "
                "still disturbed (reset the ECU, check the wiring)\n%s",
                bus.name,
                bus.interface,
                stall_s,
                self._describe(bus.interface),
            )
        self._act(bus, stall_s)

    def _act(self, bus: WatchedBus, stall_s: float) -> None:
        if self.action != "reboot" or self.rebooting:
            return
        now = self._wall()
        if not self.budget.allows(now):
            logger.error(
                "%s: not rebooting: %d reboot(s) already in the last %.0f s "
                "(budget %d); fix the bus, or clear %s",
                bus.name,
                len(self.budget.recent(now)),
                self.budget.window_s,
                self.budget.max_reboots,
                self.budget.path,
            )
            return
        self.rebooting = True
        self.budget.record(now)
        logger.error(
            "%s: rebooting the vehicle box (stalled %.0f s, reboot %d of %d this window)",
            bus.name,
            stall_s,
            len(self.budget.recent(now)),
            self.budget.max_reboots,
        )
        self._reboot()

    def run(self, interval_s: float, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        logger.info(
            "watching %s every %.1f s; stall after %.0f s; action %s (%d reboot(s) per %.0f s)",
            ", ".join(f"{b.name} on {b.interface}" for b in self.buses),
            interval_s,
            self.stall_s,
            self.action,
            self.budget.max_reboots,
            self.budget.window_s,
        )
        while not stop.wait(interval_s):
            try:
                self.poll()
            except Exception:
                logger.exception("poll failed")


# -- entry point -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openlaps-can-watchdog", description=__doc__)
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
        "--action",
        choices=ACTIONS,
        default=os.environ.get("OPENLAPS_CAN_WATCHDOG_ACTION") or "log",
        help="what a stall triggers (default: $OPENLAPS_CAN_WATCHDOG_ACTION or log)",
    )
    parser.add_argument(
        "--stall-s",
        type=float,
        default=DEFAULT_STALL_S,
        help=(
            "seconds of frames counted but not delivered before acting "
            f"(default {DEFAULT_STALL_S:g})"
        ),
    )
    parser.add_argument(
        "--interval-s", type=float, default=DEFAULT_INTERVAL_S, help="poll interval (default 1)"
    )
    parser.add_argument(
        "--max-reboots",
        type=int,
        default=DEFAULT_MAX_REBOOTS,
        help=f"reboots allowed per window (default {DEFAULT_MAX_REBOOTS})",
    )
    parser.add_argument(
        "--window-s",
        type=float,
        default=DEFAULT_WINDOW_S,
        help=f"the reboot budget's window in seconds (default {DEFAULT_WINDOW_S:g})",
    )
    parser.add_argument(
        "--reboot-timeout-s",
        type=float,
        default=DEFAULT_REBOOT_TIMEOUT_S,
        help="seconds to give `systemctl reboot` before forcing one through sysrq",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("OPENLAPS_STATE_DIR") or DEFAULT_STATE_DIR),
        help="where the reboot budget is remembered (default: $OPENLAPS_STATE_DIR)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the buses that would be watched and the action, then exit",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)

    if not args.profile:
        print(
            "openlaps-can-watchdog: no profile: pass --profile or set OPENLAPS_PROFILE",
            file=sys.stderr,
        )
        return 2
    try:
        profile = load_profile(args.profile, args.hardware)
    except (ConfigError, ValueError) as exc:
        print(f"openlaps-can-watchdog: {exc}", file=sys.stderr)
        return 2
    if args.stall_s <= 0 or args.interval_s <= 0:
        print("openlaps-can-watchdog: --stall-s and --interval-s must be positive", file=sys.stderr)
        return 2

    buses = [WatchedBus(bus.name, bus.interface) for bus in profile.vehicle.buses]
    if not buses:
        print("openlaps-can-watchdog: the profile names no CAN buses", file=sys.stderr)
        return 2
    budget = RebootBudget(args.state_dir / BUDGET_FILE, args.max_reboots, args.window_s)

    if args.dry_run:
        for bus in buses:
            print(f"watch {bus.name} on {bus.interface}")
        print(
            f"stall after {args.stall_s:g} s -> {args.action}"
            f" (budget {args.max_reboots} per {args.window_s:g} s, recorded in {budget.path})"
        )
        return 0

    counters = {bus.name: FrameCounter(bus.interface, _default_bus_factory) for bus in buses}
    watchdog = Watchdog(
        buses,
        action=args.action,
        stall_s=args.stall_s,
        budget=budget,
        delivered={name: (lambda c=c: c.count) for name, c in counters.items()},
        generation={name: (lambda c=c: c.generation) for name, c in counters.items()},
        reboot=lambda: reboot_host(args.reboot_timeout_s),
    )
    for counter in counters.values():
        counter.start()
    try:
        watchdog.run(args.interval_s)
    except KeyboardInterrupt:
        pass
    finally:
        for counter in counters.values():
            counter.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
