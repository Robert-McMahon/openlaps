"""The live numbers for the pit wall, under the pit-owned ``strategy.*`` namespace.

Same topic and payload shape as the live-decoder and the timing
extrapolator -- ``openlaps/<vehicle>/<channel>`` carrying ``{"time",
"value", "display"}`` -- so a Grafana MQTT panel reads them the way it
reads every other live value. Unlike those two, the messages are *retained*:
strategy changes once a lap, and a pit wall opened mid-stint should show the
last projection now rather than a blank until the next crossing. A value of
``null`` (no session, no plan, not enough laps) is published on purpose so
a stale number never survives the condition that produced it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime

from pit.strategy.model import StrategyState

OUTPUT_CHANNELS: tuple[str, ...] = (
    "strategy.fuel_remaining_l",
    "strategy.burn_l_per_lap",
    "strategy.laps_to_dry",
    "strategy.time_to_dry_s",
    "strategy.pit_window_open_lap",
    "strategy.pit_window_close_lap",
    "strategy.target_lap_s",
    "strategy.driver_time_remaining_s",
    "strategy.refuel_release_at",
)


def _clock(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    whole = int(round(seconds))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _lap_time(seconds: float | None) -> str:
    if seconds is None:
        return "--:--.-"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}:{rest:04.1f}"


def _number(value: float | None, digits: int = 1, suffix: str = "") -> str:
    return "--" if value is None else f"{value:.{digits}f}{suffix}"


def messages(state: StrategyState) -> Iterator[tuple[str, dict[str, object]]]:
    """Every ``strategy.*`` channel with its value and display text."""
    stamp = round(state.time.timestamp() * 1000)
    release = state.refuel_release_at
    values: list[tuple[str, float | None, str]] = [
        (
            "strategy.fuel_remaining_l",
            state.fuel_remaining_l,
            _number(state.fuel_remaining_l, 1, " L"),
        ),
        (
            "strategy.burn_l_per_lap",
            state.burn_l_per_lap,
            _number(state.burn_l_per_lap, 2, " L/lap"),
        ),
        ("strategy.laps_to_dry", state.laps_to_dry_lo, _number(state.laps_to_dry_lo, 1)),
        ("strategy.time_to_dry_s", state.time_to_dry_s_lo, _clock(state.time_to_dry_s_lo)),
        (
            "strategy.pit_window_open_lap",
            None if state.window_open_lap is None else float(state.window_open_lap),
            "--" if state.window_open_lap is None else str(state.window_open_lap),
        ),
        (
            "strategy.pit_window_close_lap",
            None if state.window_close_lap is None else float(state.window_close_lap),
            "--" if state.window_close_lap is None else str(state.window_close_lap),
        ),
        ("strategy.target_lap_s", state.target_lap_s, _lap_time(state.target_lap_s)),
        (
            "strategy.driver_time_remaining_s",
            state.driver_time_remaining_s,
            _clock(state.driver_time_remaining_s),
        ),
        (
            "strategy.refuel_release_at",
            None if release is None else release.timestamp() * 1000.0,
            "--:--:--" if release is None else _wall(release),
        ),
    ]
    for channel, value, display in values:
        yield channel, {"time": stamp, "value": value, "display": display}


def _wall(at: datetime) -> str:
    return at.astimezone().strftime("%H:%M:%S")


def mqtt_message(vehicle: str, channel: str, document: dict[str, object]) -> tuple[str, bytes]:
    """Topic and payload for one channel; refuses anything not pit-owned."""
    if channel not in OUTPUT_CHANNELS:
        raise ValueError(f"refusing to publish non-strategy channel {channel!r}")
    return (
        f"openlaps/{vehicle}/{channel}",
        json.dumps(document, separators=(",", ":"), allow_nan=False).encode(),
    )
