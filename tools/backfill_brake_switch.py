#!/usr/bin/env python3
"""Rebuild a stretch of `car.brake_pedal_switch` history from what was recorded.

Until the catalog read the brake switch from the PDM's 100 Hz input state,
`car.brake_pedal_switch` was the ECU's copy (MISC5, about 3.6 Hz), which
trails the pedal by 40 to 270 ms. The PDM pin's *voltage* was recorded all
along as `car.brake_switch_input`, report-by-exception: one sample per edge,
at the edge's own timestamp, and a 5 s heartbeat.

Neither record is enough alone. The voltage has the timing but says each
edge once, and the car was losing samples before they were stored (a steady
15 to 40 % of frames in the two candump captures from the BEERS 36 hour), so
about one edge in forty is simply absent: an application the ECU's repeating
copy shows and the voltage never does, or a release that never comes. So the
two are fused:

- every voltage sample sets the state at its own time;
- an ECU sample that disagrees with that state more than `ECU_LAG` after the
  last change means a voltage edge went missing, and sets the state at the
  ECU sample's time (late by up to one ECU period, as that channel always
  was, but not lost).

What the tool does:

1. moves the ECU rows in the window to `car.brake_pedal_switch_ecu`, the name
   the catalog now gives that source (nothing recorded is deleted);
2. writes the fused `car.brake_pedal_switch`, with a 200 ms fill while the
   level holds, which is the heartbeat the live channel has, so bucket
   averages stay comparable;
3. refreshes the 1 s and 1 min aggregates over the window.

It refuses a window it has already done unless given `--rebuild`, which
replaces the derived rows only. `--dry-run` rolls back after reporting.

    set -a; . deploy/.env; set +a
    TIMESCALE_HOST=127.0.0.1 uv run python tools/backfill_brake_switch.py \\
        --vehicle christine --session <session_id> --dry-run
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg  # noqa: E402

from pit.db.dsn import dsn_from_env  # noqa: E402

BOOL_VALUE_TYPE = 3
# The ECU's copy trails the pin by up to 270 ms in the captures. An ECU sample
# that still disagrees after this long is evidence, not lag.
ECU_LAG = timedelta(milliseconds=400)
# Two source samples further apart than this bracket a gap in the record (the
# voltage heartbeat is 5 s), so the level is not carried across it.
MAX_HOLD = timedelta(seconds=6)
FILL = timedelta(milliseconds=200)


@dataclass
class Fused:
    """The rebuilt switch and how much of it the ECU had to supply."""

    rows: list[tuple[datetime, int]]
    applications: int = 0
    releases: int = 0
    applications_from_ecu: int = 0
    releases_from_ecu: int = 0


def fuse(
    voltage: list[tuple[datetime, float]],
    ecu: list[tuple[datetime, float]],
    threshold: float,
) -> Fused:
    """Merge the pin voltage's edges with the ECU's repeating copy of the switch."""
    merged = sorted(
        [(time, 0, value > threshold) for time, value in voltage]
        + [(time, 1, value > 0.5) for time, value in ecu]
    )
    out = Fused(rows=[])
    state: bool | None = None
    changed_at: datetime | None = None
    previous: datetime | None = None
    for time, from_ecu, applied in merged:
        if state is None:
            if from_ecu:
                continue  # the voltage starts the record; the ECU only corrects it
            state, changed_at = applied, time
        elif applied != state:
            if from_ecu and time - changed_at <= ECU_LAG:
                applied = state  # the ECU has not caught up with the pin yet
            else:
                state, changed_at = applied, time
                if applied:
                    out.applications += 1
                    out.applications_from_ecu += from_ecu
                else:
                    out.releases += 1
                    out.releases_from_ecu += from_ecu
        if previous is not None and time - previous <= MAX_HOLD:
            held = previous + FILL
            while held < time - FILL / 4:
                out.rows.append((held, out.rows[-1][1]))
                held += FILL
        out.rows.append((time, int(state)))
        previous = time
    return out


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
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="replace the derived rows of a window that was already backfilled",
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

        def count(key: int) -> int:
            return conn.execute(
                f"SELECT count(*) FROM samples WHERE channel_key = %(key)s AND {in_window}",
                {"key": key, **window},
            ).fetchone()[0]

        def fetch(key: int) -> list[tuple[datetime, float]]:
            return conn.execute(
                f"SELECT time, value FROM samples WHERE channel_key = %(key)s AND {in_window} "
                "AND value IS NOT NULL ORDER BY time",
                {"key": key, **window},
            ).fetchall()

        print(f"window  {start.isoformat()} .. {end.isoformat()}")
        if count(ecu) and not args.rebuild:
            raise SystemExit(
                f"{args.ecu_channel} already has rows in this window: it has been backfilled "
                "(--rebuild replaces the derived rows), or the car already sends that channel"
            )
        if args.rebuild:
            removed = conn.execute(
                f"DELETE FROM samples WHERE channel_key = %(key)s AND {in_window}",
                {"key": switch, **window},
            ).rowcount
            print(f"removed {removed} previously derived {args.channel} rows")
        else:
            moved = conn.execute(
                f"UPDATE samples SET channel_key = %(ecu)s "
                f"WHERE channel_key = %(key)s AND {in_window}",
                {"ecu": ecu, "key": switch, **window},
            ).rowcount
            print(f"moved   {moved} ECU rows from {args.channel} to {args.ecu_channel}")

        pin = fetch(voltage)
        if not pin:
            raise SystemExit(f"no {args.voltage_channel} samples in the window; nothing to derive")
        fused = fuse(pin, fetch(ecu), args.threshold)
        with (
            conn.cursor() as cursor,
            cursor.copy("COPY samples (time, channel_key, value) FROM STDIN") as copy,
        ):
            for time, value in fused.rows:
                copy.write_row((time, switch, value))

        print(f"wrote   {len(fused.rows)} {args.channel} rows from {len(pin)} voltage samples")
        print(
            f"edges   {fused.applications} applications ({fused.applications_from_ecu} only the "
            f"ECU recorded), {fused.releases} releases ({fused.releases_from_ecu} only the ECU)"
        )
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
