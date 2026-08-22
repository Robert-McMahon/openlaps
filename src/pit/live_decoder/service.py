"""NATS-to-MQTT live telemetry bridge.

The service deliberately has no database dependency. It is a disposable,
live-only projection of selected telemetry: retained registries are recovered
first, then an ephemeral ordered `DeliverPolicy.NEW` consumer decodes batches
against their exact generation. MQTT handoff is non-blocking; when the broker
is down or its bounded queue is full, live updates are dropped and counted
instead of applying backpressure to NATS.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict, defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import aiomqtt
from google.protobuf.message import DecodeError
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.js import JetStreamContext, api

from core.pb import telemetry_pb2 as pb
from pit.live_decoder.config import LiveConfig
from pit.live_decoder.health import HealthState, serve_health
from pit.live_decoder.limiter import ConflatingLimiter, LiveUpdate
from pit.live_decoder.reload import ConfigReloader
from pit.registry_cache import (
    MSG_TYPE_HEADER,
    MSG_TYPE_REGISTRY,
    REGISTRY_SOURCE_CLASS,
    RegistryCache,
)

logger = logging.getLogger(__name__)

_CONNECT_BACKOFF_START_S = 0.5
_CONNECT_BACKOFF_MAX_S = 15.0
_REGISTRY_SCAN_TIMEOUT_S = 1.0
_CONSUME_POLL_S = 0.05
_REPORT_INTERVAL_S = 1.0
_IDLE_WARN_AFTER_S = 30.0
_IDLE_WARN_REPEAT_S = 60.0


def mqtt_message(vehicle: str, update: LiveUpdate) -> tuple[str, bytes]:
    """Format one non-retained QoS-0 MQTT publication."""
    topic = f"openlaps/{vehicle}/{update.channel}"
    payload = json.dumps(
        {"time": update.capture_unix_ms, "value": update.value},
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return topic, payload


@dataclass(frozen=True, slots=True)
class LiveDecoderSettings:
    """Deploy-time service wiring; channel policy stays in the YAML file."""

    config_path: Path
    nats_url: str
    stream: str = "TELE_VEHICLE"
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    nats_creds_path: str | None = None
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    health_port: int = 8082
    config_poll_s: float = 5.0
    mqtt_queue_size: int = 1_000
    # The pit-wide vehicle id, if the deployment sets one. Not the source of
    # truth -- the YAML's `vehicle:` key is, because it survives a reload --
    # but a cross-check against it, see `_check_vehicle_agreement`.
    vehicle_id: str | None = None

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        config_path: str | Path | None = None,
    ) -> LiveDecoderSettings:
        env = os.environ if env is None else env
        configured = config_path or env.get(
            "OPENLAPS_LIVE_CONFIG", "deploy/pit-config/live-decoder.yaml"
        )
        poll_s = float(env.get("OPENLAPS_LIVE_CONFIG_POLL_S", "5"))
        queue_size = int(env.get("OPENLAPS_LIVE_MQTT_QUEUE_SIZE", "1000"))
        port = int(env.get("OPENLAPS_MQTT_PORT", "1883"))
        health_port = int(env.get("OPENLAPS_LIVE_HEALTH_PORT", "8082"))
        stream = env.get("OPENLAPS_LIVE_STREAM", "TELE_VEHICLE").strip()
        if not stream:
            raise ValueError("OPENLAPS_LIVE_STREAM must name the pit's sourced stream")
        if poll_s <= 0:
            raise ValueError("OPENLAPS_LIVE_CONFIG_POLL_S must be greater than zero")
        if queue_size <= 0:
            raise ValueError("OPENLAPS_LIVE_MQTT_QUEUE_SIZE must be greater than zero")
        if not 1 <= port <= 65535:
            raise ValueError("OPENLAPS_MQTT_PORT must be between 1 and 65535")
        if not 0 <= health_port <= 65535:
            raise ValueError("OPENLAPS_LIVE_HEALTH_PORT must be between 0 and 65535")
        return cls(
            config_path=Path(configured),
            nats_url=env.get("OPENLAPS_NATS_URL", "nats://127.0.0.1:4222").strip(),
            stream=stream,
            nats_creds_path=env.get("OPENLAPS_NATS_CREDS", "").strip() or None,
            mqtt_host=env.get("OPENLAPS_MQTT_HOST", "127.0.0.1").strip(),
            mqtt_port=port,
            mqtt_username=env.get("OPENLAPS_MQTT_USERNAME", "").strip() or None,
            mqtt_password=env.get("OPENLAPS_MQTT_PASSWORD", "") or None,
            health_port=health_port,
            config_poll_s=poll_s,
            mqtt_queue_size=queue_size,
            vehicle_id=env.get("OPENLAPS_VEHICLE_ID", "").strip() or None,
        )


def _check_vehicle_agreement(settings: LiveDecoderSettings, config: LiveConfig) -> None:
    """Refuse to start subscribed to a vehicle that nothing publishes.

    The vehicle id reaches this service twice: through the pit-wide
    ``OPENLAPS_VEHICLE_ID`` that every other pit service reads directly, and
    through the ``vehicle:`` key of the live view YAML, which is the only one
    the subject filter is built from. Let those drift apart and the service is
    simultaneously healthy and useless -- it connects, subscribes to
    ``tele.<yaml-vehicle>.>`` against a stream carrying only
    ``tele.<env-vehicle>.>``, matches nothing, and reports zeroes for as long
    as you care to watch it. Nothing downstream can tell that apart from a car
    that is parked, so the disagreement has to be caught here, where the two
    values are side by side.
    """
    expected = settings.vehicle_id
    if expected is None or expected == config.vehicle:
        return
    raise ValueError(
        f"{settings.config_path}: vehicle {config.vehicle!r} disagrees with "
        f"OPENLAPS_VEHICLE_ID={expected!r}; this service would subscribe to "
        f"tele.{config.vehicle}.> and decode nothing. Make them match."
    )


class MqttSink:
    """Bounded non-blocking MQTT handoff with reconnect and drop accounting."""

    def __init__(self, settings: LiveDecoderSettings, vehicle: str, health: HealthState) -> None:
        self.settings = settings
        self.vehicle = vehicle
        self.health = health
        self.connected = asyncio.Event()
        self.suppressed: dict[str, int] = defaultdict(int)
        self._limited: OrderedDict[str, LiveUpdate] = OrderedDict()
        self._unlimited: deque[LiveUpdate] = deque()
        self._ready = asyncio.Event()

    @property
    def pending_count(self) -> int:
        return len(self._limited) + len(self._unlimited)

    def submit(self, update: LiveUpdate) -> None:
        """Hand off without waiting; unavailable MQTT means an intentional drop."""
        if not self.connected.is_set():
            self.health.mqtt_drops += 1
            self.suppressed[update.channel] += 1
            return
        if update.conflate and update.channel in self._limited:
            self._limited[update.channel] = update
            self.suppressed[update.channel] += 1
            self._ready.set()
            return
        if self.pending_count >= self.settings.mqtt_queue_size:
            self.health.mqtt_drops += 1
            self.suppressed[update.channel] += 1
            return
        if update.conflate:
            self._limited[update.channel] = update
        else:
            self._unlimited.append(update)
        self._ready.set()

    async def run(self, stop: asyncio.Event) -> None:
        backoff = _CONNECT_BACKOFF_START_S
        connected_once = False
        while not stop.is_set():
            try:
                async with aiomqtt.Client(
                    self.settings.mqtt_host,
                    self.settings.mqtt_port,
                    username=self.settings.mqtt_username,
                    password=self.settings.mqtt_password,
                ) as client:
                    if connected_once:
                        self.health.mqtt_reconnects += 1
                    connected_once = True
                    self.connected.set()
                    backoff = _CONNECT_BACKOFF_START_S
                    logger.info(
                        "live-decoder: connected to MQTT at %s:%d",
                        self.settings.mqtt_host,
                        self.settings.mqtt_port,
                    )
                    while not stop.is_set():
                        update = await self._next_pending(stop)
                        if update is None:
                            break
                        try:
                            topic, payload = mqtt_message(self.vehicle, update)
                        except (TypeError, ValueError) as exc:
                            self.health.invalid_values += 1
                            logger.warning(
                                "live-decoder: dropping non-JSON value for %s: %s",
                                update.channel,
                                exc,
                            )
                            continue
                        try:
                            await client.publish(topic, payload, qos=0, retain=False)
                        except Exception:
                            self.health.mqtt_drops += 1
                            raise
                        self.health.note_publish(update.channel)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - broker/network failures retry
                self.connected.clear()
                self._drop_queued()
                if not stop.is_set():
                    logger.warning("live-decoder: MQTT unavailable: %s", exc)
                    await _sleep_unless(stop, backoff)
                    backoff = min(backoff * 2, _CONNECT_BACKOFF_MAX_S)
        self.connected.clear()
        self._drop_queued()

    async def _next_pending(self, stop: asyncio.Event) -> LiveUpdate | None:
        while not stop.is_set():
            if self._unlimited:
                return self._unlimited.popleft()
            if self._limited:
                _, update = self._limited.popitem(last=False)
                return update
            self._ready.clear()
            try:
                await asyncio.wait_for(self._ready.wait(), timeout=0.25)
            except TimeoutError:
                pass
        return None

    def _drop_queued(self) -> None:
        updates = list(self._unlimited) + list(self._limited.values())
        self._unlimited.clear()
        self._limited.clear()
        for update in updates:
            self.health.mqtt_drops += 1
            self.suppressed[update.channel] += 1


class LiveDecoder:
    """Ephemeral ordered NATS consumer driving a conflating MQTT projection."""

    def __init__(self, settings: LiveDecoderSettings) -> None:
        self.settings = settings
        self.reloader = ConfigReloader(settings.config_path, settings.config_poll_s)
        self.config: LiveConfig = self.reloader.config
        _check_vehicle_agreement(settings, self.config)
        self.cache = RegistryCache()
        self.limiter = ConflatingLimiter(self.config)
        self.health = HealthState()
        self.health.config_mtime_ns = self.reloader.mtime_ns
        self.health.stream = settings.stream
        self.health.subject_filter = self.subject_filter
        self.sink = MqttSink(settings, self.config.vehicle, self.health)
        self.ready = asyncio.Event()
        self.messages_seen = 0
        self._last_registry: pb.ChannelRegistry | None = None

    @property
    def subject_filter(self) -> str:
        """The stream subject this service decodes. The vehicle cannot change."""
        return f"tele.{self.config.vehicle}.>"

    async def run(self, stop: asyncio.Event) -> None:
        health_server = serve_health(self.health, self.settings.health_port)
        sink_task = asyncio.create_task(self.sink.run(stop))
        report_task = asyncio.create_task(self._report_loop(stop))
        client: NatsClient | None = None
        try:
            client = await self._connect_nats(stop)
            if client is None:
                return
            js = client.jetstream()
            await self._scan_registries(js)
            subscription = await self._subscribe(js, self.subject_filter, api.DeliverPolicy.NEW)
            self.ready.set()
            await self._consume(subscription, stop)
        finally:
            self.ready.clear()
            stop.set()
            for task in (sink_task, report_task):
                task.cancel()
            await asyncio.gather(sink_task, report_task, return_exceptions=True)
            if client is not None:
                try:
                    await client.close()
                except Exception:  # noqa: BLE001 - closing a dead client
                    pass
            health_server.shutdown()
            health_server.server_close()

    async def _connect_nats(self, stop: asyncio.Event) -> NatsClient | None:
        backoff = _CONNECT_BACKOFF_START_S
        while not stop.is_set():
            client = NatsClient()
            try:
                # A finite initial attempt lets this outer loop apply its
                # bounded backoff and observe `stop`. Once connected, the
                # option is switched to unlimited so an outage reconnects.
                if self.settings.nats_creds_path:
                    await client.connect(
                        servers=[self.settings.nats_url],
                        max_reconnect_attempts=1,
                        reconnected_cb=self._on_nats_reconnected,
                        user_credentials=self.settings.nats_creds_path,
                    )
                else:
                    await client.connect(
                        servers=[self.settings.nats_url],
                        max_reconnect_attempts=1,
                        reconnected_cb=self._on_nats_reconnected,
                    )
            except Exception as exc:  # noqa: BLE001 - initial connection retries
                logger.warning("live-decoder: NATS unavailable: %s", exc)
                await _sleep_unless(stop, backoff)
                backoff = min(backoff * 2, _CONNECT_BACKOFF_MAX_S)
                continue
            client.options["max_reconnect_attempts"] = -1
            logger.info("live-decoder: connected to %s", self.settings.nats_url)
            return client
        return None

    async def _on_nats_reconnected(self) -> None:
        self.health.nats_reconnects += 1
        logger.info("live-decoder: NATS reconnected")

    async def _subscribe(
        self,
        js: JetStreamContext,
        subject: str,
        deliver_policy: api.DeliverPolicy,
    ) -> JetStreamContext.PushSubscription:
        """Subscribe against the *named* stream, saying so when it fails.

        The pit's ``TELE_VEHICLE`` deliberately declares no subjects (see
        deploy/provision_pit_streams.py), so nats-py's subject-to-stream
        lookup finds nothing and every subscription here must pass ``stream=``.
        When that lookup fails anyway the exception unwinds `run`, taking the
        health endpoint down with it -- but only after this line has named
        both halves of the lookup, so the log says which stream and which
        subject rather than leaving a bare NotFoundError to be guessed at.
        """
        try:
            return await js.subscribe(
                subject,
                stream=self.settings.stream,
                ordered_consumer=True,
                deliver_policy=deliver_policy,
            )
        except Exception as exc:
            logger.error(
                "live-decoder: cannot subscribe to %s on stream %r: %s",
                subject,
                self.settings.stream,
                exc,
            )
            raise

    async def _scan_registries(self, js: JetStreamContext) -> None:
        subject = f"tele.{self.config.vehicle}.{REGISTRY_SOURCE_CLASS}"
        subscription = await self._subscribe(js, subject, api.DeliverPolicy.ALL)
        try:
            while True:
                try:
                    message = await subscription.next_msg(timeout=_REGISTRY_SCAN_TIMEOUT_S)
                except TimeoutError:
                    break
                self.messages_seen += 1
                self._add_registry(message.data)
        finally:
            try:
                await subscription.unsubscribe()
            except Exception:  # noqa: BLE001 - recovery is complete either way
                pass
        logger.info(
            "live-decoder: registry scan found generations %s",
            self.cache.generations() or "none",
        )

    async def _consume(self, subscription: object, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                message = await subscription.next_msg(timeout=_CONSUME_POLL_S)
            except TimeoutError:
                message = None
            except Exception as exc:  # noqa: BLE001 - ordered consumer repairs itself
                logger.warning("live-decoder: NATS consume error: %s", exc)
                await _sleep_unless(stop, _CONNECT_BACKOFF_START_S)
                continue
            now = time.monotonic()
            if message is not None:
                self._handle_message(message, now)
            else:
                self._publish(self.limiter.admit(self.limiter.drain(now), now))
            self._reload(now)
            self._sync_limiter_health()

    def _handle_message(self, message: Msg, now: float) -> None:
        self.messages_seen += 1
        candidates: list[LiveUpdate] = []
        kind = (message.headers or {}).get(MSG_TYPE_HEADER, "batch")
        if kind == MSG_TYPE_REGISTRY:
            self._add_registry(message.data)
        else:
            try:
                decoded = self.cache.decode(message.data)
            except DecodeError as exc:
                self.health.malformed_payloads += 1
                logger.warning("live-decoder: dropping malformed protobuf batch: %s", exc)
            else:
                self.health.unknown_seq_batches = self.cache.unknown_seq_batches
                self.health.bad_version_batches = self.cache.bad_version_batches
                if decoded is not None:
                    for sample in decoded.samples:
                        candidates.extend(self.limiter.offer(sample, now))
        candidates.extend(self.limiter.drain(now))
        candidates = self.limiter.conflate_candidates(candidates)
        self._publish(self.limiter.admit(candidates, now))

    def _add_registry(self, payload: bytes) -> None:
        registry = pb.ChannelRegistry()
        try:
            registry.ParseFromString(payload)
        except DecodeError as exc:
            self.health.malformed_payloads += 1
            logger.warning("live-decoder: dropping malformed protobuf registry: %s", exc)
            return
        was_known = self.cache.known(registry.registry_seq)
        self.cache.add_registry(registry)
        self._last_registry = registry
        if not was_known:
            self.health.registries += 1
        self._refresh_unmatched_rules()

    def _reload(self, now: float) -> None:
        loaded = self.reloader.poll(now)
        if loaded is None:
            return
        if loaded.vehicle != self.config.vehicle:
            logger.error(
                "live-decoder: vehicle cannot change on reload (%s -> %s); restart required",
                self.config.vehicle,
                loaded.vehicle,
            )
            self.reloader.config = self.config
            return
        self.config = loaded
        self.limiter.reconfigure(loaded)
        self.health.config_reloads += 1
        self.health.config_mtime_ns = self.reloader.mtime_ns
        self._refresh_unmatched_rules()

    def _refresh_unmatched_rules(self) -> None:
        if self._last_registry is None:
            return
        unmatched = self.config.unmatched_rules(self._last_registry)
        self.health.unmatched_rules = unmatched
        for pattern in unmatched:
            logger.warning("live-decoder: config rule %r matches no registry channel", pattern)

    def _publish(self, updates: list[LiveUpdate]) -> None:
        for update in updates:
            self.sink.submit(update)

    def _sync_limiter_health(self) -> None:
        self.health.suppressed.clear()
        channels = set(self.limiter.suppressed) | set(self.sink.suppressed)
        self.health.suppressed.update(
            {
                channel: self.limiter.suppressed[channel] + self.sink.suppressed[channel]
                for channel in channels
            }
        )
        self.health.aggregate_sheds = self.limiter.aggregate_sheds

    async def _report_loop(self, stop: asyncio.Event) -> None:
        started = time.monotonic()
        next_idle_warning = _IDLE_WARN_AFTER_S
        while not stop.is_set():
            await _sleep_unless(stop, _REPORT_INTERVAL_S)
            self.health.roll()
            logger.info(
                "live-decoder: publish_rate=%.0f/s mqtt_drops=%d aggregate_sheds=%d",
                self.health.publish_rate,
                self.health.mqtt_drops,
                self.health.aggregate_sheds,
            )
            if self.messages_seen:
                continue
            # A publish rate of zero reads the same whether the car is
            # parked or the subscription is aimed at a subject nobody
            # publishes. Having never seen a single message narrows it, and
            # naming the subject makes the second case answerable from one
            # line of log.
            idle = time.monotonic() - started
            if idle >= next_idle_warning:
                next_idle_warning = idle + _IDLE_WARN_REPEAT_S
                logger.warning(
                    "live-decoder: no message in %.0fs on %s of stream %r; "
                    "check the vehicle id and that the stream is being sourced",
                    idle,
                    self.subject_filter,
                    self.settings.stream,
                )


async def _sleep_unless(stop: asyncio.Event, delay: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except TimeoutError:
        pass


async def run_live_decoder(settings: LiveDecoderSettings, stop: asyncio.Event) -> None:
    """Construct and run one live-decoder service."""
    await LiveDecoder(settings).run(stop)


__all__ = [
    "LiveDecoder",
    "LiveDecoderSettings",
    "LiveUpdate",
    "MqttSink",
    "mqtt_message",
    "run_live_decoder",
]
