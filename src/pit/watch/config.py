"""Profile configuration; bin widths and scales are always in catalog units.

Five monitor kinds share one file (``profiles/<car>/watch.yaml``) and one
interface. ``envelope`` (P7.5) compares a sample with its conditioning
bin; the four P7.6 kinds each catch a failure shape the envelope cannot:
``drift`` a slow per-lap decline, ``ratio`` a physical relationship that
should be a constant, ``counter`` a monotonic count that should not move,
and ``whole_car`` everything at once from a channel list and nothing else.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def _seconds(value: float | str) -> float | str:
    return float(value[:-1]) if isinstance(value, str) and value.endswith("s") else value


class Condition(StrictModel):
    channel: str
    bins: float = Field(gt=0)
    active_above: float | None = None


class When(StrictModel):
    """A gate on one channel: the monitor has an opinion only while it holds."""

    channel: str
    above: float | None = None
    below: float | None = None
    between: tuple[float, float] | None = None
    equals: float | None = None

    @model_validator(mode="after")
    def one_bound(self) -> When:
        bounds = [self.above, self.below, self.between, self.equals]
        if sum(bound is not None for bound in bounds) == 0:
            raise ValueError("a `when` needs above, below, between or equals")
        if self.between is not None and self.between[0] >= self.between[1]:
            raise ValueError("`between` needs a lower bound below its upper bound")
        return self

    def holds(self, value: float) -> bool:
        if self.above is not None and not value > self.above:
            return False
        if self.below is not None and not value < self.below:
            return False
        if self.between is not None and not self.between[0] <= value <= self.between[1]:
            return False
        if self.equals is not None and value != self.equals:
            return False
        return True


Severity = Literal["info", "warning", "critical"]
Gate = Literal["on_track"]


class EnvelopeConfig(StrictModel):
    kind: Literal["envelope"] = "envelope"
    target: str
    conditioned_on: list[Condition] = Field(min_length=1, max_length=4)
    baseline: Literal["session_start", "stored"] = "session_start"
    stored_file: str | None = None
    baseline_seconds: int = Field(default=180, ge=1, le=3600)
    min_bin_samples: int = Field(default=50, ge=2)
    residual_sigma: float = Field(default=3, gt=0)
    # Explicit physical-unit noise floor for constant/quantised bins (MAD=0).
    min_scale: float = Field(default=0.01, gt=0)
    score_window: float = Field(default=30, gt=0)
    open_finding_above: float = Field(default=0.8, gt=0, lt=1)
    severity: Severity = "warning"
    gate: Gate = "on_track"

    @field_validator("score_window", mode="before")
    @classmethod
    def seconds(cls, value: float | str) -> float | str:
        return _seconds(value)

    @property
    def channels(self) -> list[str]:
        return [self.target, *(c.channel for c in self.conditioned_on)]


class DriftConfig(StrictModel):
    """Per-lap mean of ``target`` under ``when``, CUSUM-ed against the baseline laps."""

    kind: Literal["drift"]
    target: str
    when: list[When] = Field(default_factory=list, max_length=4)
    baseline: Literal["session_start", "stored"] = "session_start"
    stored_file: str | None = None
    # Clean laps that make the baseline: their mean is the expectation and
    # their standard deviation (floored by min_scale) is the unit of residual.
    baseline_laps: int = Field(default=5, ge=2, le=50)
    # One-second observations a lap needs under `when` before its mean counts.
    min_lap_samples: int = Field(default=20, ge=1)
    min_scale: float = Field(default=0.01, gt=0)
    # CUSUM allowance and decision threshold, in baseline standard deviations.
    cusum_k: float = Field(default=0.5, ge=0)
    cusum_h: float = Field(default=4.0, gt=0)
    direction: Literal["both", "up", "down"] = "both"
    severity: Severity = "warning"
    gate: Gate = "on_track"

    @property
    def channels(self) -> list[str]:
        return [self.target, *(w.channel for w in self.when)]


class RatioConfig(StrictModel):
    """``numerator / denominator`` (a list is averaged) should be one number per group."""

    kind: Literal["ratio"]
    numerator: str
    denominator: str | list[str]
    # Group the baseline by the integer value of this channel (car.gear).
    per: str | None = None
    when: list[When] = Field(default_factory=list, max_length=4)
    baseline: Literal["session_start", "stored"] = "session_start"
    stored_file: str | None = None
    baseline_seconds: int = Field(default=180, ge=1, le=3600)
    min_group_samples: int = Field(default=30, ge=2)
    residual_sigma: float = Field(default=3, gt=0)
    min_scale: float = Field(default=0.001, gt=0)
    score_window: float = Field(default=30, gt=0)
    open_finding_above: float = Field(default=0.8, gt=0, lt=1)
    # What a persistently low or high ratio means for this pair, for the summary.
    low_means: str | None = None
    high_means: str | None = None
    severity: Severity = "warning"
    gate: Gate = "on_track"

    @field_validator("score_window", mode="before")
    @classmethod
    def seconds(cls, value: float | str) -> float | str:
        return _seconds(value)

    @field_validator("denominator")
    @classmethod
    def non_empty_denominator(cls, value: str | list[str]) -> str | list[str]:
        if isinstance(value, list) and not value:
            raise ValueError("denominator needs at least one channel")
        return value

    @property
    def denominators(self) -> list[str]:
        return [self.denominator] if isinstance(self.denominator, str) else list(self.denominator)

    @property
    def channels(self) -> list[str]:
        return [
            self.numerator,
            *self.denominators,
            *([self.per] if self.per else []),
            *(w.channel for w in self.when),
        ]


class CounterConfig(StrictModel):
    """A monotonic count whose rate should be zero."""

    kind: Literal["counter"]
    channel: str
    when: list[When] = Field(default_factory=list, max_length=4)
    # Nothing is learned: the expectation is that the count does not move.
    baseline: Literal["unchanged"] = "unchanged"
    # The rate is measured over this many seconds of eligible observations.
    score_window: float = Field(default=60, gt=0)
    # A finding opens when the count rises at least this fast.
    open_finding_above: float = Field(default=1.0, gt=0, description="counts per minute")
    severity: Severity = "warning"
    gate: Gate = "on_track"

    @field_validator("score_window", mode="before")
    @classmethod
    def seconds(cls, value: float | str) -> float | str:
        return _seconds(value)

    @property
    def channels(self) -> list[str]:
        return [self.channel, *(w.channel for w in self.when)]


class WholeCarConfig(StrictModel):
    """One monitor over a channel list: each channel predicted from all the others."""

    kind: Literal["whole_car"]
    channels: list[str] = Field(min_length=3, max_length=40)
    baseline: Literal["session_start", "stint_start", "stored"] = "stint_start"
    stored_file: str | None = None
    baseline_minutes: float = Field(default=15, gt=0, le=120)
    score_window: float = Field(default=60, gt=0)
    # The overall score, in baseline residual standard deviations.
    open_finding_above: float = Field(default=4.0, gt=0)
    severity: Severity = "warning"
    gate: Gate = "on_track"

    @field_validator("score_window", mode="before")
    @classmethod
    def seconds(cls, value: float | str) -> float | str:
        return _seconds(value)

    @field_validator("channels")
    @classmethod
    def distinct(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("whole_car channels must be distinct")
        return value


MonitorConfig = Annotated[
    EnvelopeConfig | DriftConfig | RatioConfig | CounterConfig | WholeCarConfig,
    Field(discriminator="kind"),
]


def channels_of(config: MonitorConfig) -> list[str]:
    """Every channel a monitor needs fresh before it has an opinion."""
    return list(config.channels)


class WatchConfig(StrictModel):
    baseline_rpm_min: float = Field(default=1200, ge=0)
    max_age_seconds: float = Field(default=10, ge=1)
    monitors: dict[str, MonitorConfig] = Field(min_length=1)

    @field_validator("monitors", mode="before")
    @classmethod
    def default_kind(cls, value: object) -> object:
        # The envelope was the only kind before P7.6; a monitor that names
        # none is still one.
        if isinstance(value, dict):
            for monitor in value.values():
                if isinstance(monitor, dict):
                    monitor.setdefault("kind", "envelope")
        return value

    @field_validator("monitors")
    @classmethod
    def names(cls, value: dict[str, MonitorConfig]) -> dict[str, MonitorConfig]:
        if any(not re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in value):
            raise ValueError("monitor names must be lower snake case")
        return value


def load_config(path: Path) -> WatchConfig:
    config = WatchConfig.model_validate(yaml.safe_load(path.read_text()))
    catalog = yaml.safe_load(path.with_name("catalog.yaml").read_text())["channels"]
    for name, monitor in config.monitors.items():
        for channel in channels_of(monitor):
            if channel not in catalog:
                raise ValueError(f"{name}: watch channel absent from catalog: {channel}")
        if monitor.baseline == "stored" and not getattr(monitor, "stored_file", None):
            raise ValueError(f"{name}: stored baseline needs stored_file")
    return config
