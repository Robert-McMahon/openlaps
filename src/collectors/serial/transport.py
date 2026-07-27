"""Blocking pyserial transport with reconnect and sentence capture stamping."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import serial

from collectors.clock import Emit, MonotonicWallClock, WallClock
from collectors.serial.nmea import NmeaDecoder
from collectors.serial.um980 import UM980ConfigurationError, UM980Driver
from core.config import SerialConfig
from core.samples import Sample

logger = logging.getLogger(__name__)

DEFAULT_BACKOFF_START_S = 0.5
DEFAULT_BACKOFF_MAX_S = 30.0
MAX_SENTENCE_BYTES = 1024


class SerialPort(Protocol):
    """Subset of pyserial used by the transport."""

    def reset_input_buffer(self) -> None: ...

    def write(self, data: bytes, /) -> int | None: ...

    def readline(self, size: int = -1, /) -> bytes: ...

    def close(self) -> None: ...


SerialFactory = Callable[[SerialConfig], SerialPort]


@dataclass(slots=True)
class SerialTransportStats:
    """Transport-side counters for one serial source."""

    open_failures: int = 0
    configuration_failures: int = 0
    read_errors: int = 0
    reconnects: int = 0
    lines: int = 0
    samples: int = 0
    oversized_lines: int = 0
    rtcm_write_failures: int = 0


class SerialCollector:
    """Read and decode one serial source, reconnecting when it is absent."""

    def __init__(
        self,
        config: SerialConfig,
        emit: Emit,
        *,
        wall_clock: WallClock | None = None,
        serial_factory: SerialFactory | None = None,
        backoff_start_s: float = DEFAULT_BACKOFF_START_S,
        backoff_max_s: float = DEFAULT_BACKOFF_MAX_S,
    ) -> None:
        device = config.driver.name if config.driver is not None else config.decoder
        if config.decoder != "nmea":
            raise ValueError(f"unsupported serial decoder {config.decoder!r}")
        if config.driver is not None and config.driver.name != "um980":
            raise ValueError(f"unsupported serial driver {config.driver.name!r}")

        self.config = config
        self.decoder = NmeaDecoder(config.name, device)
        self.stats = SerialTransportStats()
        self._emit = emit
        self._wall_clock = wall_clock if wall_clock is not None else MonotonicWallClock()
        self._serial_factory = serial_factory if serial_factory is not None else _open_serial
        self._backoff_start_s = backoff_start_s
        self._backoff_max_s = backoff_max_s
        self._active_lock = threading.Lock()
        self._active_driver: UM980Driver | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def name(self) -> str:
        return self.config.name

    def handle_line(self, line: bytes, *, t_mono_ns: int | None = None) -> int:
        """Decode and emit one complete line stamped at terminator arrival."""
        if t_mono_ns is None:
            t_mono_ns = time.monotonic_ns()
        values = self.decoder.decode(line)
        t_wall_ms = self._wall_clock(t_mono_ns)
        for source_ref, value in values:
            self._emit(Sample(source_ref, t_mono_ns, t_wall_ms, value))
        self.stats.lines += 1
        self.stats.samples += len(values)
        return len(values)

    def write_rtcm(self, payload: bytes) -> bool:
        """Best-effort correction write-back to the currently connected receiver."""
        with self._active_lock:
            driver = self._active_driver
        if driver is None:
            self.stats.rtcm_write_failures += 1
            return False
        try:
            written = driver.write_rtcm(payload)
        except (serial.SerialException, OSError):
            self.stats.rtcm_write_failures += 1
            logger.warning("serial %s: RTCM write failed", self.config.name, exc_info=True)
            return False
        if written != len(payload):
            self.stats.rtcm_write_failures += 1
            return False
        return True

    def run(self, stop: threading.Event | None = None) -> None:
        """Read until stopped, retrying open/config/read failures with backoff."""
        stop = self._stop if stop is None else stop
        backoff = self._backoff_start_s
        while not stop.is_set():
            port = self._open()
            if port is None:
                stop.wait(backoff)
                backoff = min(backoff * 2, self._backoff_max_s)
                continue

            connected = False
            try:
                driver = self._build_driver(port)
                configuration_ok = True
                if driver is not None:
                    try:
                        driver.configure()
                    except UM980ConfigurationError as exc:
                        self.stats.configuration_failures += 1
                        logger.error("serial %s: receiver configuration failed: %s", self.name, exc)
                        configuration_ok = False
                if configuration_ok:
                    with self._active_lock:
                        self._active_driver = driver
                    connected = self._read_until_error(port, stop)
            finally:
                with self._active_lock:
                    self._active_driver = None
                _close(port, self.name)

            if stop.is_set():
                break
            self.stats.reconnects += 1
            if connected:
                backoff = self._backoff_start_s
            stop.wait(backoff)
            backoff = min(backoff * 2, self._backoff_max_s)

    def start(self) -> None:
        """Run the transport loop on a daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._thread_main, name=f"serial-{self.name}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the transport loop to finish and join its thread."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if not thread.is_alive() and self._thread is thread:
                self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _thread_main(self) -> None:
        try:
            self.run(self._stop)
        except Exception:
            logger.exception("serial %s: collector thread failed", self.name)

    def _open(self) -> SerialPort | None:
        try:
            return self._serial_factory(self.config)
        except (serial.SerialException, OSError) as exc:
            self.stats.open_failures += 1
            logger.warning("serial %s: cannot open %s: %s", self.name, self.config.port, exc)
            return None

    def _build_driver(self, port: SerialPort) -> UM980Driver | None:
        if self.config.driver is None:
            return None
        return UM980Driver(port, self.config.driver.config)

    def _read_until_error(self, port: SerialPort, stop: threading.Event) -> bool:
        received = False
        while not stop.is_set():
            try:
                line = port.readline(MAX_SENTENCE_BYTES + 1)
            except (serial.SerialException, OSError) as exc:
                self.stats.read_errors += 1
                logger.warning("serial %s: read failed: %s", self.name, exc)
                return received
            if not line:
                continue
            received = True
            if len(line) > MAX_SENTENCE_BYTES and not line.endswith((b"\n", b"\r")):
                self.stats.oversized_lines += 1
                continue
            self.handle_line(line, t_mono_ns=time.monotonic_ns())
        return received


def _open_serial(config: SerialConfig) -> SerialPort:
    return serial.Serial(
        port=config.port,
        baudrate=config.baud,
        timeout=0.2,
        rtscts=True,
        dsrdtr=True,
    )


def _close(port: SerialPort, source_name: str) -> None:
    try:
        port.close()
    except (serial.SerialException, OSError) as exc:
        logger.warning("serial %s: close failed: %s", source_name, exc)
