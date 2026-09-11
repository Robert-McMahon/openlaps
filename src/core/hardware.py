"""The host-hardware overlay: which board this profile is plugged into.

A vehicle profile describes *the car* -- which DBCs decode which bus, which
receiver is on which serial source, what the channels are called. Two of its
fields are not about the car at all: `buses[].interface` is a socketCAN
device name and `serial[].port` is a path under `/dev`, and both belong to
the SBC the agent happens to be running on. The same car moved from a Radxa
X4 to a Luckfox Omni3576 keeps every DBC and all 123 channels, and changes
exactly those two strings.

Until this module existed the only way to change them was to edit
`vehicle.yaml`, which is why the Phase 4 bench rig was once a byte-for-byte
copy of the whole example profile with three lines different, and why a car's
checkout ends up carrying a local edit that can never be committed. This
module is that same substitution made first-class:

    OPENLAPS_HARDWARE=deploy/targets/luckfox-omni3576/hardware.yaml

The overlay addresses transports by the profile's own `name` -- `can0`,
`serial0` -- which is the stable identifier the catalog's `from:` references
already resolve against, so remapping a port cannot invalidate a channel.
Naming a transport the profile does not define is an error at load, not a
silently ignored key: a typo in a hardware file must not leave the agent
opening the profile's default port while the operator believes otherwise.

Video is deliberately absent. go2rtc reads its own config and the agent
never touches a camera, so a `video:` block here would be configuration
nothing consumes; the encoder pipeline lives in the target's `go2rtc.yaml`
beside this file. See `deploy/targets/README.md` and ADR 0010.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, ValidationError, model_validator

from core.config import (
    ConfigError,
    StrictModel,
    VehicleConfig,
    format_validation_error,
    load_yaml_model,
)

_NON_EMPTY = Annotated[str, Field(min_length=1)]


class CanLinkConfig(StrictModel):
    """Extra ``ip link set <dev> up type can ...`` arguments for this board.

    These are link-layer bring-up arguments and nothing else: `tools/can_up.py`
    passes them to `ip`, and neither the profile nor the collector ever sees
    them. That is the distinction they exist for -- on a Luckfox Omni3576 the
    `rk3576_canfd` controller **cannot be brought up at all** without them:

        rk3576_canfd 2ac00000.can can0: incorrect/missing data bit-timing
        RTNETLINK answers: Invalid argument

    and adding `dbitrate` without `fd on` gets `Operation not supported`. The
    car's bus there is still ordinary 1 Mbit/s classic CAN -- an FD-mode
    controller receives classic frames unchanged, and nothing in this stack
    transmits -- so this is a property of the silicon, not of the car, and it
    belongs in a target rather than in `vehicle.yaml`.
    """

    fd: bool = False
    dbitrate: Annotated[int, Field(gt=0)] | None = None

    @model_validator(mode="after")
    def data_bitrate_needs_fd(self) -> CanLinkConfig:
        if self.dbitrate is not None and not self.fd:
            raise ValueError("dbitrate is only meaningful with fd: true")
        return self


class BusHardware(StrictModel):
    """Host wiring for one CAN bus named in the profile."""

    interface: _NON_EMPTY | None = None
    bitrate: Annotated[int, Field(gt=0)] | None = None
    link: CanLinkConfig | None = None

    def profile_fields(self) -> dict[str, Any]:
        """The subset that overrides `BusConfig`; `link` is not one of them."""
        return self.model_dump(exclude_none=True, exclude={"link"})


class SerialDriverHardware(StrictModel):
    """The one driver setting a *rig*, rather than a car, gets to decide.

    `configure_on_start` is not a property of the receiver: it answers "is a
    real receiver on the other end of this port, one that will answer
    commands?". On a car it is; on a bench rig whose `serial0` is a pty fed
    by `tools/bench_gps.py` it is not, and a driver that tries to configure
    the absent receiver logs a failure every reconnect.

    Deliberately one field and not the whole driver config. `rate_hz`,
    `sentences`, `pps` and `timing_output` describe the receiver the car
    carries and stay in `vehicle.yaml` where a reader can find them --
    inert but documenting, since `UM980Driver.configure` returns immediately
    when this is false.
    """

    configure_on_start: bool | None = None


class SerialHardware(StrictModel):
    """Host wiring for one serial source named in the profile."""

    port: _NON_EMPTY | None = None
    baud: Annotated[int, Field(gt=0)] | None = None
    driver: SerialDriverHardware | None = None

    def profile_fields(self) -> dict[str, Any]:
        """The subset that overrides `SerialConfig`; `driver` is nested."""
        return self.model_dump(exclude_none=True, exclude={"driver"})


class HardwareConfig(StrictModel):
    """Validated contents of a target's ``hardware.yaml``.

    Every field but `target` is optional. An overlay naming only
    `serial0.port` is a complete and useful one: it says "this board's GNSS
    receiver is on a different tty" and leaves the rest of the profile alone.
    """

    target: _NON_EMPTY
    buses: dict[str, BusHardware] = Field(default_factory=dict)
    serial: dict[str, SerialHardware] = Field(default_factory=dict)

    def link(self, bus_name: str) -> CanLinkConfig:
        """Bring-up arguments for `bus_name`, defaulted when it names none."""
        overlay = self.buses.get(bus_name)
        if overlay is None or overlay.link is None:
            return CanLinkConfig()
        return overlay.link


def load_hardware(path: str | Path) -> HardwareConfig:
    """Load and validate one target's hardware overlay."""
    return load_yaml_model(Path(path), HardwareConfig)


