"""Strict models and loaders for vehicle profiles and channel catalogs."""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

_DURATION_RE = re.compile(r"^(?P<value>(?:\d+(?:\.\d*)?|\.\d+))(?P<unit>ns|us|ms|s|m|h)$")
_DURATION_FACTORS = {
    "ns": 1,
    "us": 1_000,
    "ms": 1_000_000,
    "s": 1_000_000_000,
    "m": 60_000_000_000,
    "h": 3_600_000_000_000,
}
_CHANNEL_RE = re.compile(r"^(?:car\.[a-z0-9_]+|position\.[a-z0-9_]+|sys\.[a-z0-9_.]+)$")
_CAN_SOURCE_RE = re.compile(r"^(?P<transport>[^:]+):(?P<device>[^.]+)\.[^.]+\.[^.]+$")
_NON_EMPTY = Annotated[str, Field(min_length=1)]
ModelT = TypeVar("ModelT", bound=BaseModel)


class ConfigError(ValueError):
    """A profile file could not be parsed or validated."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses duplicate mapping keys."""


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
    """Base for configuration objects that reject misspelled keys."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class VehicleIdentity(StrictModel):
    """Stable identity of a vehicle profile."""

    id: _NON_EMPTY


class DbcConfig(StrictModel):
    """A DBC attached to a CAN bus under a source-reference alias."""

    device: _NON_EMPTY
    file: _NON_EMPTY


class BusConfig(StrictModel):
    """A SocketCAN transport and all DBCs decoded from it."""

    name: _NON_EMPTY
    interface: _NON_EMPTY
    bitrate: Annotated[int, Field(gt=0)]
    dbcs: list[DbcConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_devices(self) -> BusConfig:
        _require_unique([dbc.device for dbc in self.dbcs], "DBC device alias")
        return self


class PpsConfig(StrictModel):
    """UM980 PPS output parameters from ``CONFIG PPS``."""

    mode: Literal["ENABLE", "ENABLE2", "ENABLE3"] = "ENABLE"
    time_reference: Literal["GPS", "BDS", "GAL", "GLO"] = "GPS"
    polarity: Literal["POSITIVE", "NEGATIVE"] = "POSITIVE"
    width_us: Annotated[int, Field(gt=0)] = 500000
    period_ms: Annotated[int, Field(ge=50, le=20000, multiple_of=50)] = 1000
    rf_delay_ns: Annotated[int, Field(ge=-32768, le=32767)] = 0
    user_delay_ns: Annotated[int, Field(ge=-32768, le=32767)] = 0

    @model_validator(mode="after")
    def width_is_shorter_than_period(self) -> PpsConfig:
        if self.width_us >= self.period_ms * 1000:
            raise ValueError("PPS width_us must be smaller than period_ms")
        return self


class TimingOutputConfig(StrictModel):
    """Spare UM980 serial port feeding the RP2040 timing head."""

    port: Literal["COM1", "COM2", "COM3"] = "COM2"
    baud: Literal[9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600] = 115200


class Um980Settings(StrictModel):
    """Startup settings for the Unicore UM980, and only for it.

    `pps` and `timing_output` are this receiver's command surface, not a
    general one: they exist because `CONFIG PPS` and a spare `COMn` are how a
    UM980 is told to produce a timing signal. A different receiver brings a
    different model rather than growing optional fields on a shared one.
    """

    rate_hz: Annotated[int, Field(gt=0)]
    sentences: list[_NON_EMPTY] = Field(min_length=1)
    configure_on_start: bool
    pps: PpsConfig | None = None
    timing_output: TimingOutputConfig | None = None


class Um980DriverConfig(StrictModel):
    """The `um980` driver and the settings it accepts."""

    name: Literal["um980"]
    config: Um980Settings


# One receiver is supported today, so this is an alias rather than a union.
# A second one adds its settings model and config class beside UM980's and
# makes this a discriminated union:
#
#     DriverConfig = Annotated[
#         Um980DriverConfig | F9pDriverConfig, Field(discriminator="name")
#     ]
#
# `name` is a Literal rather than a free string so an unknown driver fails at
# profile load, naming the field, instead of at the first port open inside a
# collector thread that then has to retry it forever.
DriverConfig = Um980DriverConfig


class SerialConfig(StrictModel):
    """A serial transport and its decoder/optional device driver.

    ``raw_log`` asks the agent to tee every received line, before decode, to
    crash-safe capture files (``docs/RAW_CAPTURE.md``). The profile says
    *whether* to capture; *where* is deploy wiring (``OPENLAPS_RAW_CAPTURE_DIR``).
    """

    name: _NON_EMPTY
    port: _NON_EMPTY
    baud: Annotated[int, Field(gt=0)]
    decoder: _NON_EMPTY
    driver: DriverConfig | None = None
    raw_log: bool = False


_TEMPERATURE_ALIAS_RE = re.compile(r"[a-z0-9_]+")
_TEMPERATURE_SENSOR_RE = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")


class HostConfig(StrictModel):
    """Host metrics collector settings.

    `temperatures` names the board's sensors behind stable aliases: each
    ``<alias>: <chip>.<label>`` entry makes the collector emit
    ``host:temp.<alias>`` carrying the reading of ``host:temp.<chip>.<label>``,
    which is how psutil names it after ``collectors.host`` normalizes it.
    A catalog maps the alias (``host:temp.cpu``), so the channel is the same
    on every board and only this mapping -- a property of the SBC, overlaid
    per target by ``hardware.yaml`` (ADR 0010) -- says where it comes from.
    The raw ``host:temp.<chip>.<label>`` refs are still emitted alongside.
    """

    enabled: bool
    interval: str
    temperatures: dict[str, str] = Field(default_factory=dict)
    interval_ns: int = Field(init=False, exclude=True, default=0)

    @model_validator(mode="after")
    def parse_interval(self) -> HostConfig:
        object.__setattr__(self, "interval_ns", parse_duration_ns(self.interval))
        return self

    @model_validator(mode="after")
    def temperature_aliases_are_well_formed(self) -> HostConfig:
        for alias, sensor in self.temperatures.items():
            if not _TEMPERATURE_ALIAS_RE.fullmatch(alias):
                raise ValueError(
                    f"temperature alias {alias!r} must be lowercase letters, digits and '_'"
                )
            if not _TEMPERATURE_SENSOR_RE.fullmatch(sensor):
                raise ValueError(
                    f"temperature sensor {sensor!r} for {alias!r} must be '<chip>.<label>', "
                    "as the collector names it in host:temp.<chip>.<label>"
                )
        return self


class VehicleConfig(StrictModel):
    """Validated contents of vehicle.yaml."""

    vehicle: VehicleIdentity
    buses: list[BusConfig]
    serial: list[SerialConfig]
    host: HostConfig

    @model_validator(mode="after")
    def unique_transports(self) -> VehicleConfig:
        _require_unique([bus.name for bus in self.buses], "bus name")
        _require_unique([source.name for source in self.serial], "serial source name")
        names = [bus.name for bus in self.buses] + [source.name for source in self.serial]
        _require_unique(names, "transport name")
        return self


class RbeConfig(StrictModel):
    """Report-by-exception policy expressed in monotonic nanoseconds."""

    deadband: Annotated[float, Field(ge=0)] | None = None
    min_interval: str | None = None
    max_interval: str | None = None
    min_interval_ns: int | None = Field(init=False, exclude=True, default=None)
    max_interval_ns: int | None = Field(init=False, exclude=True, default=None)

    @model_validator(mode="after")
    def parse_intervals(self) -> RbeConfig:
        if self.min_interval is not None:
            object.__setattr__(self, "min_interval_ns", parse_duration_ns(self.min_interval))
        if self.max_interval is not None:
            object.__setattr__(self, "max_interval_ns", parse_duration_ns(self.max_interval))
        if (
            self.min_interval_ns is not None
            and self.max_interval_ns is not None
            and self.min_interval_ns > self.max_interval_ns
        ):
            raise ValueError("min_interval must not exceed max_interval")
        return self


class EncodeConfig(StrictModel):
    """Per-channel protobuf value-arm and optional fixed-point encoding."""

    type: Literal["double", "float", "uint", "int"]
    scale: float = 0.0
    offset: float = 0.0

    @model_validator(mode="after")
    def coefficients_only_for_integer_types(self) -> EncodeConfig:
        if not math.isfinite(self.scale):
            raise ValueError("scale must be finite")
        if not math.isfinite(self.offset):
            raise ValueError("offset must be finite")
        if self.scale == 0 and self.offset != 0:
            raise ValueError("offset must be zero when scale is zero")
        if self.type not in {"uint", "int"} and (self.scale != 0 or self.offset != 0):
            raise ValueError("scale/offset are only valid for uint or int encoding")
        if self.scale < 0:
            raise ValueError("scale must be non-negative")
        return self


class ChannelConfig(StrictModel):
    """Mapping and policy for one canonical channel."""

    source_ref: _NON_EMPTY = Field(alias="from")
    units: str = ""
    type: Literal["double", "int", "bool", "string"] = "double"
    rbe: RbeConfig | None = None
    encode: EncodeConfig | None = None

    @field_validator("encode", mode="before")
    @classmethod
    def expand_encoding_shorthand(cls, value: Any) -> Any:
        if isinstance(value, str):
            aliases = {"float32": "float", "int64": "int"}
            return {"type": aliases.get(value, value)}
        return value

    @model_validator(mode="after")
    def encoding_matches_value_kind(self) -> ChannelConfig:
        if self.type in {"bool", "string"} and self.encode is not None:
            raise ValueError(f"{self.type} channels cannot override their wire encoding")
        return self


class LapTimingConfig(StrictModel):
    """Timing-engine channel selection and track fixture."""

    position: _NON_EMPTY
    track: _NON_EMPTY


class AppsConfig(StrictModel):
    """In-agent application configuration."""

    lap_timing: LapTimingConfig | None = None


class CatalogConfig(StrictModel):
    """Validated contents of catalog.yaml, preserving channel order."""

    channels: dict[str, ChannelConfig] = Field(min_length=1)
    apps: AppsConfig

    @field_validator("channels")
    @classmethod
    def validate_channel_names(cls, channels: dict[str, ChannelConfig]) -> dict[str, ChannelConfig]:
        for name in channels:
            if not _CHANNEL_RE.fullmatch(name):
                raise ValueError(
                    f"channel {name!r} uses an invalid or reserved namespace; "
                    "expected car.*, position.*, or sys.* (lap.* and timing.* are reserved)"
                )
        return channels


class ProfileConfig(StrictModel):
    """The two validated files that define one vehicle profile.

    `hardware_target` names the host-wiring overlay that was applied on top
    of them, or is None when the profile was loaded as written. It is the
    overlay's name rather than its parsed contents so this module stays free
    of an import back from `core.hardware`, which builds on these models.
    """

    path: Path
    vehicle: VehicleConfig
    catalog: CatalogConfig
    catalog_hash: str
    hardware_target: str | None = None


def parse_duration_ns(value: str) -> int:
    """Parse a positive duration such as ``20ms`` or ``5s`` to nanoseconds."""
    match = _DURATION_RE.fullmatch(value)
    if match is None:
        raise ValueError("must be a duration such as '20ms', '5s', or '1m'")
    duration = float(match.group("value")) * _DURATION_FACTORS[match.group("unit")]
    if not math.isfinite(duration):
        raise ValueError("duration is too large")
    if duration <= 0:
        raise ValueError("duration must be greater than zero")
    return round(duration)


def load_profile(profile_dir: str | Path, hardware: str | Path | None = None) -> ProfileConfig:
    """Load and cross-validate ``vehicle.yaml`` and ``catalog.yaml``.

    `hardware`, when given, is a target's host-wiring overlay
    (``core.hardware``): it substitutes socketCAN interface names and serial
    device paths into the loaded profile before anything else looks at them,
    so one profile serves every board the car has ever been bolted to.
    """
    root = Path(profile_dir).resolve()
    vehicle_path = root / "vehicle.yaml"
    catalog_path = root / "catalog.yaml"
    vehicle = load_yaml_model(vehicle_path, VehicleConfig)
    overlay = None
    if hardware is not None:
        # Imported here rather than at module scope: `core.hardware` builds on
        # this module's models, so a top-level import would be circular.
        from core.hardware import apply_hardware, load_hardware

        overlay = load_hardware(hardware)
        vehicle = apply_hardware(vehicle, overlay, path=hardware)
    catalog_bytes = _read_bytes(catalog_path)
    try:
        catalog_text = catalog_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{catalog_path}: unable to decode file as UTF-8: {exc}") from exc
    catalog = _load_model_text(catalog_path, CatalogConfig, catalog_text)
    _validate_dbc_paths(vehicle, root, vehicle_path)
    _validate_source_refs(vehicle, catalog, catalog_path)
    return ProfileConfig(
        path=root,
        vehicle=vehicle,
        catalog=catalog,
        catalog_hash=hashlib.sha256(catalog_bytes).hexdigest(),
        hardware_target=overlay.target if overlay is not None else None,
    )


def load_yaml_model(path: Path, model: type[ModelT]) -> ModelT:  # noqa: UP047
    """Read one YAML file into `model`, reporting failures against `path`."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: unable to decode file as UTF-8: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: unable to read file: {exc.strerror or exc}") from exc
    return _load_model_text(path, model, text)


