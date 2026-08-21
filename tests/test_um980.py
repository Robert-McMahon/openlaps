"""UM980 startup configuration and RTCM write-back tests."""

import pytest

from collectors.serial.um980 import (
    UM980ConfigurationError,
    UM980Driver,
    _checksummed_command,
    _startup_commands,
    parse_command_response,
)
from core.config import DriverSettings


class FakeSerial:
    def __init__(self, responses: list[bytes], *, write_limit: int | None = None) -> None:
        self.responses = list(responses)
        self.writes: list[bytes] = []
        self.resets = 0
        self.write_limit = write_limit

    def reset_input_buffer(self) -> None:
        self.resets += 1

    def write(self, data: bytes) -> int:
        written = len(data) if self.write_limit is None else min(len(data), self.write_limit)
        self.writes.append(data[:written])
        return written

    def readline(self, size: int = -1) -> bytes:
        return self.responses.pop(0) if self.responses else b""


class ScriptedSerial(FakeSerial):
    def __init__(self, script: list[tuple[bytes, list[bytes]]]) -> None:
        super().__init__([])
        self.script = list(script)

    def write(self, data: bytes) -> int:
        expected, responses = self.script.pop(0)
        assert data == expected
        self.responses = list(responses)
        self.writes.append(data)
        return len(data)


def _settings(**overrides) -> DriverSettings:
    values = {"rate_hz": 50, "sentences": ["RMC"], "configure_on_start": True}
    values.update(overrides)
    return DriverSettings(**values)


def test_configure_switches_to_checksummed_commands():
    port = ScriptedSerial(
        [
            (
                b"CONFIG CMDFORMAT 1\r\n",
                [b"$command,CONFIG CMDFORMAT 1,response: OK*08\r\n"],
            ),
            (b"$UNLOG*5F\r\n", [b"$command,UNLOG,response: OK*01\r\n"]),
            (
                b"$GPRMC 0.02*77\r\n",
                [
                    b"$GNRMC,ignored while waiting for ack\r\n",
                    b"$command,GPRMC 0.02,response: OK*29\r\n",
                ],
            ),
        ]
    )
    driver = UM980Driver(port, _settings())

    driver.configure()

    assert port.writes == [
        b"CONFIG CMDFORMAT 1\r\n",
        b"$UNLOG*5F\r\n",
        b"$GPRMC 0.02*77\r\n",
    ]
    assert port.resets == 3


def test_startup_configures_fix_gated_gps_pps_and_one_hz_timing_port():
    settings = _settings(
        pps={
            "mode": "ENABLE",
            "time_reference": "GPS",
            "polarity": "POSITIVE",
            "width_us": 500000,
            "period_ms": 1000,
            "rf_delay_ns": 0,
            "user_delay_ns": 0,
        },
        timing_output={"port": "COM2", "baud": 115200},
    )

    assert _startup_commands(settings) == (
        "UNLOG",
        "GPRMC 0.02",
        "CONFIG COM2 115200 8 N 1",
        "CONFIG PPS ENABLE GPS POSITIVE 500000 1000 0 0",
        "GPZDA COM2 1",
        "GPGGA COM2 1",
    )


@pytest.mark.parametrize("user_delay_ns", [-32769, 32768])
def test_pps_user_delay_rejects_values_outside_the_um980_range(user_delay_ns: int):
    with pytest.raises(ValueError):
        _settings(pps={"user_delay_ns": user_delay_ns})


@pytest.mark.parametrize("user_delay_ns", [-32768, 32767])
def test_pps_user_delay_accepts_documented_endpoints(user_delay_ns: int):
    settings = _settings(pps={"user_delay_ns": user_delay_ns})

    assert settings.pps is not None
    assert settings.pps.user_delay_ns == user_delay_ns


def test_configure_fails_immediately_on_bad_ack():
    port = ScriptedSerial(
        [
            (
                b"CONFIG CMDFORMAT 1\r\n",
                [b"$command,CONFIG CMDFORMAT 1,response: OK*08\r\n"],
            ),
            (
                b"$UNLOG*5F\r\n",
                [b"$command,UNLOG,response: PARSING FAILD! NO MATCHING FUNC*3E\r\n"],
            ),
        ]
    )
    driver = UM980Driver(port, _settings())

    with pytest.raises(UM980ConfigurationError, match="UNLOG.*PARSING FAILD"):
        driver.configure()

    assert port.writes == [b"CONFIG CMDFORMAT 1\r\n", b"$UNLOG*5F\r\n"]


def test_write_rtcm_forwards_bytes_unchanged():
    port = FakeSerial([])
    driver = UM980Driver(port, _settings(configure_on_start=False))

    assert driver.write_rtcm(b"\xd3\x00\x03payload") == 10
    assert port.writes == [b"\xd3\x00\x03payload"]


def test_write_rtcm_completes_partial_serial_writes():
    port = FakeSerial([], write_limit=3)
    driver = UM980Driver(port, _settings(configure_on_start=False))

    assert driver.write_rtcm(b"abcdefgh") == 8
    assert b"".join(port.writes) == b"abcdefgh"


