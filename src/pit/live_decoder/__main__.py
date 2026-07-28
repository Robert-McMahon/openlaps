"""Entry point: ``python -m pit.live_decoder`` or ``openlaps-live-decoder``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from pit.live_decoder.config import ConfigError
from pit.live_decoder.service import LiveDecoder, LiveDecoderSettings


def main(argv: list[str] | None = None) -> int:
    """Run the live-decoder until SIGTERM/SIGINT, reloading on SIGHUP."""
    parser = argparse.ArgumentParser(prog="openlaps-live-decoder", description=__doc__)
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "live view YAML (default: $OPENLAPS_LIVE_CONFIG or deploy/pit-config/live-decoder.yaml)"
        ),
    )
    parser.add_argument("--log-level", default="INFO", help="python logging level name")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = LiveDecoderSettings.from_env(config_path=args.config)
        decoder = LiveDecoder(settings)
    except (ConfigError, OSError, ValueError) as exc:
        print(f"openlaps-live-decoder: {exc}", file=sys.stderr)
        return 2
    return asyncio.run(_run(decoder))


async def _run(decoder: LiveDecoder) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, _request_stop, stop, signum)
    loop.add_signal_handler(signal.SIGHUP, decoder.reloader.request_reload)
    await decoder.run(stop)
    return 0


def _request_stop(stop: asyncio.Event, signum: int) -> None:
    logging.getLogger("pit.live_decoder").info("received signal %d, shutting down", signum)
    stop.set()


if __name__ == "__main__":
    sys.exit(main())
