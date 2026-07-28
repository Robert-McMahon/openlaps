"""Entry point: ``python -m pit.ntrip_client`` or ``openlaps-ntrip-client``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from pit.ntrip_client.service import NtripSettings, run_service


def main(argv: list[str] | None = None) -> int:
    """Run the ntrip-client until SIGTERM/SIGINT."""
    parser = argparse.ArgumentParser(prog="openlaps-ntrip-client", description=__doc__)
    parser.add_argument("--log-level", default="INFO", help="python logging level name")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        settings = NtripSettings.from_env()
    except ValueError as exc:
        # Config errors are the one thing that should stop the service —
        # loudly and precisely, the same discipline as the agent.
        print(f"openlaps-ntrip-client: {exc}", file=sys.stderr)
        return 2

    return asyncio.run(_run(settings))


async def _run(settings: NtripSettings) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, _request_stop, stop, signum)
    await run_service(settings, stop)
    return 0


def _request_stop(stop: asyncio.Event, signum: int) -> None:
    logging.getLogger("pit.ntrip_client").info("received signal %d, shutting down", signum)
    stop.set()


if __name__ == "__main__":
    sys.exit(main())
