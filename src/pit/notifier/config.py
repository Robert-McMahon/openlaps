"""Settings and the channel policy for the notifier."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pit.db.dsn import dsn_from_env
from pit.live_decoder.config import _UniqueKeyLoader

_NON_EMPTY = Annotated[str, Field(min_length=1)]
_POSITIVE = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class ConfigError(ValueError):
    """The notifier config could not be read or validated."""


class ChannelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: _NON_EMPTY
    # `annunciator` and `log` ship with P7.2; `ntfy` and `discord` are P7.3's
    # modules behind the same interface.
    type: Literal["annunciator", "log", "ntfy", "discord"]
    min_severity: Literal["none", "warning", "critical"] = "warning"
    # How often an unacknowledged alert is re-delivered on this channel.
    # None means once. Acknowledging stops repeats; resolving stops them too.
    repeat_s: _POSITIVE | None = None
    # Channel-specific settings (a base URL, topic names); secrets come from
    # the environment, never from this file.
    settings: dict[str, str] = Field(default_factory=dict)


class NotifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # The uid of the always-firing Grafana rule that proves the path is alive
    # (profiles/<car>/alarms.yaml, kind: heartbeat) and how often its
    # notification policy repeats it.
    heartbeat_rule: _NON_EMPTY = "notifier-heartbeat"
    heartbeat_expected_s: _POSITIVE = 60.0
    heartbeat_missed_intervals: int = Field(default=3, ge=1)
    retry_deadline_s: _POSITIVE = 1800.0
    channels: tuple[ChannelConfig, ...] = Field(min_length=1)


def load_config(path: str | Path) -> NotifierConfig:
    config_path = Path(path)
    try:
        data = yaml.load(config_path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"{config_path}: {exc}") from exc
    try:
        return NotifierConfig.model_validate(data)
    except ValidationError as exc:
        error = exc.errors(include_url=False)[0]
        key = ".".join(str(part) for part in error["loc"]) or "<root>"
        raise ConfigError(f"{config_path}: {key}: {error['msg']}") from exc


@dataclass(frozen=True, slots=True)
class NotifierSettings:
    config_path: Path
    dsn: str | None
    host: str = "127.0.0.1"
    port: int = 8086
    vehicle_id: str | None = None

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, config_path: str | Path | None = None
    ) -> NotifierSettings:
        env = os.environ if env is None else env
        port = int(env.get("OPENLAPS_NOTIFIER_PORT", "8086"))
        if not 0 <= port <= 65535:
            raise ValueError("OPENLAPS_NOTIFIER_PORT must be between 0 and 65535")
        try:
            dsn: str | None = dsn_from_env(env)
        except ValueError:
            # No database is a degraded notifier, not a dead one: alerts still
            # annunciate, and /health says the ledger is missing.
            dsn = None
        return cls(
            config_path=Path(
                config_path
                or env.get("OPENLAPS_NOTIFIER_CONFIG", "deploy/pit-config/notifier.yaml")
            ),
            dsn=dsn,
            host=env.get("OPENLAPS_NOTIFIER_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=port,
            vehicle_id=env.get("OPENLAPS_VEHICLE_ID", "").strip() or None,
        )
