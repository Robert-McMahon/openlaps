"""Sourced TELE consumer; bounded reorder buffer, one DB transaction per second."""

from __future__ import annotations

import asyncio
import copy
import heapq
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import aiomqtt
from google.protobuf.message import DecodeError
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.js import api

from pit.db.dsn import dsn_from_env
from pit.registry_cache import MSG_TYPE_HEADER, MSG_TYPE_REGISTRY, RegistryCache
from pit.watch.config import load_config
from pit.watch.engine import WatchEngine
from pit.watch.health import Health, serve_health
from pit.watch.store import WatchStore

log = logging.getLogger(__name__)


@dataclass
class Settings:
    config: Path
    vehicle: str
    dsn: str
    nats_url: str = "nats://127.0.0.1:4222"
    stream: str = "TELE_VEHICLE"
    nats_creds: str | None = None
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    health_port: int = 8090

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        vehicle = env.get("OPENLAPS_VEHICLE_ID", "").strip()
        if not vehicle:
            raise ValueError("OPENLAPS_VEHICLE_ID is required")
        return cls(
            config=Path(env.get("OPENLAPS_WATCH_CONFIG", f"profiles/{vehicle}/watch.yaml")),
            vehicle=vehicle,
            dsn=dsn_from_env(),
            nats_url=env.get("OPENLAPS_NATS_URL", "nats://127.0.0.1:4222"),
            stream=env.get("OPENLAPS_WATCH_STREAM", "TELE_VEHICLE"),
            nats_creds=env.get("OPENLAPS_NATS_CREDS") or None,
            mqtt_host=env.get("OPENLAPS_MQTT_HOST", "127.0.0.1"),
            mqtt_port=int(env.get("OPENLAPS_MQTT_PORT", "1883")),
            mqtt_username=env.get("OPENLAPS_MQTT_USERNAME") or None,
            mqtt_password=env.get("OPENLAPS_MQTT_PASSWORD") or None,
            health_port=int(env.get("OPENLAPS_WATCH_HEALTH_PORT", "8090")),
        )


