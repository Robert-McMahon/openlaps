"""Profile configuration; bin widths are always in catalog units."""

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Condition(StrictModel):
    channel: str
    bins: float = Field(gt=0)
    active_above: float | None = None


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
    severity: Literal["info", "warning", "critical"] = "warning"
    gate: Literal["on_track"] = "on_track"

    @field_validator("score_window", mode="before")
    @classmethod
    def seconds(cls, value: float | str) -> float | str:
        return float(value[:-1]) if isinstance(value, str) and value.endswith("s") else value


class WatchConfig(StrictModel):
    baseline_rpm_min: float = Field(default=1200, ge=0)
    max_age_seconds: float = Field(default=10, ge=1)
    monitors: dict[str, EnvelopeConfig] = Field(min_length=1)

    @field_validator("monitors")
    @classmethod
    def names(cls, value: dict[str, EnvelopeConfig]) -> dict[str, EnvelopeConfig]:
        if any(not re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in value):
            raise ValueError("monitor names must be lower snake case")
        return value


def load_config(path: Path) -> WatchConfig:
    config = WatchConfig.model_validate(yaml.safe_load(path.read_text()))
    catalog = yaml.safe_load(path.with_name("catalog.yaml").read_text())["channels"]
    for monitor in config.monitors.values():
        for channel in [monitor.target, *(c.channel for c in monitor.conditioned_on)]:
            if channel not in catalog:
                raise ValueError(f"watch channel absent from catalog: {channel}")
        if monitor.baseline == "stored" and not monitor.stored_file:
            raise ValueError("stored baseline needs stored_file")
    return config
