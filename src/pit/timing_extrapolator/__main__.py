"""Entry point for the pit timing extrapolator."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from pit.timing_extrapolator.config import ConfigError
from pit.timing_extrapolator.service import TimingService, TimingServiceSettings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openlaps-timing-extrapolator", description=__doc__)
    parser.add_argument("--config", default=None, help="timing extrapolator YAML")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        service = TimingService(TimingServiceSettings.from_env(config_path=args.config))
    except (ConfigError, OSError, ValueError) as exc:
        print(f"openlaps-timing-extrapolator: {exc}", file=sys.stderr)
        return 2
    return asyncio.run(_run(service))


async def _run(service: TimingService) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    await service.run(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
