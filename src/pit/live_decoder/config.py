"""Strict pit-side live view configuration for the live-decoder."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Annotated, Any, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.pb import telemetry_pb2 as pb

_NON_EMPTY = Annotated[str, Field(min_length=1)]
_RATE = Annotated[float, Field(ge=0, allow_inf_nan=False)]
ModelT = TypeVar("ModelT", bound=BaseModel)


class ConfigError(ValueError):
    """A live-decoder config file could not be parsed or validated."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"unhashable mapping key {key!r}",
                key_node.start_mark,
            ) from exc
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LiveDefaults(StrictModel):
    """Rates inherited by channel rules and the aggregate safety valve."""

    max_hz: _RATE = 10
    total_max_hz: _RATE = 500


class ChannelRule(StrictModel):
    """One ordered glob rule; first matching rule wins."""

    match: _NON_EMPTY
    max_hz: _RATE | None = None


class LiveConfig(StrictModel):
    """Complete pit-side live view configuration.

    `vehicle` is optional and normally absent. The deployment's car is named
    once, by `OPENLAPS_VEHICLE_ID`, which every other pit service already
    reads -- a committed config that named a car would be a third copy of it,
    and this file ships in a public repository that carries one example
    profile and no real vehicle (ADR 0007). Set it here only to pin *this*
    service to a different car than the rest of the pit; when both are set
    they must agree, and `live_decoder.service` refuses to start if they do
    not.
    """

    vehicle: _NON_EMPTY | None = None
    defaults: LiveDefaults = LiveDefaults()
    channels: list[ChannelRule] = Field(min_length=1)

    def rule_for(self, channel: str) -> ChannelRule | None:
        """Return the first matching rule, preserving file order."""
        return next(
            (rule for rule in self.channels if fnmatch.fnmatchcase(channel, rule.match)),
            None,
        )

    def max_hz_for(self, channel: str) -> float | None:
        """Effective per-channel ceiling, or None when the channel is not selected."""
        rule = self.rule_for(channel)
        if rule is None:
            return None
        return self.defaults.max_hz if rule.max_hz is None else rule.max_hz

    def unmatched_rules(self, registry: pb.ChannelRegistry) -> list[str]:
        """Configured globs matching no channel in this registry."""
        names = [channel.name for channel in registry.channels]
        return [
            rule.match
            for rule in self.channels
            if not any(fnmatch.fnmatchcase(name, rule.match) for name in names)
        ]


def load_live_config(path: str | Path) -> LiveConfig:
    """Read and validate a live view YAML file with precise errors."""
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{config_path}: unable to decode file as UTF-8: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{config_path}: unable to read file: {exc.strerror or exc}") from exc
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        reason = getattr(exc, "problem", None) or str(exc)
        raise ConfigError(f"{config_path}: YAML error: {reason}") from exc
    try:
        return LiveConfig.model_validate(data)
    except ValidationError as exc:
        error = exc.errors(include_url=False)[0]
        key = ".".join(str(part) for part in error["loc"]) or "<root>"
        raise ConfigError(f"{config_path}: {key}: {error['msg']}") from exc
