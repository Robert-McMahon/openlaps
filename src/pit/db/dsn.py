"""Where every pit service gets its TimescaleDB connection string.

Deploy-time wiring only, per `docs/AGENT_DESIGN.md` -> Configuration
surface: connection details come from the environment, nothing about the
schema or the data model does. Every variable read here is documented in
`example.env`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from psycopg import conninfo

DEFAULT_PORT = "5432"


def dsn_from_env(env: Mapping[str, str] | None = None) -> str:
    """Build a libpq connection string from ``TIMESCALE_*``.

    ``TIMESCALE_DSN`` wins outright when set (it is the escape hatch for
    anything the discrete variables can't express: TLS modes, connection
    timeouts, a unix socket). Otherwise host/db/user are required and the
    rest is optional.
    """
    env = os.environ if env is None else env
    dsn = env.get("TIMESCALE_DSN", "").strip()
    if dsn:
        return dsn
    parts = {
        "host": env.get("TIMESCALE_HOST", "").strip(),
        "port": env.get("TIMESCALE_PORT", "").strip() or DEFAULT_PORT,
        "dbname": env.get("TIMESCALE_DB", "").strip(),
        "user": env.get("TIMESCALE_USER", "").strip(),
        "password": env.get("TIMESCALE_PASSWORD", ""),
    }
    required = (("TIMESCALE_HOST", "host"), ("TIMESCALE_DB", "dbname"), ("TIMESCALE_USER", "user"))
    missing = [name for name, key in required if not parts[key]]
    if missing:
        raise ValueError(
            f"TimescaleDB is not configured: set TIMESCALE_DSN, or all of {', '.join(missing)} "
            "(see example.env)"
        )
    if not parts["password"]:
        del parts["password"]
    return conninfo.make_conninfo(**parts)


def redacted(dsn: str) -> str:
    """``dsn`` with the password removed, safe to log."""
    try:
        parts = dict(conninfo.conninfo_to_dict(dsn))
    except Exception:
        # An unparseable DSN is the caller's problem to report; never risk
        # echoing a password while doing so.
        return "<unparseable dsn>"
    if parts.pop("password", None) is not None:
        parts["password"] = "***"
    return conninfo.make_conninfo(**parts)
