"""NATS-to-MQTT live telemetry bridge.

The service deliberately has no database dependency. It is a disposable,
live-only projection of selected telemetry: retained registries are recovered
first, then an ephemeral ordered `DeliverPolicy.NEW` consumer decodes batches
against their exact generation. MQTT handoff is non-blocking; when the broker
is down or its bounded queue is full, live updates are dropped and counted
instead of applying backpressure to NATS.

Where the live consumer sits is a deployment choice (``OPENLAPS_LIVE_SOURCE``):

- ``pit`` (default): on the pit's sourced copy of the vehicle stream. Simple,
  local, and strictly in order -- which after a radio dropout means the
  gauges show the backlog replaying at link speed and reach "now" only once
  the whole gap has crossed the radio.
- ``vehicle``: on the vehicle's own stream, reached through the leafnode via
  the vehicle's JetStream domain. ``DeliverPolicy.NEW`` then means *new at
  the car*, so the moment the link is back the gauges show current values
  while the sourced copy catches up behind them. The price is that every
  batch crosses the radio twice while the link is healthy (once for the
  sourced copy, once for this consumer); the registries are still scanned
  from the pit's complete local copy, and a consumer that has gone quiet for
  ``resubscribe_after_s`` is replaced with a fresh ``NEW`` one rather than
  left to nats-py's by-sequence recovery, which would replay the gap.
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
from pit.timing_display import display_for

logger = logging.getLogger(__name__)

_CONNECT_BACKOFF_START_S = 0.5
_CONNECT_BACKOFF_MAX_S = 15.0
_REGISTRY_SCAN_TIMEOUT_S = 1.0
_CONSUME_POLL_S = 0.05
_REPORT_INTERVAL_S = 1.0
_IDLE_WARN_AFTER_S = 30.0
_IDLE_WARN_REPEAT_S = 60.0
# JetStream API calls made across the leafnode (vehicle source mode) get a
# bounded wait: when the radio is down they fail and the service retries,
# rather than hanging the consume loop on a request nothing will answer.
_JS_API_TIMEOUT_S = 5.0
# A batch naming a registry generation this service has never seen means a
# registry message went past while no live consumer was attached (the agent
# restarted during a dropout, say). The pit's sourced copy has it; re-scan
# there, but no more often than this.
_REGISTRY_RESCAN_MIN_S = 5.0
LIVE_SOURCES = ("pit", "vehicle")


def mqtt_message(vehicle: str, update: LiveUpdate) -> tuple[str, bytes]:
    """Format one non-retained QoS-0 MQTT publication."""
    topic = f"openlaps/{vehicle}/{update.channel}"
    document: dict[str, object] = {"time": update.capture_unix_ms, "value": update.value}
    display = display_for(update.channel, update.value)
    if display is not None:
        document["display"] = display
    payload = json.dumps(
        document,
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
    # The pit-wide vehicle id. This is the source of truth and is
    # normally the *only* one set -- the YAML's `vehicle:` key is an optional
    # override, see `_resolve_vehicle`.
    vehicle_id: str | None = None
    # Which stream the live consumer reads: the pit's sourced copy ("pit") or
    # the vehicle's own stream across the leafnode ("vehicle"). See the
    # module docstring for the trade.
    live_source: str = "pit"
    # Vehicle-source wiring: the vehicle's JetStream domain and stream name,
    # the same two values provision_pit_streams.py sources from.
    vehicle_domain: str = "veh"
    vehicle_stream: str = "TELE"
    # Vehicle source only: a live consumer that has delivered nothing for this
    # long is dropped and re-created as DeliverPolicy.NEW. The car publishes
    # at least once a second while it is on, so silence this long means the
    # link was down (or the consumer died with it); a fresh consumer resumes
    # at "now" instead of replaying the gap.
    resubscribe_after_s: float = 10.0

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
        live_source = env.get("OPENLAPS_LIVE_SOURCE", "pit").strip().lower() or "pit"
        if live_source not in LIVE_SOURCES:
            raise ValueError(
                f"OPENLAPS_LIVE_SOURCE must be one of {', '.join(LIVE_SOURCES)}, "
                f"not {live_source!r}"
            )
        vehicle_domain = env.get("OPENLAPS_VEHICLE_JS_DOMAIN", "veh").strip()
        vehicle_stream = env.get("OPENLAPS_VEHICLE_STREAM", "TELE").strip()
        if live_source == "vehicle" and not (vehicle_domain and vehicle_stream):
            raise ValueError(
                "OPENLAPS_LIVE_SOURCE=vehicle needs OPENLAPS_VEHICLE_JS_DOMAIN and "
                "OPENLAPS_VEHICLE_STREAM"
            )
        resubscribe_after_s = float(env.get("OPENLAPS_LIVE_RESUBSCRIBE_S", "10"))
        if resubscribe_after_s <= 0:
            raise ValueError("OPENLAPS_LIVE_RESUBSCRIBE_S must be greater than zero")
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
            live_source=live_source,
            vehicle_domain=vehicle_domain,
            vehicle_stream=vehicle_stream,
            resubscribe_after_s=resubscribe_after_s,
        )

    @property
    def live_stream(self) -> str:
        """The stream the live consumer is created on."""
        return self.vehicle_stream if self.live_source == "vehicle" else self.stream


def _resolve_vehicle(settings: LiveDecoderSettings, config: LiveConfig) -> str:
    """The car this service decodes, and refuse to start if it is ambiguous.

    Normally the YAML names no vehicle and this returns OPENLAPS_VEHICLE_ID,
    the one place the pit names its car. The rest of this docstring is about
    the case where the YAML does name one, which is an override.

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
    if config.vehicle is None:
        if expected is None:
            raise ValueError(
                "no vehicle id: set OPENLAPS_VEHICLE_ID, or name one in "
                f"{settings.config_path}. Without it this service has no "
                "subject to subscribe to."
            )
        return expected
    if expected is None or expected == config.vehicle:
        return config.vehicle
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
        # Resolved once: the subject filter and the MQTT topic prefix are both
        # built from it, and a reload must not be able to move either.
        self.vehicle: str = _resolve_vehicle(settings, self.config)
        self.cache = RegistryCache()
        self.limiter = ConflatingLimiter(self.config)
        self.health = HealthState()
        self.health.config_mtime_ns = self.reloader.mtime_ns
        self.health.stream = settings.stream
        self.health.live_source = settings.live_source
        self.health.live_stream = settings.live_stream
        self.health.subject_filter = self.subject_filter
        self.sink = MqttSink(settings, self.vehicle, self.health)
        self.ready = asyncio.Event()
        self.messages_seen = 0
        self._last_registry: pb.ChannelRegistry | None = None

    @property
    def subject_filter(self) -> str:
        """The stream subject this service decodes. The vehicle cannot change."""
        return f"tele.{self.vehicle}.>"

    async def run(self, stop: asyncio.Event) -> None:
        health_server = serve_health(self.health, self.settings.health_port)
        sink_task = asyncio.create_task(self.sink.run(stop))
        report_task = asyncio.create_task(self._report_loop(stop))
        client: NatsClient | None = None
        try:
            client = await self._connect_nats(stop)
            if client is None:
                return
            # Registries always come from the pit's complete local copy; a
            # vehicle-sourced live consumer only has to carry what is new.
            js = client.jetstream()
            await self._scan_registries(js)
            if self.settings.live_source == "vehicle":
                js_live = client.jetstream(
                    domain=self.settings.vehicle_domain, timeout=_JS_API_TIMEOUT_S
                )
            else:
                js_live = js
            subscription = await self._subscribe_live(js_live, stop)
            if subscription is None:
                return
            self.ready.set()
            await self._consume(subscription, stop, js_live, js_registry=js)
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
        *,
        stream: str | None = None,
        idle_heartbeat: float | None = None,
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
        stream = self.settings.stream if stream is None else stream
        options: dict[str, object] = {}
        if idle_heartbeat is not None:
            options["idle_heartbeat"] = idle_heartbeat
        try:
            return await js.subscribe(
                subject,
                stream=stream,
                ordered_consumer=True,
                deliver_policy=deliver_policy,
                **options,
            )
        except Exception as exc:
            logger.error(
                "live-decoder: cannot subscribe to %s on stream %r: %s",
                subject,
                stream,
                exc,
            )
            raise

    async def _subscribe_live(
        self, js: JetStreamContext, stop: asyncio.Event
    ) -> JetStreamContext.PushSubscription | None:
        """The ``DeliverPolicy.NEW`` consumer the gauges are driven from.

        On the pit stream a failure is a wiring error and unwinds `run` as it
        always has. On the vehicle stream the request crosses the radio, so a
        failure usually just means the link is down: keep trying with the
        connection backoff until it is back or `stop` is set. Nothing is lost
        meanwhile -- there is no live data to show while the link is down,
        and the registries are already in hand.

        The idle heartbeat in vehicle mode is set to ``resubscribe_after_s``:
        nats-py's ordered consumer re-creates itself *by sequence* after two
        missed heartbeats, which would replay the gap, so our own fresh-NEW
        replacement (`_consume`) is given the first chance at exactly that
        age.
        """
        if self.settings.live_source != "vehicle":
            return await self._subscribe(js, self.subject_filter, api.DeliverPolicy.NEW)
        backoff = _CONNECT_BACKOFF_START_S
        while not stop.is_set():
            try:
                return await self._subscribe(
                    js,
                    self.subject_filter,
                    api.DeliverPolicy.NEW,
                    stream=self.settings.vehicle_stream,
                    idle_heartbeat=self.settings.resubscribe_after_s,
                )
            except Exception:  # noqa: BLE001 - already logged by _subscribe
                self.health.live_subscribe_failures += 1
                await _sleep_unless(stop, backoff)
                backoff = min(backoff * 2, _CONNECT_BACKOFF_MAX_S)
        return None

    async def _scan_registries(self, js: JetStreamContext) -> None:
        subject = f"tele.{self.vehicle}.{REGISTRY_SOURCE_CLASS}"
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

    async def _consume(
        self,
        subscription: object,
        stop: asyncio.Event,
        js_live: JetStreamContext | None = None,
        js_registry: JetStreamContext | None = None,
    ) -> None:
        last_message = time.monotonic()
        self.health.last_message_mono = last_message
        unknown_seen = self.cache.unknown_seq_batches
        last_rescan = float("-inf")
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
                last_message = now
                self.health.last_message_mono = now
                self._handle_message(message, now)
                if (
                    js_registry is not None
                    and self.cache.unknown_seq_batches > unknown_seen
                    and now - last_rescan >= _REGISTRY_RESCAN_MIN_S
                ):
                    # A DeliverPolicy.NEW consumer on the vehicle stream never
                    # sees a registry published while it was not attached;
                    # the sourced copy keeps every generation.
                    unknown_seen = self.cache.unknown_seq_batches
                    last_rescan = now
                    logger.info(
                        "live-decoder: batch names an unknown registry generation; "
                        "re-scanning registries on %s",
                        self.settings.stream,
                    )
                    try:
                        await self._scan_registries(js_registry)
                    except Exception as exc:  # noqa: BLE001 - try again next period
                        logger.warning("live-decoder: registry re-scan failed: %s", exc)
            else:
                self._publish(self.limiter.admit(self.limiter.drain(now), now))
                if (
                    js_live is not None
                    and self.settings.live_source == "vehicle"
                    and now - last_message >= self.settings.resubscribe_after_s
                ):
                    replacement = await self._resubscribe_live(subscription, js_live, stop)
                    if replacement is None:
                        return
                    subscription = replacement
                    # Measured from the new consumer's creation, so a link
                    # that stays down costs one attempt per period, not one
                    # per poll.
                    last_message = time.monotonic()
            self._reload(now)
            self._sync_limiter_health()

    async def _resubscribe_live(
        self,
        subscription: object,
        js_live: JetStreamContext,
        stop: asyncio.Event,
    ) -> JetStreamContext.PushSubscription | None:
        """Replace a quiet vehicle-stream consumer with a fresh NEW one.

        Quiet means no batch for `resubscribe_after_s`. Either the link went
        away and took the consumer with it, or the link is fine and the car
        really is silent; a fresh consumer is the right answer to both, and
        cheap in the second case. What it must never do is resume where the
        old one stopped: that is the backlog, and the sourced stream already
        carries it.
        """
        self.health.live_resubscribes += 1
        logger.info(
            "live-decoder: no batch for %.0fs on %s; re-creating the live consumer",
            self.settings.resubscribe_after_s,
            self.settings.live_stream,
        )
        try:
            await subscription.unsubscribe()
        except Exception:  # noqa: BLE001 - the old consumer is being discarded
            pass
        return await self._subscribe_live(js_live, stop)

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
        if loaded.vehicle is not None and loaded.vehicle != self.vehicle:
            logger.error(
                "live-decoder: vehicle cannot change on reload (%s -> %s); restart required",
                self.vehicle,
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
                    self.settings.live_stream,
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
    "LIVE_SOURCES",
    "LiveDecoder",
    "LiveDecoderSettings",
    "LiveUpdate",
    "MqttSink",
    "mqtt_message",
    "run_live_decoder",
]
