#!/usr/bin/env python3
"""Rebuild a stretch of `car.brake_pedal_switch` history from the PDM input voltage.

Until the catalog read the brake switch from the PDM's 100 Hz input state,
`car.brake_pedal_switch` was the ECU's copy (MISC5, about 3.6 Hz), which
trails the pedal by 40 to 270 ms. The PDM pin's *voltage* was recorded all
along as `car.brake_switch_input`, report-by-exception, so every edge is in
the database with its own timestamp. This tool turns that into the switch:

1. moves the ECU rows in the window to `car.brake_pedal_switch_ecu`, the name
   the catalog now gives that source (nothing is deleted);
2. writes `car.brake_pedal_switch` rows derived from the voltage: one per
   stored voltage sample, plus a 200 ms fill while the level holds, which is
   the heartbeat the live channel has, so bucket averages stay comparable;
3. refreshes the 1 s and 1 min aggregates over the window.

Measured against two raw candump captures from the same race (106 edges):
the derived edge is on the PDM's own edge for about nine in ten, and up to
110 ms late for the rest, where a voltage heartbeat happened to fall inside
the 100 ms `min_interval` just before the pedal moved.

It refuses to run twice over the same window, and `--dry-run` rolls back
after printing what it would have done.

    set -a; . deploy/.env; set +a
    TIMESCALE_HOST=127.0.0.1 uv run python tools/backfill_brake_switch.py \\
        --vehicle christine --session <session_id> --dry-run
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg  # noqa: E402

from pit.db.dsn import dsn_from_env  # noqa: E402

BOOL_VALUE_TYPE = 3
# A voltage sample further than this from the next one is the edge of a gap in
# the record (the heartbeat is 5 s), so the level is not carried across it.
MAX_HOLD = "6 seconds"
FILL = "200 milliseconds"


def _channel_key(conn: psycopg.Connection, vehicle: str, name: str) -> int | None:
    row = conn.execute(
        "SELECT channel_key FROM channels WHERE vehicle_id = %s AND name = %s", (vehicle, name)
    ).fetchone()
    return None if row is None else int(row[0])


def _window(conn: psycopg.Connection, args: argparse.Namespace) -> tuple[datetime, datetime]:
    if args.session:
        row = conn.execute(
            "SELECT started, ended FROM sessions WHERE session_id = %s AND vehicle_id = %s",
            (args.session, args.vehicle),
        ).fetchone()
        if row is None or row[1] is None:
            raise SystemExit(f"session {args.session!r} not found for {args.vehicle}, or not ended")
        return row[0], row[1]
    if not (args.start and args.end):
        raise SystemExit("give --session, or both --start and --end")
    return datetime.fromisoformat(args.start), datetime.fromisoformat(args.end)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--vehicle", required=True)
    parser.add_argument("--session", help="session_id whose start..end is the window")
    parser.add_argument("--start", help="ISO time with offset, inclusive")
    parser.add_argument("--end", help="ISO time with offset, exclusive")
    parser.add_argument("--channel", default="car.brake_pedal_switch")
    parser.add_argument("--ecu-channel", default="car.brake_pedal_switch_ecu")
    parser.add_argument("--voltage-channel", default="car.brake_switch_input")
    parser.add_argument(
        "--threshold", type=float, default=4.5, help="volts above which the pedal is applied"
    )
    parser.add_argument("--dry-run", action="store_true", help="report, then roll back")
    args = parser.parse_args(argv)

    with psycopg.connect(dsn_from_env()) as conn:
        start, end = _window(conn, args)
        switch = _channel_key(conn, args.vehicle, args.channel)
        voltage = _channel_key(conn, args.vehicle, args.voltage_channel)
        if switch is None or voltage is None:
            raise SystemExit(f"{args.vehicle} has no {args.channel} or {args.voltage_channel}")

        conn.execute(
            "INSERT INTO channels (vehicle_id, name, units, value_type) VALUES (%s, %s, '', %s) "
            "ON CONFLICT (vehicle_id, name) DO NOTHING",
            (args.vehicle, args.ecu_channel, BOOL_VALUE_TYPE),
        )
        ecu = _channel_key(conn, args.vehicle, args.ecu_channel)
        window = {"start": start, "end": end}
        in_window = "time >= %(start)s AND time < %(end)s"

        already = conn.execute(
            f"SELECT count(*) FROM samples WHERE channel_key = %(key)s AND {in_window}",
            {"key": ecu, **window},
        ).fetchone()[0]
        if already:
            raise SystemExit(
                f"{args.ecu_channel} already has {already} rows in this window: "
                "it has been backfilled (or the car already sends that channel)"
            )
        sources = conn.execute(
            f"SELECT count(*) FROM samples WHERE channel_key = %(key)s AND {in_window}",
            {"key": voltage, **window},
        ).fetchone()[0]
        if not sources:
            raise SystemExit(f"no {args.voltage_channel} samples in the window; nothing to derive")

        moved = conn.execute(
            f"UPDATE samples SET channel_key = %(ecu)s WHERE channel_key = %(key)s AND {in_window}",
            {"ecu": ecu, "key": switch, **window},
        ).rowcount
        written = conn.execute(
            f"""
            WITH v AS (
                SELECT time, (value > %(threshold)s)::int AS applied,
                       lead(time) OVER (ORDER BY time) AS next_time
                FROM samples
                WHERE channel_key = %(voltage)s AND {in_window}
            )
            INSERT INTO samples (time, channel_key, value)
            SELECT held, %(key)s, v.applied
            FROM v, LATERAL generate_series(
                v.time,
                CASE WHEN v.next_time - v.time <= %(max_hold)s::interval
                     THEN greatest(v.time, v.next_time - interval '50 milliseconds')
                     ELSE v.time END,
                %(fill)s::interval
            ) AS held
            """,
            {
                "threshold": args.threshold,
                "voltage": voltage,
                "key": switch,
                "max_hold": MAX_HOLD,
                "fill": FILL,
                **window,
            },
        ).rowcount
        edges = conn.execute(
            f"""
            SELECT count(*) FILTER (WHERE applied AND NOT was),
                   count(*) FILTER (WHERE was AND NOT applied)
            FROM (SELECT value > %(threshold)s AS applied,
                         lag(value > %(threshold)s) OVER (ORDER BY time) AS was
                  FROM samples WHERE channel_key = %(voltage)s AND {in_window}) e
            """,
            {"threshold": args.threshold, "voltage": voltage, **window},
        ).fetchone()

        print(f"window  {start.isoformat()} .. {end.isoformat()}")
        print(f"moved   {moved} ECU rows from {args.channel} to {args.ecu_channel}")
        print(f"wrote   {written} {args.channel} rows from {sources} voltage samples")
        print(f"edges   {edges[0]} applications, {edges[1]} releases")
        if args.dry_run:
            conn.rollback()
            print("dry run: rolled back")
            return 0
        conn.commit()

    with psycopg.connect(dsn_from_env(), autocommit=True) as conn:
        for view in ("samples_1s", "samples_1m"):
            conn.execute("CALL refresh_continuous_aggregate(%s, %s, %s)", (view, start, end))
            print(f"refreshed {view}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
