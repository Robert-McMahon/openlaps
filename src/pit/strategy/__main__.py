"""Entry point: ``python -m pit.strategy`` or ``openlaps-strategy``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from pit.strategy.service import StrategySettings, run_service


def main(argv: list[str] | None = None) -> int:
    """Run the strategy service until SIGTERM/SIGINT."""
    parser = argparse.ArgumentParser(prog="openlaps-strategy", description=__doc__)
    parser.add_argument("--log-level", default="INFO", help="python logging level name")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        settings = StrategySettings.from_env()
    except ValueError as exc:
        # Config errors are the one thing that should stop the service --
        # loudly and precisely, the same discipline as the agent.
        print(f"openlaps-strategy: {exc}", file=sys.stderr)
        return 2

    asyncio.run(_run(settings))
    return 0


async def _run(settings: StrategySettings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, _request_stop, stop, signum)
    await run_service(settings, stop)


def _request_stop(stop: asyncio.Event, signum: int) -> None:
    logging.getLogger("pit.strategy").info("received signal %d, shutting down", signum)
    stop.set()


if __name__ == "__main__":
    sys.exit(main())
