"""Unicore UM980 startup configuration and RTCM write-back driver."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from collectors.serial.driver import DriverConfigurationError, SerialPort
from core.config import Um980Settings

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
COMMAND_FORMAT = "CONFIG CMDFORMAT 1"


class UM980ConfigurationError(DriverConfigurationError):
    """The receiver rejected or failed to acknowledge startup configuration.

    Subclasses the generic error so the transport's retry loop catches it
    without knowing which receiver is attached.
    """


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
        settings: Um980Settings,
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
        command = COMMAND_FORMAT
        try:
            result = self._send_command(command)
            if result.response == "timeout":
                result = self._send_command(command, checksummed=True)
            self._require_ok(result, command)
            for command in self._commands:
                result = self._send_command(command, checksummed=True)
                self._require_ok(result, command)
        except OSError as exc:
            raise UM980ConfigurationError(f"UM980 command {command!r} failed: {exc}") from exc

    def write_rtcm(self, payload: bytes) -> int:
        """Write one correction payload unchanged to the receiver."""
        if not payload:
            return 0
        with self._write_lock:
            return self._write_all(payload)

    def _send_command(self, command: str, *, checksummed: bool = False) -> CommandResult:
        with self._write_lock:
            self._port.reset_input_buffer()
            wire_command = _checksummed_command(command) if checksummed else command
            self._write_all(wire_command.encode("ascii") + b"\r\n")
            deadline = time.monotonic() + self._ack_timeout_s
            while time.monotonic() < deadline:
                line = self._port.readline(513).decode("ascii", errors="replace").strip()
                try:
                    result = parse_command_response(line, verify_checksum=checksummed)
                except ValueError as exc:
                    return CommandResult(command, False, str(exc))
                if result is not None and result.command.upper() == command.upper():
                    return result
        return CommandResult(command, False, "timeout")

    @staticmethod
    def _require_ok(result: CommandResult, command: str) -> None:
        if not result.ok:
            raise UM980ConfigurationError(f"UM980 command {command!r} failed: {result.response}")

    def _write_all(self, payload: bytes) -> int:
        written = 0
        while written < len(payload):
            count = self._port.write(payload[written:])
            if count is None or count <= 0:
                raise OSError("serial write made no progress")
            written += count
        return written


def parse_command_response(line: str, *, verify_checksum: bool = False) -> CommandResult | None:
    """Parse a UM980 ``$command,...,response: ...`` acknowledgement.

    In the receiver's default abbreviated mode, any trailing ``*hh`` field is
    stripped but deliberately not verified. That is the predecessor's
    bench-proven behaviour: strict verification in this mode broke real
    hardware because the field is not a meaningful XOR checksum. After
    ``CONFIG CMDFORMAT 1`` the acks do validate, and ``verify_checksum``
    checks them over the span the receiver actually uses -- ``$`` included,
    unlike the commands we send it (see ``_verify_checksum``). Anything that
    is not a command acknowledgement returns ``None`` so callers keep
    scanning the interleaved NMEA stream.
    """
    if not line.startswith("$command,"):
        return None
    body = line[len("$command,") :]
    star = body.rfind("*")
    if star != -1:
        if verify_checksum:
            _verify_checksum(line)
        body = body[:star]
    elif verify_checksum:
        raise ValueError("missing UM980 acknowledgement checksum")
    command, separator, response = body.rpartition(",response:")
    if not separator or not command.strip():
        return None
    status = response.strip()
    return CommandResult(command.strip(), status.upper() == "OK", status)


def _checksummed_command(command: str) -> str:
    # Excludes the '$', NMEA-style. Confirmed against a real UM980 in
    # CMDFORMAT 1 on 2026-08-13: this framing is acknowledged, while a
    # '$'-inclusive checksum, a wrong one and no checksum at all are all
    # silently ignored -- the receiver validates what we send.
    return f"${command}*{_xor_checksum(command):02X}"


def _verify_checksum(line: str) -> None:
    # Includes the '$', which is *not* how the receiver wants commands
    # checksummed. The asymmetry is real, not a transcription slip: the same
    # bench session read back four acks, and all four XOR-validate only with
    # the '$' in the span --
    #     $command,CONFIG CMDFORMAT 1,response: OK*2C
    #     $command,UNLOG,response: OK*01
    #     $command,GPRMC 0.02,response: OK*29
    #     $command,CONFIG,response: OK*54
    # Verifying the NMEA span instead rejected every one of them, which is
    # what broke startup configuration against a receiver an earlier run had
    # already left in CMDFORMAT 1.
    body, separator, supplied_checksum = line.rpartition("*")
    if not separator or len(supplied_checksum) != 2:
        raise ValueError("invalid UM980 acknowledgement checksum field")
    if supplied_checksum.upper() != f"{_xor_checksum(body):02X}":
        raise ValueError("invalid UM980 acknowledgement checksum")


def _xor_checksum(body: str) -> int:
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return checksum


def _startup_commands(settings: Um980Settings) -> tuple[str, ...]:
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
    timing = settings.timing_output
    if timing is not None:
        commands.append(f"CONFIG {timing.port} {timing.baud} 8 N 1")
    pps = settings.pps
    if pps is not None:
        commands.append(
            f"CONFIG PPS {pps.mode} {pps.time_reference} {pps.polarity} "
            f"{pps.width_us} {pps.period_ms} {pps.rf_delay_ns} {pps.user_delay_ns}"
        )
    if timing is not None:
        # ZDA names the PPS second. GGA supplies explicit fix validity so the
        # timing head can stop immediately instead of trusting PPS holdover.
        commands.extend((f"GPZDA {timing.port} 1", f"GPGGA {timing.port} 1"))
    return tuple(commands)
