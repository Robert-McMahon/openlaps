"""Entry point: ``python -m agent [profile-dir]`` or ``openlaps-agent``."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from agent.agent import AgentSettings, VehicleAgent
from core.config import ConfigError


def main(argv: list[str] | None = None) -> int:
    """Run the vehicle agent until SIGTERM/SIGINT."""
    parser = argparse.ArgumentParser(prog="openlaps-agent", description=__doc__)
    parser.add_argument(
        "profile",
        nargs="?",
        default=None,
        help="profile directory (default: $OPENLAPS_PROFILE)",
    )
    parser.add_argument(
        "--hardware",
        default=None,
        help="host-wiring overlay for this board (default: $OPENLAPS_HARDWARE)",
    )
    parser.add_argument("--log-level", default="INFO", help="python logging level name")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        settings = AgentSettings.from_env(profile_dir=args.profile, hardware_path=args.hardware)
        agent = VehicleAgent(settings)
    except (ConfigError, ValueError) as exc:
        # Config errors are the one thing that should stop the agent —
        # loudly and precisely (docs/AGENT_DESIGN.md -> Failure modes).
        print(f"openlaps-agent: {exc}", file=sys.stderr)
        return 2

    stop_event = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        logging.getLogger("agent").info("received signal %d, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    agent.run_forever(stop_event)
    return 0


if __name__ == "__main__":
    sys.exit(main())
