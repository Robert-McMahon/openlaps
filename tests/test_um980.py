"""UM980 startup configuration and RTCM write-back tests."""

import pytest

from collectors.serial.um980 import UM980ConfigurationError, UM980Driver
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


def _settings(**overrides) -> DriverSettings:
    values = {"rate_hz": 50, "sentences": ["RMC"], "configure_on_start": True}
    values.update(overrides)
    return DriverSettings(**values)


def test_configure_applies_profile_rate_and_sentences_in_order():
    port = FakeSerial(
        [
            b"$command,UNLOG,response: OK*25\r\n",
            b"$GNRMC,ignored while waiting for ack\r\n",
            b"$command,GPRMC 0.02,response: OK*0D\r\n",
        ]
    )
    driver = UM980Driver(port, _settings())

    driver.configure()

    assert port.writes == [b"UNLOG\r\n", b"GPRMC 0.02\r\n"]
    assert port.resets == 2


def test_configure_fails_immediately_on_bad_ack():
    port = FakeSerial([b"$command,UNLOG,response: PARSING FAILD! NO MATCHING FUNC*1A\r\n"])
    driver = UM980Driver(port, _settings())

    with pytest.raises(UM980ConfigurationError, match="UNLOG.*PARSING FAILD"):
        driver.configure()

    assert port.writes == [b"UNLOG\r\n"]


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


def test_configure_rejects_corrupted_ack_checksum():
    port = FakeSerial([b"$command,UNLOG,response: OK*00\r\n"])
    driver = UM980Driver(port, _settings(), ack_timeout_s=0.001)

    with pytest.raises(UM980ConfigurationError, match="checksum"):
        driver.configure()


def test_configure_rejects_ack_without_checksum():
    port = FakeSerial([b"$command,UNLOG,response: OK\r\n"])
    driver = UM980Driver(port, _settings(), ack_timeout_s=0.001)

    with pytest.raises(UM980ConfigurationError, match="checksum"):
        driver.configure()


def test_configure_wraps_serial_io_failures():
    class BrokenSerial(FakeSerial):
        def reset_input_buffer(self) -> None:
            raise OSError("receiver unplugged")

    driver = UM980Driver(BrokenSerial([]), _settings())

    with pytest.raises(UM980ConfigurationError, match="receiver unplugged"):
        driver.configure()
