"""NATS-to-MQTT pit timing extrapolation service."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import aiomqtt
from google.protobuf.message import DecodeError
from nats.aio.client import Client as NatsClient
from nats.js import api

from core.pb import telemetry_pb2 as pb
from pit.registry_cache import (
    MSG_TYPE_HEADER,
    MSG_TYPE_REGISTRY,
    REGISTRY_SOURCE_CLASS,
    RegistryCache,
)
from pit.timing_display import pit_clock_display
from pit.timing_extrapolator.config import TimingConfig, load_config
from pit.timing_extrapolator.engine import ClockPolicy, ClockValue, TimingExtrapolator
from pit.timing_extrapolator.health import HealthState, serve_health

logger = logging.getLogger(__name__)
_INPUT_CHANNELS = frozenset(
    {
        "lap.event",
        "lap.best_time",
        "lap.sector",
        "sys.host.clock_offset_s",
        "sys.host.clock_stratum",
        "sys.host.clock_source",
    }
)


@dataclass(frozen=True, slots=True)
class TimingServiceSettings:
    config_path: Path
    nats_url: str
    stream: str = "TELE_VEHICLE"
    nats_creds_path: str | None = None
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    health_port: int = 8084
    vehicle_id: str | None = None

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, config_path: str | Path | None = None
    ) -> TimingServiceSettings:
        env = os.environ if env is None else env
        stream = env.get("OPENLAPS_TIMING_STREAM", "TELE_VEHICLE").strip()
        if not stream:
            raise ValueError("OPENLAPS_TIMING_STREAM must name the pit sourced stream")
        mqtt_port = int(env.get("OPENLAPS_MQTT_PORT", "1883"))
        health_port = int(env.get("OPENLAPS_TIMING_HEALTH_PORT", "8084"))
        if not 1 <= mqtt_port <= 65535:
            raise ValueError("OPENLAPS_MQTT_PORT must be between 1 and 65535")
        if not 0 <= health_port <= 65535:
            raise ValueError("OPENLAPS_TIMING_HEALTH_PORT must be between 0 and 65535")
        return cls(
            config_path=Path(
                config_path
                or env.get("OPENLAPS_TIMING_CONFIG", "deploy/pit-config/timing-extrapolator.yaml")
            ),
            nats_url=env.get("OPENLAPS_NATS_URL", "nats://127.0.0.1:4222").strip(),
            stream=stream,
            nats_creds_path=env.get("OPENLAPS_NATS_CREDS", "").strip() or None,
            mqtt_host=env.get("OPENLAPS_MQTT_HOST", "127.0.0.1").strip(),
            mqtt_port=mqtt_port,
            mqtt_username=env.get("OPENLAPS_MQTT_USERNAME", "").strip() or None,
            mqtt_password=env.get("OPENLAPS_MQTT_PASSWORD", "") or None,
            health_port=health_port,
            vehicle_id=env.get("OPENLAPS_VEHICLE_ID", "").strip() or None,
        )


def mqtt_message(
    vehicle: str,
    channel: str,
    now_s: float,
    value: float | None,
    status: str,
    reason: str | None,
) -> tuple[str, bytes]:
    if channel not in {"timing.lap_elapsed_pit", "timing.sector_elapsed_pit"}:
        raise ValueError(f"refusing to publish non-pit timing channel {channel!r}")
    document: dict[str, object] = {
        "time": round(now_s * 1000),
        "value": value,
        "display": pit_clock_display(value, status),
        "status": status,
        "source": "pit",
    }
    if reason is not None:
        document["reason"] = reason
    return (
        f"openlaps/{vehicle}/{channel}",
        json.dumps(document, separators=(",", ":"), allow_nan=False).encode(),
    )


class TimingService:
    """Ephemeral telemetry consumer whose only durable output is live MQTT."""

    def __init__(self, settings: TimingServiceSettings) -> None:
        self.settings = settings
        self.config: TimingConfig = load_config(settings.config_path)
        if settings.vehicle_id is not None and settings.vehicle_id != self.config.vehicle:
            raise ValueError(
                f"{settings.config_path}: vehicle {self.config.vehicle!r} disagrees with "
                f"OPENLAPS_VEHICLE_ID={settings.vehicle_id!r}"
            )
        self.engine = TimingExtrapolator(
            ClockPolicy(
                fallback_max_lap_s=self.config.fallback_max_lap_s,
                max_clock_offset_s=self.config.max_clock_offset_s,
                max_clock_stratum=self.config.max_clock_stratum,
                allowed_clock_sources=tuple(
                    source.upper() for source in self.config.allowed_clock_sources
                ),
                best_lap_multiple=self.config.best_lap_multiple,
            )
        )
        self.cache = RegistryCache()
        self.health = HealthState(settings.stream, self.subject_filter)
        self.ready = asyncio.Event()
        self._outbox: asyncio.Queue[tuple[str, ClockValue, float]] = asyncio.Queue(maxsize=2)
        self.publish = self._queue_publish

    @property
    def subject_filter(self) -> str:
        return f"tele.{self.config.vehicle}.>"

    def _queue_publish(self, item: tuple[str, ClockValue, float]) -> None:
        channel = item[0]
        if channel not in self.config.output_channels:
            raise ValueError(f"refusing non-pit output {channel}")
        if self._outbox.full():
            retained = []
            while not self._outbox.empty():
                retained.append(self._outbox.get_nowait())
            by_channel = {queued[0]: queued for queued in retained}
            by_channel[channel] = item
            for queued in by_channel.values():
                if not self._outbox.full():
                    self._outbox.put_nowait(queued)
            self.health.mqtt_drops += 1
            return
        self._outbox.put_nowait(item)

    def _emit(self, values: dict[str, ClockValue], now_s: float) -> None:
        for channel, value in values.items():
            self.publish((channel, value, now_s))

    def _publish_tick(self, now_s: float) -> None:
        self._emit(self.engine.tick(now_s), now_s)

    def _handle_message(self, message: object) -> None:
        kind = (getattr(message, "headers", None) or {}).get(MSG_TYPE_HEADER, "batch")
        payload = message.data
        if kind == MSG_TYPE_REGISTRY:
            registry = pb.ChannelRegistry()
            try:
                registry.ParseFromString(payload)
            except DecodeError:
                self.health.malformed_payloads += 1
                return
            self.cache.add_registry(registry)
            self.health.registries += 1
            return
        try:
            decoded = self.cache.decode(payload)
        except DecodeError:
            self.health.malformed_payloads += 1
            return
        self.health.unknown_seq_batches = self.cache.unknown_seq_batches
        self.health.bad_version_batches = self.cache.bad_version_batches
        if decoded is None:
            return
        for sample in decoded.samples:
            if sample.channel.name not in _INPUT_CHANNELS:
                continue
            try:
                values = self.engine.observe(sample.channel.name, sample.value)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, OverflowError):
                self.health.malformed_events += 1
                continue
            self._emit(values, sample.capture_unix_ms / 1000.0)

    async def run(self, stop: asyncio.Event) -> None:
        server = serve_health(self.health, self.settings.health_port)
        mqtt_task = asyncio.create_task(self._mqtt_loop(stop))
        tick_task = asyncio.create_task(self._tick_loop(stop))
        client: NatsClient | None = None
        try:
            client = NatsClient()
            options: dict[str, object] = {
                "servers": [self.settings.nats_url],
                "max_reconnect_attempts": -1,
                "reconnected_cb": self._on_nats_reconnected,
            }
            if self.settings.nats_creds_path:
                options["user_credentials"] = self.settings.nats_creds_path
            await client.connect(**options)
            js = client.jetstream()
            await self._scan_registries(js)
            subscription = await js.subscribe(
                self.subject_filter,
                stream=self.settings.stream,
                ordered_consumer=True,
                deliver_policy=api.DeliverPolicy.NEW,
            )
            self.ready.set()
            while not stop.is_set():
                try:
                    message = await subscription.next_msg(timeout=0.1)
                except TimeoutError:
                    continue
                self._handle_message(message)
        finally:
            self.ready.clear()
            stop.set()
            for task in (mqtt_task, tick_task):
                task.cancel()
            await asyncio.gather(mqtt_task, tick_task, return_exceptions=True)
            if client is not None:
                await client.close()
            server.shutdown()
            server.server_close()

    async def _scan_registries(self, js: object) -> None:
        subscription = await js.subscribe(
            f"tele.{self.config.vehicle}.{REGISTRY_SOURCE_CLASS}",
            stream=self.settings.stream,
            ordered_consumer=True,
            deliver_policy=api.DeliverPolicy.ALL,
        )
        try:
            while True:
                try:
                    message = await subscription.next_msg(timeout=1.0)
                except TimeoutError:
                    break
                self._handle_message(message)
        finally:
            await subscription.unsubscribe()

    async def _tick_loop(self, stop: asyncio.Event) -> None:
        interval = 1.0 / self.config.publish_hz
        while not stop.is_set():
            self._publish_tick(time.time())
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def _mqtt_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                async with aiomqtt.Client(
                    self.settings.mqtt_host,
                    self.settings.mqtt_port,
                    username=self.settings.mqtt_username,
                    password=self.settings.mqtt_password,
                ) as client:
                    self.health.mqtt_connected = True
                    while not stop.is_set():
                        try:
                            channel, value, at = await asyncio.wait_for(
                                self._outbox.get(), timeout=0.25
                            )
                        except TimeoutError:
                            continue
                        topic, payload = mqtt_message(
                            self.config.vehicle,
                            channel,
                            at,
                            value.value,
                            value.status,
                            value.reason,
                        )
                        await client.publish(topic, payload, qos=0, retain=False)
                        self.health.note_publish(channel, value)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - broker outages retry forever
                self.health.mqtt_connected = False
                self.health.mqtt_reconnects += 1
                logger.warning("timing-extrapolator: MQTT unavailable: %s", exc)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
                except TimeoutError:
                    pass
        self.health.mqtt_connected = False

    async def _on_nats_reconnected(self) -> None:
        self.health.nats_reconnects += 1


__all__ = ["TimingService", "TimingServiceSettings", "mqtt_message"]
