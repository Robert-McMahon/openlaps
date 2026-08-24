"""Strict configuration for the pit timing extrapolator."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pit.live_decoder.config import _UniqueKeyLoader

_NON_EMPTY = Annotated[str, Field(min_length=1)]
_POSITIVE = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class ConfigError(ValueError):
    """The extrapolator config could not be read or validated."""


class TimingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    vehicle: _NON_EMPTY
    publish_hz: _POSITIVE = 10
    max_clock_offset_s: _POSITIVE = 0.1
    max_clock_stratum: int = Field(default=4, ge=1)
    allowed_clock_sources: tuple[_NON_EMPTY, ...] = ("GPS", "PPS", "NTP")
    best_lap_multiple: _POSITIVE = 3.0
    fallback_max_lap_s: _POSITIVE = 300.0

    @property
    def output_channels(self) -> tuple[str, str]:
        return ("timing.lap_elapsed_pit", "timing.sector_elapsed_pit")


def load_config(path: str | Path) -> TimingConfig:
    config_path = Path(path)
    try:
        data = yaml.load(config_path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"{config_path}: {exc}") from exc
    try:
        return TimingConfig.model_validate(data)
    except ValidationError as exc:
        error = exc.errors(include_url=False)[0]
        key = ".".join(str(part) for part in error["loc"]) or "<root>"
        raise ConfigError(f"{config_path}: {key}: {error['msg']}") from exc
