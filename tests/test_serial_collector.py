"""Serial transport capture, configuration, and reconnect tests."""

import threading

import pytest
import serial
from pydantic import ValidationError

from collectors.serial.transport import MAX_SENTENCE_BYTES, SerialCollector
from core.config import DriverConfig, SerialConfig, Um980Settings
from core.samples import Sample

RMC = b"$GNRMC,061912.00,A,3145.8100,S,11548.7500,E,0.05,0.0,130626,,,D,V*1B\r\n"


class FakeSerial:
    def __init__(self, responses: list[bytes], stop: threading.Event | None = None) -> None:
        self.responses = list(responses)
        self.stop = stop
        self.writes: list[bytes] = []
        self.closed = False

    def reset_input_buffer(self) -> None:
        pass

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def readline(self, size: int = -1) -> bytes:
        if self.responses:
            return self.responses.pop(0)[:size] if size >= 0 else self.responses.pop(0)
        if self.stop is not None:
            self.stop.set()
        return b""

    def close(self) -> None:
        self.closed = True


def _config(*, configure_on_start: bool = True) -> SerialConfig:
    return SerialConfig(
        name="serial0",
        port="/dev/ttyUSB9",
        baud=115_200,
        decoder="nmea",
        driver=DriverConfig(
            name="um980",
            config=Um980Settings(
                rate_hz=50,
                sentences=["RMC"],
                configure_on_start=configure_on_start,
            ),
        ),
    )


def test_complete_sentence_is_stamped_once_and_emitted_as_samples():
    samples: list[Sample] = []
    collector = SerialCollector(
        _config(configure_on_start=False),
        samples.append,
        wall_clock=lambda monotonic_ns: monotonic_ns / 1e6,
    )

    assert collector.handle_line(RMC, t_mono_ns=123_000_000) == 5

    assert {sample.source_ref for sample in samples} == {
        "serial0:um980.RMC.lat",
        "serial0:um980.RMC.lon",
        "serial0:um980.RMC.speed",
        "serial0:um980.RMC.heading",
        "serial0:um980.RMC.mode",
    }
    assert {sample.t_mono_ns for sample in samples} == {123_000_000}
    assert {sample.t_wall_ms for sample in samples} == {123.0}


def test_run_configures_receiver_before_reading_sentences():
    stop = threading.Event()
    port = FakeSerial(
        [
            b"$command,CONFIG CMDFORMAT 1,response: OK*08\r\n",
            b"$command,UNLOG,response: OK*01\r\n",
            b"$command,GPRMC 0.02,response: OK*29\r\n",
            RMC,
        ],
        stop,
    )
    samples: list[Sample] = []
    collector = SerialCollector(
        _config(),
        samples.append,
        serial_factory=lambda config: port,
        backoff_start_s=0.001,
        backoff_max_s=0.001,
    )

    collector.run(stop)

    assert port.writes == [
        b"CONFIG CMDFORMAT 1\r\n",
        b"$UNLOG*5F\r\n",
        b"$GPRMC 0.02*77\r\n",
    ]
    assert len(samples) == 5
    assert port.closed


def test_absent_device_is_retried_without_raising():
    stop = threading.Event()
    attempts: list[str] = []

    def factory(config: SerialConfig):
        attempts.append(config.port)
        if len(attempts) == 3:
            stop.set()
        raise serial.SerialException("device absent")

    collector = SerialCollector(
        _config(),
        lambda sample: None,
        serial_factory=factory,
        backoff_start_s=0.001,
        backoff_max_s=0.001,
    )

    collector.run(stop)

    assert attempts == ["/dev/ttyUSB9"] * 3
    assert collector.stats.open_failures == 3


def test_configuration_io_failure_closes_and_reconnects():
    stop = threading.Event()
    ports: list[FakeSerial] = []

    class UnpluggedSerial(FakeSerial):
        def reset_input_buffer(self) -> None:
            raise serial.SerialException("receiver unplugged")

    def factory(config: SerialConfig) -> FakeSerial:
        if not ports:
            port = UnpluggedSerial([])
        else:
            port = FakeSerial(
                [
                    b"$command,CONFIG CMDFORMAT 1,response: OK*08\r\n",
                    b"$command,UNLOG,response: OK*01\r\n",
                    b"$command,GPRMC 0.02,response: OK*29\r\n",
                    RMC,
                ],
                stop,
            )
        ports.append(port)
        return port

    samples: list[Sample] = []
    collector = SerialCollector(
        _config(),
        samples.append,
        serial_factory=factory,
        backoff_start_s=0.001,
        backoff_max_s=0.001,
    )

    collector.run(stop)

    assert len(ports) == 2
    assert ports[0].closed and ports[1].closed
    assert collector.stats.configuration_failures == 1
    assert collector.stats.reconnects == 1
    assert len(samples) == 5


