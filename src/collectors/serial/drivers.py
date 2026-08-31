"""Which `driver.name` builds which driver.

The registry is a dict, not a plugin system: this codebase has no ORM and no
entry-point discovery, and a receiver is added by someone editing the
repository, not by dropping a package next to it. Adding one means a settings
model and a config class in `core.config`, a driver module implementing
`SerialDriver`, and one line here.

`core.config` already rejects an unknown `name` at profile load, so the
lookup below cannot fail in normal operation. It still raises rather than
returning None, because a registry that silently disagrees with the schema is
worse than one that says so.
"""

from __future__ import annotations

from collections.abc import Callable

from collectors.serial.driver import SerialDriver, SerialPort
from collectors.serial.um980 import UM980Driver
from core.config import DriverConfig

DriverFactory = Callable[[SerialPort, object], SerialDriver]

DRIVERS: dict[str, DriverFactory] = {
    "um980": UM980Driver,
}


def is_supported(name: str) -> bool:
    """Whether a driver name has an implementation registered."""
    return name in DRIVERS


def build_driver(config: DriverConfig, port: SerialPort) -> SerialDriver:
    """Build the driver `config` names, bound to an open `port`."""
    try:
        factory = DRIVERS[config.name]
    except KeyError:
        supported = ", ".join(sorted(DRIVERS)) or "none"
        raise ValueError(
            f"unsupported serial driver {config.name!r}; supported drivers: {supported}"
        ) from None
    return factory(port, config.config)
