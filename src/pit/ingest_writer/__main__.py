"""Entry point: ``python -m pit.ingest_writer`` or ``openlaps-ingest-writer``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from pit.ingest_writer.writer import WriterSettings, run_writer


def main(argv: list[str] | None = None) -> int:
    """Run the ingest-writer until SIGTERM/SIGINT."""
    parser = argparse.ArgumentParser(prog="openlaps-ingest-writer", description=__doc__)
    parser.add_argument("--log-level", default="INFO", help="python logging level name")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        settings = WriterSettings.from_env()
    except ValueError as exc:
        # Config errors are the one thing that should stop the service —
        # loudly and precisely, the same discipline as the agent.
        print(f"openlaps-ingest-writer: {exc}", file=sys.stderr)
        return 2

    return asyncio.run(_run(settings))


async def _run(settings: WriterSettings) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, _request_stop, stop, signum)
    await run_writer(settings, stop)
    return 0


def _request_stop(stop: asyncio.Event, signum: int) -> None:
    logging.getLogger("pit.ingest_writer").info("received signal %d, shutting down", signum)
    stop.set()


if __name__ == "__main__":
    sys.exit(main())
