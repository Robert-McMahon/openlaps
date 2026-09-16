"""Entry point: ``python -m pit.pit_monitor`` or ``openlaps-pit-monitor``."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from pit.pit_monitor.service import PitMonitorSettings, run_service


def main(argv: list[str] | None = None) -> int:
    """Run the pit-monitor until SIGTERM/SIGINT."""
    parser = argparse.ArgumentParser(prog="openlaps-pit-monitor", description=__doc__)
    parser.add_argument("--log-level", default="INFO", help="python logging level name")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        settings = PitMonitorSettings.from_env()
    except ValueError as exc:
        # Config errors are the one thing that should stop the service —
        # loudly and precisely, the same discipline as the agent.
        print(f"openlaps-pit-monitor: {exc}", file=sys.stderr)
        return 2

    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda number, _frame: _request_stop(stop, number))
    run_service(settings, stop)
    return 0


def _request_stop(stop: threading.Event, signum: int) -> None:
    logging.getLogger("pit.pit_monitor").info("received signal %d, shutting down", signum)
    stop.set()


if __name__ == "__main__":
    sys.exit(main())
