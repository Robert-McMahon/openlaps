#!/usr/bin/env python3
"""Fit frozen envelope models from a past session's archived samples.

Example: uv run tools/watch_fit.py --session <id> --output /tmp/watch-models
Configure stored_file and baseline: stored before fitting; output is one JSON
file per monitor, ready to review and copy into the profile.
"""

import argparse
import json
import math
from pathlib import Path

import psycopg

from pit.db.dsn import dsn_from_env
from pit.watch.config import load_config
from pit.watch.engine import WatchEngine


def fit(samples, engine):
    """Chronological (epoch seconds, channel, value, units), bounded 1 Hz fit."""
    tick = None
    for at, channel, value, unit in samples:
        if tick is None:
            tick = math.ceil(at)
        while tick < at:
            engine.tick(tick)
            tick += 1
        engine.observe(channel, value, at, unit)
        if all(m.frozen for m in engine.monitors.values()):
            break
    if tick is not None:
        engine.tick(tick)
    if not all(m.frozen for m in engine.monitors.values()):
        raise ValueError("session lacks enough clean on-track time for every monitor")
    for name, monitor in engine.monitors.items():
        # Envelope and ratio baselines are tables of bins or groups; a table
        # with no usable entry is a baseline that will never have an opinion.
        table = getattr(monitor, "table", None)
        minimum = getattr(monitor.config, "min_bin_samples", None) or getattr(
            monitor.config, "min_group_samples", 0
        )
        if table is not None and not any(b["count"] >= minimum for b in table.values()):
            raise ValueError(
                f"{name}: no bin has enough samples; extend the baseline or widen bins"
            )
    return {name: m.snapshot() for name, m in engine.monitors.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("profiles/example-club-racer/watch.yaml")
    )
    parser.add_argument("--session", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    engine = WatchEngine(load_config(args.config))
    with psycopg.connect(dsn_from_env()) as conn:
        session = conn.execute(
            "SELECT vehicle_id, started, ended FROM sessions WHERE session_id=%s", (args.session,)
        ).fetchone()
        if session is None or session[2] is None:
            raise ValueError("choose an ended session")
        with conn.cursor(name="watch_fit") as cur:
            cur.execute(
                "SELECT extract(epoch FROM time)::double precision, channel, "
                "value, value_text, units FROM v_samples_named WHERE vehicle_id=%s "
                "AND time BETWEEN %s AND %s AND channel=ANY(%s) ORDER BY time",
                (*session, sorted(engine.input_channels)),
            )
            models = fit(
                ((t, c, v if v is not None else text, u) for t, c, v, text, u in cur), engine
            )
    args.output.mkdir(parents=True, exist_ok=True)
    for name, model in models.items():
        # Fitted session_start configurations become stored baselines on export.
        model["source_session"] = args.session
        model["config"]["baseline"] = "stored"
        model["config"]["stored_file"] = f"{name}.json"
        model["score"], model["finding"] = 0, None
        (args.output / f"{name}.json").write_text(
            json.dumps(model, indent=2, allow_nan=False) + "\n"
        )


if __name__ == "__main__":
    main()