def format_validation_error(exc: ValidationError) -> str:
    """Render the first error as ``<dotted key>: <message>``."""
    error = exc.errors(include_url=False)[0]
    key = ".".join(str(part) for part in error["loc"]) or "<root>"
    return f"{key}: {error['msg']}"


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"{path}: unable to read file: {exc.strerror or exc}") from exc


def _load_model_text(path: Path, model: type[ModelT], text: str) -> ModelT:  # noqa: UP047
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        reason = getattr(exc, "problem", None) or str(exc)
        raise ConfigError(f"{path}: YAML error: {reason}") from exc
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path}: {format_validation_error(exc)}") from exc


def _validate_dbc_paths(vehicle: VehicleConfig, root: Path, path: Path) -> None:
    for bus_index, bus in enumerate(vehicle.buses):
        for dbc_index, dbc in enumerate(bus.dbcs):
            key = f"buses.{bus_index}.dbcs.{dbc_index}.file"
            configured_path = Path(dbc.file)
            dbc_path = root / configured_path
            try:
                dbc_path.resolve().relative_to(root)
            except ValueError:
                raise ConfigError(
                    f"{path}: {key}: DBC path must be relative to and contained within the profile"
                ) from None
            if configured_path.is_absolute():
                raise ConfigError(
                    f"{path}: {key}: DBC path must be relative to and contained within the profile"
                )
            if not dbc_path.is_file():
                raise ConfigError(f"{path}: {key}: referenced DBC {dbc.file!r} does not exist")