class WatchService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.config = load_config(settings.config)
        self.engine = WatchEngine(self.config)
        self.store = WatchStore(settings.dsn)
        self.cache = RegistryCache()
        self.health = Health()
        self.session: str | None = None
        self.initialized = False
        self.pending: list[tuple] = []
        self.sequence = 0
        self.outbox: asyncio.Queue = asyncio.Queue(maxsize=2 * len(self.config.monitors))

    def handle(self, message: Msg) -> None:
        try:
            if (message.headers or {}).get(MSG_TYPE_HEADER) == MSG_TYPE_REGISTRY:
                self.cache.add(message.data)
                return
            batch = self.cache.decode(message.data)
            self.health.unknown_registry = self.cache.unknown_seq_batches
            self.health.bad_version = self.cache.bad_version_batches
            if batch is None:
                return
            for sample in batch.samples:
                if sample.channel.name not in self.engine.input_channels:
                    continue
                at = sample.capture_unix_ms / 1000
                # Bounded live consumer: radio backfill is archived by ingest-writer,
                # never learned as today's current operating condition.
                if at < time.time() - 30 or at > time.time() + 5:
                    self.health.dropped += 1
                    continue
                if len(self.pending) >= 50000:
                    self.health.dropped += 1
                    continue
                self.sequence += 1
                heapq.heappush(self.pending, (at, self.sequence, sample))
        except (DecodeError, ValueError, TypeError):
            self.health.malformed += 1

    def _context(self, at: int) -> None:
        session = self.store.session(self.settings.vehicle, at)
        if self.initialized and session == self.session:
            return
        engine = WatchEngine(self.config)
        models = self.store.load(self.settings.vehicle, session) if session else {}
        for name, monitor in engine.monitors.items():
            if name in models:
                monitor.restore(models[name])
            elif monitor.config.baseline == "stored":
                path = self.settings.config.parent / monitor.config.stored_file
                model = json.loads(path.read_text())
                monitor.restore(model)
                if not monitor.frozen:
                    raise ValueError(f"{path}: stored baseline is not frozen")
                monitor.score, monitor.finding = 0.0, None
            if monitor.config.baseline == "session_start" and session:
                monitor.source_session = session
        keep = [m.finding["finding_id"] for m in engine.monitors.values() if m.finding]
        self.store.close_previous(self.settings.vehicle, list(engine.monitors), at, keep)
        ticks = [m["last_tick"] for m in models.values() if m.get("last_tick") is not None]
        engine.last_tick = max(ticks) if ticks else None
        self.engine, self.session, self.initialized = engine, session, True

    async def tick(self, at: int) -> None:
        await asyncio.to_thread(self._context, at)
        while self.pending and self.pending[0][0] <= at:
            _, _, sample = heapq.heappop(self.pending)
            try:
                self.engine.observe(
                    sample.channel.name,
                    sample.value,
                    sample.capture_unix_ms / 1000,
                    sample.channel.units,
                )
            except (ValueError, TypeError):
                self.health.malformed += 1
        previous = copy.deepcopy(self.engine)
        rows = self.engine.tick(at, active=self.session is not None)
        if not rows:
            return
        models = {
            name: dict(m.snapshot(), last_tick=at) for name, m in self.engine.monitors.items()
        }
        try:
            await asyncio.to_thread(
                self.store.write_tick, self.settings.vehicle, self.session, rows, models
            )
        except Exception:
            self.engine = previous
            raise
        self.health.database_ok = True
        self.health.last_tick = at
        self.health.status = {name: r["baseline_status"] for name, r in rows.items()}
        for name, row in rows.items():
            if self.outbox.full():
                self.outbox.get_nowait()
            self.outbox.put_nowait((name, row))

    async def _ticks(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                # Two seconds permit the independent CAN source batches to align.
                await self.tick(int(time.time()) - 2)
            except Exception:
                self.health.database_ok = False
                self.store.close()
                log.exception("watch tick failed; baseline not advanced")
            try:
                await asyncio.wait_for(stop.wait(), 1)
            except TimeoutError:
                pass

    async def _mqtt(self, stop: asyncio.Event) -> None:
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
                        name, row = await self.outbox.get()
                        await client.publish(
                            f"openlaps/{self.settings.vehicle}/watch.{name}.score",
                            json.dumps(
                                dict(
                                    time=row["time"] * 1000,
                                    value=row["score"],
                                    status=row["baseline_status"],
                                    source="pit",
                                ),
                                allow_nan=False,
                            ),
                        )
            except Exception:
                self.health.mqtt_connected = False
                log.warning("watch MQTT disconnected; retrying")
                await asyncio.sleep(1)

    async def run(self, stop: asyncio.Event) -> None:
        server = serve_health(self.health, self.settings.health_port)
        client = NatsClient()
        tasks = []

        async def disconnected():
            self.health.nats_connected = False

        async def reconnected():
            self.health.nats_connected = True

        try:
            options = dict(
                servers=[self.settings.nats_url],
                max_reconnect_attempts=-1,
                disconnected_cb=disconnected,
                reconnected_cb=reconnected,
            )
            if self.settings.nats_creds:
                options["user_credentials"] = self.settings.nats_creds
            await client.connect(**options)
            self.health.nats_connected = True
            js = client.jetstream()
            # Subscribe first so telemetry arriving during registry hydration is
            # queued rather than falling into a startup gap.
            sub = await js.subscribe(
                f"tele.{self.settings.vehicle}.>",
                stream=self.settings.stream,
                ordered_consumer=True,
                deliver_policy=api.DeliverPolicy.NEW,
            )
            registries = await js.subscribe(
                f"tele.{self.settings.vehicle}.catalog",
                stream=self.settings.stream,
                ordered_consumer=True,
                deliver_policy=api.DeliverPolicy.ALL,
            )
            try:
                while True:
                    try:
                        self.handle(await registries.next_msg(timeout=0.5))
                    except TimeoutError:
                        break
            finally:
                await registries.unsubscribe()
            tasks = [asyncio.create_task(self._ticks(stop)), asyncio.create_task(self._mqtt(stop))]
            while not stop.is_set():
                try:
                    self.handle(await sub.next_msg(timeout=0.2))
                except TimeoutError:
                    pass
        finally:
            stop.set()
            # Let the DB worker complete its transaction before closing its
            # connection; cancelling to_thread would leave the worker running.
            for task in tasks[1:]:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.close()
            self.store.close()
            server.shutdown()
            server.server_close()
