"""Entry point for the pit notifier."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from pit.notifier.config import ConfigError, NotifierSettings
from pit.notifier.service import NotifierService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openlaps-notifier", description=__doc__)
    parser.add_argument("--config", default=None, help="notifier YAML")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        service = NotifierService(NotifierSettings.from_env(config_path=args.config))
    except (ConfigError, OSError, ValueError) as exc:
        print(f"openlaps-notifier: {exc}", file=sys.stderr)
        return 2
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    service.run(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