def test_abbreviated_parser_accepts_acks_regardless_of_checksum_field():
    """Bench-proven predecessor behaviour: the ``*hh`` trailer is stripped,
    never verified — the real UM980's acks do not validate under NMEA XOR,
    and a stricter port of this parser failed against actual hardware."""
    for ack in (
        "$command,UNLOG,response: OK*00",  # checksum that doesn't XOR-validate
        "$command,UNLOG,response: OK*21",
        "$command,UNLOG,response: OK",  # no checksum trailer at all
    ):
        result = parse_command_response(ack)
        assert result is not None and result.ok


@pytest.mark.parametrize(
    "ack",
    [
        b"$command,UNLOG,response: OK*00\r\n",
        b"$command,UNLOG,response: OK\r\n",
    ],
)
def test_checksummed_mode_rejects_invalid_ack_checksum(ack: bytes):
    port = ScriptedSerial(
        [
            (
                b"CONFIG CMDFORMAT 1\r\n",
                [b"$command,CONFIG CMDFORMAT 1,response: OK*08\r\n"],
            ),
            (b"$UNLOG*5F\r\n", [ack]),
        ]
    )
    driver = UM980Driver(port, _settings(), ack_timeout_s=0.001)

    with pytest.raises(UM980ConfigurationError, match="checksum"):
        driver.configure()


def test_configure_retries_mode_switch_checksummed_if_receiver_is_already_in_mode_one():
    port = ScriptedSerial(
        [
            (b"CONFIG CMDFORMAT 1\r\n", [b"$command,unparseable acknowledgement\r\n"]),
            (
                b"$CONFIG CMDFORMAT 1*72\r\n",
                [b"$command,CONFIG CMDFORMAT 1,response: OK*2C\r\n"],
            ),
            (b"$UNLOG*5F\r\n", [b"$command,UNLOG,response: OK*01\r\n"]),
            (
                b"$GPRMC 0.02*77\r\n",
                [b"$command,GPRMC 0.02,response: OK*29\r\n"],
            ),
        ]
    )
    driver = UM980Driver(port, _settings(), ack_timeout_s=0.001)

    driver.configure()

    assert port.writes[:2] == [
        b"CONFIG CMDFORMAT 1\r\n",
        b"$CONFIG CMDFORMAT 1*72\r\n",
    ]


def test_configure_does_not_retry_an_explicit_mode_switch_rejection():
    port = ScriptedSerial(
        [
            (
                b"CONFIG CMDFORMAT 1\r\n",
                [b"$command,CONFIG CMDFORMAT 1,response: PARSING FAILD! NO MATCHING FUNC*37\r\n"],
            ),
        ]
    )
    driver = UM980Driver(port, _settings(), ack_timeout_s=0.001)

    with pytest.raises(UM980ConfigurationError, match="PARSING FAILD"):
        driver.configure()

    assert port.writes == [b"CONFIG CMDFORMAT 1\r\n"]


def test_configure_still_fails_on_error_acks():
    port = ScriptedSerial(
        [
            (
                b"CONFIG CMDFORMAT 1\r\n",
                [b"$command,CONFIG CMDFORMAT 1,response: OK*08\r\n"],
            ),
            (
                b"$UNLOG*5F\r\n",
                [b"$command,UNLOG,response: PARSING FAILD! NO MATCHING FUNC*3E\r\n"],
            ),
        ]
    )
    driver = UM980Driver(port, _settings(), ack_timeout_s=0.05)

    with pytest.raises(UM980ConfigurationError, match="PARSING FAILD"):
        driver.configure()


def test_configure_wraps_serial_io_failures():
    class BrokenSerial(FakeSerial):
        def reset_input_buffer(self) -> None:
            raise OSError("receiver unplugged")

    driver = UM980Driver(BrokenSerial([]), _settings())

    with pytest.raises(UM980ConfigurationError, match="receiver unplugged"):
        driver.configure()


# Captured from a real UM980 in CMDFORMAT 1 on 2026-08-13 -- the exact bytes
# that startup configuration used to reject. Their XOR validates only with the
# leading '$' inside the span.
BENCH_ACKS = (
    "$command,CONFIG CMDFORMAT 1,response: OK*2C",
    "$command,UNLOG,response: OK*01",
    "$command,GPRMC 0.02,response: OK*29",
    "$command,CONFIG,response: OK*54",
)


@pytest.mark.parametrize("ack", BENCH_ACKS)
def test_checksummed_mode_accepts_the_acks_real_hardware_sends(ack: str):
    result = parse_command_response(ack, verify_checksum=True)

    assert result is not None and result.ok


def test_commands_are_checksummed_over_the_nmea_span():
    """The two directions disagree, and the hardware settles both.

    The same bench session that produced ``BENCH_ACKS`` established that the
    receiver acknowledges this framing and silently ignores a '$'-inclusive
    checksum, a wrong one, and no checksum at all. Widening this span to match
    the ack span would leave the agent unable to configure the receiver at
    all, with no error to read -- just silence and a timeout.
    """
    assert _checksummed_command("CONFIG CMDFORMAT 1") == "$CONFIG CMDFORMAT 1*72"
    assert _checksummed_command("UNLOG") == "$UNLOG*5F"