def apply_hardware(
    vehicle: VehicleConfig, hardware: HardwareConfig, *, path: str | Path
) -> VehicleConfig:
    """Return `vehicle` with `hardware`'s host wiring substituted in.

    Rebuilt through `VehicleConfig` rather than patched in place, so an
    overlay is held to exactly the validation `vehicle.yaml` is: a bitrate of
    zero, a blank port or a duplicate transport name fails here for the same
    reason and with the same message shape it would there.
    """
    _require_known(hardware.buses, {bus.name for bus in vehicle.buses}, "bus", path)
    _require_known(
        hardware.serial, {source.name for source in vehicle.serial}, "serial source", path
    )

    data: dict[str, Any] = vehicle.model_dump()
    for bus in data["buses"]:
        _apply(bus, hardware.buses.get(bus["name"]))
    for source in data["serial"]:
        overlay = hardware.serial.get(source["name"])
        _apply(source, overlay)
        _apply_driver(source, overlay, path=path)

    try:
        return VehicleConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path}: {format_validation_error(exc)}") from exc


def _apply(transport: dict[str, Any], overlay: BusHardware | SerialHardware | None) -> None:
    """Overwrite the fields the overlay actually sets, and only those."""
    if overlay is not None:
        transport.update(overlay.profile_fields())


def _apply_driver(
    source: dict[str, Any], overlay: SerialHardware | None, *, path: str | Path
) -> None:
    """Substitute the driver settings the rig decides, into the driver block."""
    if overlay is None or overlay.driver is None:
        return
    changes = overlay.driver.model_dump(exclude_none=True)
    if not changes:
        return
    if source["driver"] is None:
        raise ConfigError(
            f"{path}: serial source {source['name']!r} has no driver to configure; "
            "the profile attaches none to it"
        )
    source["driver"]["config"].update(changes)


def _require_known(
    named: dict[str, Any], known: set[str], description: str, path: str | Path
) -> None:
    unknown = sorted(set(named) - known)
    if unknown:
        listed = ", ".join(sorted(known)) or "none"
        raise ConfigError(
            f"{path}: {description} {unknown[0]!r} is not defined by the profile; "
            f"the profile defines: {listed}"
        )
