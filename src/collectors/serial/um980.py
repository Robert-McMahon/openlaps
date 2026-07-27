"""Unicore UM980 startup configuration and RTCM write-back driver."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Protocol

from core.config import DriverSettings

SUPPORTED_RATES_HZ = {
    1: "1",
    2: "0.5",
    5: "0.2",
    10: "0.1",
    20: "0.05",
    50: "0.02",
}
SENTENCE_COMMANDS = {
    "RMC": "GPRMC",
    "GGA": "GPGGA",
    "VTG": "GPVTG",
    "GSA": "GPGSA",
    "GSV": "GPGSV",
    "GST": "GPGST",
    "GLL": "GPGLL",
    "ZDA": "GPZDA",
}


class SerialPort(Protocol):
    """Subset of pyserial used by the driver and its test fakes."""

    def reset_input_buffer(self) -> None: ...

    def write(self, data: bytes, /) -> int | None: ...

    def readline(self, size: int = -1, /) -> bytes: ...


class UM980ConfigurationError(RuntimeError):
    """The receiver rejected or failed to acknowledge startup configuration."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of one command sent to the receiver."""

    command: str
    ok: bool
    response: str


class UM980Driver:
    """Apply configured NMEA output and accept correction write-back."""

    def __init__(
        self,
        port: SerialPort,
        settings: DriverSettings,
        *,
        ack_timeout_s: float = 2.0,
    ) -> None:
        self._port = port
        self._settings = settings
        self._ack_timeout_s = ack_timeout_s
        self._write_lock = threading.RLock()
        self._commands = _startup_commands(settings)

    def configure(self) -> None:
        """Apply startup settings, failing on the first bad acknowledgement."""
        if not self._settings.configure_on_start:
            return
        for command in self._commands:
            try:
                result = self._send_command(command)
            except OSError as exc:
                raise UM980ConfigurationError(f"UM980 command {command!r} failed: {exc}") from exc
            if not result.ok:
                raise UM980ConfigurationError(
                    f"UM980 command {command!r} failed: {result.response}"
                )

    def write_rtcm(self, payload: bytes) -> int:
        """Write one correction payload unchanged to the receiver."""
        if not payload:
            return 0
        with self._write_lock:
            return self._write_all(payload)

    def _send_command(self, command: str) -> CommandResult:
        with self._write_lock:
            self._port.reset_input_buffer()
            self._write_all(command.encode("ascii") + b"\r\n")
            deadline = time.monotonic() + self._ack_timeout_s
            while time.monotonic() < deadline:
                line = self._port.readline(513).decode("ascii", errors="replace").strip()
                try:
                    result = parse_command_response(line)
                except ValueError as exc:
                    return CommandResult(command, False, str(exc))
                if result is not None and result.command.upper() == command.upper():
                    return result
        return CommandResult(command, False, "timeout")

    def _write_all(self, payload: bytes) -> int:
        written = 0
        while written < len(payload):
            count = self._port.write(payload[written:])
            if count is None or count <= 0:
                raise OSError("serial write made no progress")
            written += count
        return written


def parse_command_response(line: str) -> CommandResult | None:
    """Parse a UM980 ``$command,...,response: ...`` acknowledgement."""
    if not line.startswith("$command,"):
        return None
    framed_body = line[1:]
    body_with_prefix, separator, supplied_checksum = framed_body.rpartition("*")
    if not separator:
        raise ValueError("missing UM980 acknowledgement checksum")
    if len(supplied_checksum) != 2:
        raise ValueError("invalid UM980 acknowledgement checksum field")
    checksum = 0
    for character in body_with_prefix:
        checksum ^= ord(character)
    if supplied_checksum.upper() != f"{checksum:02X}":
        raise ValueError("invalid UM980 acknowledgement checksum")
    framed_body = body_with_prefix
    body = framed_body[len("command,") :]
    command, separator, response = body.rpartition(",response:")
    if not separator or not command.strip():
        return None
    status = response.strip()
    return CommandResult(command.strip(), status.upper() == "OK", status)


def _startup_commands(settings: DriverSettings) -> tuple[str, ...]:
    try:
        interval = SUPPORTED_RATES_HZ[settings.rate_hz]
    except KeyError:
        supported = ", ".join(str(rate) for rate in SUPPORTED_RATES_HZ)
        raise ValueError(
            f"unsupported UM980 rate {settings.rate_hz} Hz; supported rates: {supported}"
        ) from None

    commands = ["UNLOG"]
    for configured_sentence in settings.sentences:
        sentence = configured_sentence.upper()
        try:
            command = SENTENCE_COMMANDS[sentence]
        except KeyError:
            raise ValueError(f"unsupported UM980 NMEA sentence {configured_sentence!r}") from None
        commands.append(f"{command} {interval}")
    return tuple(commands)
