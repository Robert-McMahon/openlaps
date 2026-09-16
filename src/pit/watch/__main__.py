"""Run the pit envelope watcher."""

import argparse
import asyncio
import logging
import signal

from pit.watch.service import Settings, WatchService


async def run(settings: Settings) -> None:
    service = WatchService(settings)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await service.run(stop)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=str, help="profile watch.yaml")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper())
    settings = Settings.from_env()
    if args.config:
        from pathlib import Path

        settings.config = Path(args.config)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
