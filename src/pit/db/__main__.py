"""Entry point: ``python -m pit.db`` or ``openlaps-migrate``.

One-shot, not a service: it applies pending migrations and exits, so it
takes no signal handling beyond letting an interrupt abort the in-flight
transaction (Postgres rolls it back; the run is safe to repeat).
"""

from __future__ import annotations

import argparse
import logging
import sys

import psycopg

from pit.db.dsn import dsn_from_env, redacted
from pit.db.migrate import apply_migrations, pending


def main(argv: list[str] | None = None) -> int:
    """Apply pending migrations to the pit database."""
    parser = argparse.ArgumentParser(prog="openlaps-migrate", description=__doc__)
    parser.add_argument("--dsn", default=None, help="connection string (default: from TIMESCALE_*)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list pending migrations without applying them",
    )
    parser.add_argument("--log-level", default="INFO", help="python logging level name")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("pit.db")

    try:
        dsn = args.dsn or dsn_from_env()
    except ValueError as exc:
        print(f"openlaps-migrate: {exc}", file=sys.stderr)
        return 2

    log.info("connecting to %s", redacted(dsn))
    try:
        with psycopg.connect(dsn) as conn:
            if args.dry_run:
                todo = [path.name for path in pending(conn)]
                log.info("pending: %s", ", ".join(todo) if todo else "none")
                return 0
            applied = apply_migrations(conn)
            log.info("applied %d migration(s): %s", len(applied), ", ".join(applied) or "none")
    except psycopg.Error as exc:
        print(f"openlaps-migrate: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("openlaps-migrate: interrupted; the in-flight migration rolled back", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