def _validate_source_refs(vehicle: VehicleConfig, catalog: CatalogConfig, path: Path) -> None:
    buses = {bus.name: {dbc.device for dbc in bus.dbcs} for bus in vehicle.buses}
    serial = {
        source.name: source.driver.name if source.driver is not None else source.decoder
        for source in vehicle.serial
    }
    for name, channel in catalog.channels.items():
        key = f"channels.{name}.from"
        source_ref = channel.source_ref
        if source_ref.startswith("host:"):
            if len(source_ref) == len("host:"):
                raise ConfigError(f"{path}: {key}: host metric is missing")
            continue
        match = _CAN_SOURCE_RE.fullmatch(source_ref)
        if match is None:
            raise ConfigError(f"{path}: {key}: invalid source reference {source_ref!r}")
        transport = match.group("transport")
        device = match.group("device")
        if transport in buses:
            if device not in buses[transport]:
                raise ConfigError(f"{path}: {key}: unknown device {device!r} on bus {transport!r}")
        elif transport in serial:
            if device != serial[transport]:
                raise ConfigError(
                    f"{path}: {key}: unknown driver {device!r} on serial source {transport!r}"
                )
        else:
            raise ConfigError(f"{path}: {key}: unknown transport {transport!r}")


def _require_unique(values: list[str], description: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {description} {value!r}")
        seen.add(value)
