"""Entry point: ``python -m pit.session_control`` or ``openlaps-session-control``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from pit.session_control.service import SessionControlSettings, run_service


def main(argv: list[str] | None = None) -> int:
    """Run session-control until SIGTERM/SIGINT."""
    parser = argparse.ArgumentParser(prog="openlaps-session-control", description=__doc__)
    parser.add_argument("--log-level", default="INFO", help="python logging level name")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = SessionControlSettings.from_env()
    except (OSError, ValueError) as exc:
        print(f"openlaps-session-control: {exc}", file=sys.stderr)
        return 2
    return asyncio.run(_run(settings))


async def _run(settings: SessionControlSettings) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, _request_stop, stop, signum)
    await run_service(settings, stop)
    return 0


def _request_stop(stop: asyncio.Event, signum: int) -> None:
    logging.getLogger("pit.session_control").info("received signal %d, shutting down", signum)
    stop.set()


if __name__ == "__main__":
    sys.exit(main())
