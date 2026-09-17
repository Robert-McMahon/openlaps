"""One-second sample-and-hold envelopes, independent of transport and storage.

Uniform capture-time ticks give RBE samples time weight, not arrival-count
weight. Stale data, missing bins and closed gates have no opinion and never
clear an existing finding. Only a valid healthy score can do that.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median
from uuid import uuid4

from pit.watch.config import (
    CounterConfig,
    DriftConfig,
    EnvelopeConfig,
    RatioConfig,
    WatchConfig,
    WholeCarConfig,
    channels_of,
)


@dataclass
class Reading:
    value: float
    time: float
    unit: str


class Envelope:
    def __init__(self, config: EnvelopeConfig) -> None:
        self.config = config
        self.learned = 0
        self.frozen = False
        self.bins: dict[str, list[float]] = {}
        self.table: dict[str, dict] = {}
        self.units: dict[str, str] = {}
        self.score = 0.0
        self.finding: dict | None = None
        self.source_session: str | None = None

    @property
    def channels(self) -> list[str]:
        return self.config.channels

    def snapshot(self) -> dict:
        return {
            "version": 1,
            "config": self.config.model_dump(),
            "learned": self.learned,
            "frozen": self.frozen,
            "bins": self.bins,
            "table": self.table,
            "units": self.units,
            "score": self.score,
            "finding": self.finding,
            "source_session": self.source_session,
        }

    def restore(self, model: dict) -> None:
        if model.get("version") != 1 or model["config"] != self.config.model_dump():
            raise ValueError("baseline configuration changed; fit a matching baseline")
        self.source_session = model.get("source_session")
        for key in ("learned", "frozen", "bins", "table", "units", "score", "finding"):
            setattr(self, key, model[key])

    def freeze(self) -> None:
        for key, values in self.bins.items():
            center = median(values)
            self.table[key] = {
                "median": center,
                "mad": median(abs(v - center) for v in values),
                "count": len(values),
            }
        self.bins = {}
        self.frozen = True

    def evaluate(self, at: float, readings: dict[str, Reading], valid: bool) -> dict:
        cfg = self.config
        result = dict(
            time=at,
            score=None,
            residual=None,
            expected=None,
            observed=None,
            baseline_status="gated",
            finding=None,
        )
        if not valid:
            return result
        needed = [cfg.target, *(c.channel for c in cfg.conditioned_on)]
        units = {c: readings[c].unit for c in needed}
        if self.units and units != self.units:
            result["baseline_status"] = "unit_mismatch"
            return result
        self.units = units
        observed = readings[cfg.target].value
        indices = [
            int(readings[c.channel].value > c.active_above)
            if c.active_above is not None
            else math.floor(readings[c.channel].value / c.bins)
            for c in cfg.conditioned_on
        ]
        key = ",".join(map(str, indices))
        result["observed"] = observed
        if not self.frozen:
            self.bins.setdefault(key, []).append(observed)
            self.learned += 1
            if self.learned >= cfg.baseline_seconds:
                self.freeze()
            result["baseline_status"] = "learning"
            return result
        stats = self.table.get(key)
        if stats is None or stats["count"] < cfg.min_bin_samples:
            result["baseline_status"] = "insufficient_bin"
            return result
        expected = stats["median"]
        scale = max(cfg.min_scale, 1.4826 * stats["mad"])
        residual = (observed - expected) / scale
        outside = float(abs(residual) > cfg.residual_sigma)
        self.score += -math.expm1(-1 / cfg.score_window) * (outside - self.score)
        summary = {
            "target": cfg.target,
            "expected": expected,
            "observed": observed,
            "unit": units[cfg.target],
            "baseline": cfg.baseline,
            "baseline_session": self.source_session,
            "baseline_samples": stats["count"],
            "median": expected,
            "mad": stats["mad"],
            "residual": residual,
            "bins": [
                dict(
                    channel=c.channel,
                    lower=i * c.bins if c.active_above is None else None,
                    upper=(i + 1) * c.bins if c.active_above is None else None,
                    bin_index=i,
                    observed=readings[c.channel].value,
                    unit=units[c.channel],
                    active_above=c.active_above,
                    on=bool(i) if c.active_above is not None else None,
                )
                for c, i in zip(cfg.conditioned_on, indices, strict=True)
            ],
        }
        if self.score > cfg.open_finding_above:
            if self.finding is None:
                self.finding = dict(
                    finding_id=str(uuid4()),
                    opened_at=at,
                    closed_at=None,
                    severity=cfg.severity,
                    peak_score=self.score,
                    summary=summary,
                )
            if self.score >= self.finding["peak_score"]:
                self.finding.update(peak_score=self.score, summary=summary)
            result["finding"] = dict(self.finding)
        elif self.finding is not None:
            result["finding"] = dict(self.finding, closed_at=at)
            self.finding = None
        result.update(
            score=self.score, residual=residual, expected=expected, baseline_status="ready"
        )
        return result


def build_monitor(config):
    """The monitor class for a configuration, by its ``kind``."""
    from pit.watch.monitors import Counter, Drift, Ratio, WholeCar

    if isinstance(config, EnvelopeConfig):
        return Envelope(config)
    if isinstance(config, DriftConfig):
        return Drift(config)
    if isinstance(config, RatioConfig):
        return Ratio(config)
    if isinstance(config, CounterConfig):
        return Counter(config)
    if isinstance(config, WholeCarConfig):
        return WholeCar(config)
    raise TypeError(f"no monitor for {type(config).__name__}")


class WatchEngine:
    def __init__(self, config: WatchConfig) -> None:
        self.config = config
        self.monitors = {name: build_monitor(cfg) for name, cfg in config.monitors.items()}
        self.readings: dict[str, Reading] = {}
        self.pit_status: str | None = None
        self.pit_time = float("-inf")
        self.last_tick: int | None = None

    @property
    def input_channels(self) -> set[str]:
        return {"car.rpm", "lap.event"} | {
            channel for cfg in self.config.monitors.values() for channel in channels_of(cfg)
        }

    def observe(self, channel: str, value: object, at: float, unit: str = "") -> None:
        if channel not in self.input_channels or not math.isfinite(at):
            return
        # Never use a late/backfilled sample to rewrite a tick already evaluated.
        if self.last_tick is not None and at <= self.last_tick:
            return
        if channel == "lap.event":
            import json

            event = json.loads(value) if isinstance(value, str) else value
            if isinstance(event, dict) and at >= self.pit_time:
                self.pit_status = event.get("pit_status")
                self.pit_time = at
                self._lap_event(event, at)
            return
        previous = self.readings.get(channel)
        if (
            isinstance(value, (int, float))
            and math.isfinite(value)
            and (previous is None or at >= previous.time)
        ):
            self.readings[channel] = Reading(float(value), at, unit)

    def _lap_event(self, event: dict, at: float) -> None:
        """Tell the per-lap kinds about lap boundaries and pit visits."""
        kind = event.get("type")
        if kind == "lap_completed":
            for monitor in self.monitors.values():
                if hasattr(monitor, "on_lap"):
                    monitor.on_lap(event, at)
        elif kind in ("pit_entry", "pit_exit") or event.get("pit_status") == "pit":
            for monitor in self.monitors.values():
                if hasattr(monitor, "on_pit"):
                    monitor.on_pit()

    def tick(self, at: int, active: bool = True) -> dict[str, dict]:
        if self.last_tick is not None and at <= self.last_tick:
            return {}
        # Do not fill missing transport time with a single newly arrived value.
        self.last_tick = at
        rows = {}
        for name, monitor in self.monitors.items():
            needed = ["car.rpm", *monitor.channels]
            fresh = all(
                c in self.readings
                and 0 <= at - self.readings[c].time <= self.config.max_age_seconds
                for c in needed
            )
            valid = (
                active
                and fresh
                and self.pit_status == "track"
                and self.readings["car.rpm"].value > self.config.baseline_rpm_min
            )
            rows[name] = monitor.evaluate(at, self.readings, valid)
            if not fresh:
                rows[name]["baseline_status"] = "missing_or_stale"
            elif not active:
                rows[name]["baseline_status"] = "no_session"
        return rows
