"""Apply the pit database's SQL migrations, in filename order.

Plain `.sql` files plus this applier — no Alembic, no ORM. There is no ORM
in this codebase and the schema is small and mostly DDL, so the whole
mechanism is: run each unapplied file inside one transaction that also
records it in `schema_migrations`. A migration either lands completely or
not at all, and re-running is a no-op.

Postgres has transactional DDL, which is what makes this safe enough to be
this small.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from pathlib import Path

from psycopg import Connection

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Arbitrary but fixed: two appliers racing (compose bringing up two services
# that both migrate on boot) serialise here rather than both running DDL.
_ADVISORY_LOCK_KEY = 0x0_0DEC_A11

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

log = logging.getLogger(__name__)


def discover(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    """Every migration file, in the order they must be applied."""
    return sorted(directory.glob("*.sql"))


def applied_versions(conn: Connection) -> set[str]:
    """Versions already recorded in ``schema_migrations`` (empty if absent)."""
    with conn.transaction():
        conn.execute(_SCHEMA_MIGRATIONS_DDL)
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row[0] for row in rows}


def pending(conn: Connection, directory: Path = MIGRATIONS_DIR) -> list[Path]:
    """Migrations present on disk that this database has not applied."""
    done = applied_versions(conn)
    return [path for path in discover(directory) if path.name not in done]


def apply_migrations(conn: Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every pending migration; return the versions applied."""
    todo = pending(conn, directory)
    if not todo:
        return []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
    try:
        todo = pending(conn, directory)
        # Every dashboard-facing migration uses the deployment role name for
        # an explicit grant. Session settings do not survive between upgrade
        # runs, so configure them whenever any read-surface migration is
        # pending, not only when 002 creates the role for the first time.
        if any(path.name >= "002_" for path in todo):
            _configure_grafana_role(conn)
        return _apply_each(conn, todo)
    finally:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))


def _configure_grafana_role(conn: Connection) -> None:
    """Pass deployment-only role credentials to migration 002 without SQL interpolation."""
    user = os.environ.get("GRAFANA_DB_USER", "grafana_ro")
    password = os.environ.get("GRAFANA_DB_PASSWORD", "")
    if not user:
        raise ValueError("GRAFANA_DB_USER must not be empty while migration 002 is pending")
    if not password:
        raise ValueError("GRAFANA_DB_PASSWORD is required while migration 002 is pending")
    with conn.transaction():
        conn.execute("SELECT set_config('openlaps.grafana_db_user', %s, false)", (user,))
        conn.execute("SELECT set_config('openlaps.grafana_db_password', %s, false)", (password,))


def _apply_each(conn: Connection, todo: Iterable[Path]) -> list[str]:
    applied: list[str] = []
    for path in todo:
        log.info("applying migration %s", path.name)
        with conn.transaction():
            # No parameters, so psycopg sends this as one simple query and
            # the file may contain any number of statements.
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
        applied.append(path.name)
    return applied
