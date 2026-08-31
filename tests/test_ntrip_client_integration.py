"""ntrip-client against a real nats-server: delivery, no JetStream capture,
and an end-to-end run through the real agent-side publisher and serial
write-back path.

Skipped automatically when docker is unavailable — see the ``nats_url``
fixture in conftest.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import nats
import pytest
from conftest import TEST_TELE_MAX_BYTES
from nats.js.errors import NotFoundError

from agent.publisher import JetStreamPublisher
from collectors.serial.transport import SerialCollector
from core.config import DriverConfig, SerialConfig, Um980Settings
from pit.ntrip_client.service import NtripService, NtripSettings

VEHICLE = "example-club-racer"
RTCM_CHUNKS = [b"\xd3\x00\x03aaa", b"\xd3\x00\x03bbb", b"\xd3\x00\x03ccc"]


class _FakeCaster:
    """Streams a fixed sequence of RTCM chunks once the handshake completes."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self._server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\n\r\n")
        for chunk in self.chunks:
            writer.write(chunk)
            await writer.drain()
            await asyncio.sleep(0.02)
        try:
            while await reader.read(4096):
                pass
        except (ConnectionError, OSError):
            pass


def _settings(nats_url: str, caster_port: int, **overrides) -> NtripSettings:
    defaults = {
        "nats_url": nats_url,
        "vehicle_id": VEHICLE,
        "host": "127.0.0.1",
        "port": caster_port,
        "mountpoint": "MOUNT1",
        "username": "alice",
        "password": "s3cret",
        "idle_timeout_s": 2.0,
        "health_port": 0,
    }
    return NtripSettings(**{**defaults, **overrides})


async def _run_until(
    settings: NtripSettings, predicate: Callable[[NtripService], bool], *, timeout_s: float = 15.0
) -> NtripService:
    service = NtripService(settings)
    stop = asyncio.Event()
    task = asyncio.create_task(service.run(stop))
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            if task.done() or predicate(service):
                break
            await asyncio.sleep(0.02)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10.0)
    return service


def test_forwards_caster_bytes_verbatim_in_order_with_no_jetstream_capture(nats_url):
    async def scenario():
        caster = _FakeCaster(RTCM_CHUNKS)
        await caster.start()
        try:
            client = await nats.connect(nats_url)
            collected: list[bytes] = []

            async def _on_message(msg):
                collected.append(msg.data)

            sub = await client.subscribe(f"rtcm.{VEHICLE}", cb=_on_message)
            try:
                settings = _settings(nats_url, caster.port)
                service = await _run_until(settings, lambda svc: len(collected) >= len(RTCM_CHUNKS))

                assert collected == RTCM_CHUNKS
                assert service.health.bytes_from_caster == sum(len(c) for c in RTCM_CHUNKS)
                assert service.health.publishes == len(RTCM_CHUNKS)

                js = client.jetstream()
                with pytest.raises(NotFoundError):
                    await js.find_stream_name_by_subject(f"rtcm.{VEHICLE}")
            finally:
                await sub.unsubscribe()
                await client.close()
        finally:
            await caster.stop()

    asyncio.run(scenario())


# --- end-to-end through the real agent write-back path -----------------------


class _FakeSerial:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def reset_input_buffer(self) -> None:
        pass

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        return len(data)

    def readline(self, size: int = -1) -> bytes:
        return b""

    def close(self) -> None:
        pass


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 15.0, message: str = "") -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for {message}")


def test_end_to_end_reaches_serial_write_back(nats_url):
    """Fake caster -> ntrip-client -> rtcm.<vehicle> -> real publisher -> real
    UM980 driver write-back, the exact path `docs/adr/0006` describes."""
    fake_serial = _FakeSerial()
    driver_settings = Um980Settings(rate_hz=1, sentences=["RMC"], configure_on_start=False)
    serial_config = SerialConfig(
        name="serial0",
        port="/dev/fake",
        baud=115200,
        decoder="nmea",
        driver=DriverConfig(name="um980", config=driver_settings),
    )
    collector = SerialCollector(
        serial_config, emit=lambda sample: None, serial_factory=lambda cfg: fake_serial
    )
    collector.start()
    try:
        _wait_until(lambda: collector.write_rtcm(b"\x00"), message="serial driver active")
        baseline = len(fake_serial.writes)

        publisher = JetStreamPublisher(
            nats_url=nats_url,
            vehicle_id=VEHICLE,
            registry_payload=b"",
            registry_interval_s=3600.0,
            on_rtcm=collector.write_rtcm,
            tele_max_bytes=TEST_TELE_MAX_BYTES,
        )
        publisher.start()
        try:
            _wait_until(lambda: publisher.connected, message="publisher connect")

            async def scenario():
                caster = _FakeCaster([b"\xd3\x00\x04e2e-frame"])
                await caster.start()
                try:
                    settings = _settings(nats_url, caster.port)
                    await _run_until(
                        settings, lambda svc: svc.health.publishes >= 1, timeout_s=10.0
                    )
                finally:
                    await caster.stop()

            asyncio.run(scenario())

            _wait_until(lambda: len(fake_serial.writes) > baseline, message="RTCM write-back")
            assert fake_serial.writes[-1] == b"\xd3\x00\x04e2e-frame"
        finally:
            publisher.stop()
    finally:
        collector.stop()