def test_oversized_unterminated_input_is_bounded_and_dropped():
    stop = threading.Event()
    port = FakeSerial([b"$" + b"x" * (MAX_SENTENCE_BYTES + 10)], stop)
    collector = SerialCollector(
        _config(configure_on_start=False),
        lambda sample: None,
        serial_factory=lambda config: port,
    )

    collector.run(stop)

    assert collector.stats.oversized_lines == 1
    assert collector.stats.samples == 0


def test_stop_keeps_live_thread_registered_after_join_timeout():
    entered = threading.Event()
    release = threading.Event()
    attempts = 0

    class BlockingSerial(FakeSerial):
        def readline(self, size: int = -1) -> bytes:
            entered.set()
            release.wait(timeout=5)
            return b""

    def factory(config: SerialConfig) -> BlockingSerial:
        nonlocal attempts
        attempts += 1
        return BlockingSerial([])

    collector = SerialCollector(
        _config(configure_on_start=False),
        lambda sample: None,
        serial_factory=factory,
    )
    collector.start()
    assert entered.wait(timeout=1)

    collector.stop(timeout=0.001)
    assert collector.is_running()
    collector.start()
    assert attempts == 1

    release.set()
    collector.stop(timeout=1)
    assert not collector.is_running()


# --------------------------------------------------- the driver seam (P6)


def test_the_registry_builds_the_driver_a_profile_names():
    from collectors.serial.drivers import DRIVERS, build_driver, is_supported
    from collectors.serial.um980 import UM980Driver

    assert is_supported("um980")
    assert not is_supported("f9p")
    assert set(DRIVERS) == {"um980"}

    driver = build_driver(_config().driver, FakeSerial([]))
    assert isinstance(driver, UM980Driver)


def test_an_unregistered_driver_name_is_refused_by_name():
    from collectors.serial.drivers import build_driver

    class Named:
        name = "f9p"
        config = None

    with pytest.raises(ValueError, match="unsupported serial driver 'f9p'.*um980"):
        build_driver(Named(), FakeSerial([]))


def test_an_unknown_driver_name_fails_at_profile_load_not_at_first_open():
    """The Literal on DriverConfig.name is what moves this error forward.

    A free-string name would be accepted here and only rejected inside a
    collector thread, which would then retry it with backoff forever.
    """
    with pytest.raises(ValidationError):
        SerialConfig(
            name="serial0",
            port="/dev/ttyUSB9",
            baud=115_200,
            decoder="nmea",
            driver={"name": "f9p", "config": {}},
        )


def test_the_retry_loop_handles_any_drivers_configuration_error():
    """The transport must not know which receiver refused it.

    Registering a non-UM980 driver that raises the generic error proves the
    reconnect path is keyed on `DriverConfigurationError`, not on
    `UM980ConfigurationError` -- which is what lets a second receiver reuse
    every line of this transport.
    """
    from collectors.serial import drivers
    from collectors.serial.driver import DriverConfigurationError

    attempts: list[int] = []

    class RefusingDriver:
        def __init__(self, port, settings) -> None:
            pass

        def configure(self) -> None:
            attempts.append(1)
            raise DriverConfigurationError("receiver said no")

        def write_rtcm(self, payload: bytes) -> int:
            return 0

    stop = threading.Event()
    collector = SerialCollector(
        _config(),
        lambda sample: None,
        serial_factory=lambda config: FakeSerial([]),
        backoff_start_s=0.0,
        backoff_max_s=0.0,
    )

    original = dict(drivers.DRIVERS)
    drivers.DRIVERS["um980"] = RefusingDriver
    try:
        threading.Timer(0.2, stop.set).start()
        collector.run(stop)
    finally:
        drivers.DRIVERS.clear()
        drivers.DRIVERS.update(original)

    assert attempts, "the driver was never asked to configure"
    assert collector.stats.configuration_failures == len(attempts)
