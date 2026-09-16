"""Entry point: ``python -m pit.timing_feed`` or ``openlaps-timing-feed``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from pit.timing_feed.service import TimingFeedSettings, run_service


def main(argv: list[str] | None = None) -> int:
    """Run the timing feed service until SIGTERM/SIGINT."""
    parser = argparse.ArgumentParser(prog="openlaps-timing-feed", description=__doc__)
    parser.add_argument("--log-level", default="INFO", help="python logging level name")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        settings = TimingFeedSettings.from_env()
    except ValueError as exc:
        print(f"openlaps-timing-feed: {exc}", file=sys.stderr)
        return 2

    asyncio.run(_run(settings))
    return 0


async def _run(settings: TimingFeedSettings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, _request_stop, stop, signum)
    await run_service(settings, stop)


def _request_stop(stop: asyncio.Event, signum: int) -> None:
    logging.getLogger("pit.timing_feed").info("received signal %d, shutting down", signum)
    stop.set()


if __name__ == "__main__":
    sys.exit(main())
