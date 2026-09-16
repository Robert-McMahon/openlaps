"""Pit-side NTRIP client: caster bytes in, `rtcm.<vehicle>` core-NATS out.

Fire-and-forget by design (ADR 0006, `docs/WIRE_FORMAT.md` -> `rtcm.<vehicle>`):
correction bytes are published on core NATS, never JetStream, so a link
dropout simply drops whatever was in flight rather than buffering a
correction that would be stale by the time it could be redelivered. This
service does not retry a publish or hold bytes across a caster reconnect —
the next chunk from the caster is always more valuable than the last.

Two independent loops run concurrently: the caster connection (reconnects
with backoff, treats a read timeout as a dead caster) and, only when GGA
upstream is enabled, a live-only subscription to the vehicle's
`position.lat` / `position.lon` used to keep the caster fed with the
rover's approximate location. Losing the position feed does not stop
corrections flowing — GGA is best-effort on top of the main stream, not a
precondition for it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass

import nats
from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext, api

from pit.ntrip_client.gga import build_gga
from pit.ntrip_client.health import HealthState, serve_health
from pit.ntrip_client.ntrip import NtripAuthError, NtripCasterError, NtripConnection, connect
from pit.registry_cache import (
    MSG_TYPE_HEADER,
    MSG_TYPE_REGISTRY,
    REGISTRY_SOURCE_CLASS,
    RegistryCache,
)

logger = logging.getLogger(__name__)

POSITION_LAT_CHANNEL = "position.lat"
POSITION_LON_CHANNEL = "position.lon"

_CONNECT_BACKOFF_START_S = 1.0
_CONNECT_BACKOFF_MAX_S = 30.0
_AUTH_ERROR_RETRY_S = 30.0
_CASTER_CONNECT_TIMEOUT_S = 10.0
_REGISTRY_SCAN_TIMEOUT_S = 1.0
_REPORT_INTERVAL_S = 1.0


@dataclass(frozen=True, slots=True)
class NtripSettings:
    """Deploy-time wiring; credentials live only here, never on the vehicle."""

    nats_url: str
    vehicle_id: str
    host: str
    port: int
    mountpoint: str
    username: str
    password: str
    stream: str = "TELE_VEHICLE"
    creds_path: str | None = None
    enable_gga: bool = False
    gga_interval_s: float = 10.0
    idle_timeout_s: float = 30.0
    health_port: int = 8083

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> NtripSettings:
        """Build settings from the environment; see `example.env`."""
        env = os.environ if env is None else env
        vehicle_id = env.get("OPENLAPS_VEHICLE_ID", "").strip()
        if not vehicle_id:
            raise ValueError("OPENLAPS_VEHICLE_ID is required (see example.env)")
        host = env.get("NTRIP_HOST", "").strip()
        if not host:
            raise ValueError("NTRIP_HOST is required (see example.env)")
        mountpoint = env.get("NTRIP_MOUNTPOINT", "").strip()
        if not mountpoint:
            raise ValueError("NTRIP_MOUNTPOINT is required (see example.env)")
        stream = env.get("OPENLAPS_NTRIP_STREAM", "TELE_VEHICLE").strip()
        if not stream:
            raise ValueError("OPENLAPS_NTRIP_STREAM must name the pit's sourced stream")
        return cls(
            nats_url=env.get("OPENLAPS_NATS_URL", "nats://127.0.0.1:4222").strip(),
            vehicle_id=vehicle_id,
            host=host,
            port=int(env.get("NTRIP_PORT", "2101") or "2101"),
            mountpoint=mountpoint,
            username=env.get("NTRIP_USER", "").strip(),
            password=env.get("NTRIP_PASSWORD", "").strip(),
            stream=stream,
            creds_path=env.get("OPENLAPS_NATS_CREDS", "").strip() or None,
            enable_gga=env.get("NTRIP_ENABLE_GGA", "0").strip().lower() in ("1", "true", "yes"),
            gga_interval_s=float(env.get("NTRIP_GGA_INTERVAL_S", "10")),
            idle_timeout_s=float(env.get("NTRIP_IDLE_TIMEOUT_S", "30")),
            health_port=int(env.get("OPENLAPS_NTRIP_HEALTH_PORT", "8083")),
        )


class NtripService:
    """Owns the caster connection and, optionally, the GGA position feed."""

    def __init__(self, settings: NtripSettings) -> None:
        self.settings = settings
        self.health = HealthState(
            caster_host=f"{settings.host}:{settings.port}", mountpoint=settings.mountpoint
        )
        self._last_lat: float | None = None
        self._last_lon: float | None = None

    async def run(self, stop: asyncio.Event) -> None:
        client = await self._connect_nats(stop)
        if client is None:
            return
        report = asyncio.create_task(self._report_loop(stop))
        gga_task = (
            asyncio.create_task(self._track_position(client, stop))
            if self.settings.enable_gga
            else None
        )
        try:
            await self._caster_loop(client, stop)
        finally:
            report.cancel()
            if gga_task is not None:
                gga_task.cancel()
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - closing a dead client
                pass

    async def _connect_nats(self, stop: asyncio.Event) -> NatsClient | None:
        backoff = _CONNECT_BACKOFF_START_S
        options: dict[str, object] = {
            "servers": [self.settings.nats_url],
            "max_reconnect_attempts": -1,
            "reconnected_cb": self._on_reconnected,
        }
        if self.settings.creds_path:
            options["user_credentials"] = self.settings.creds_path
        while not stop.is_set():
            try:
                client = await nats.connect(**options)
            except Exception as exc:  # noqa: BLE001 - any connect failure retries
                logger.warning(
                    "ntrip-client: cannot connect to %s: %s", self.settings.nats_url, exc
                )
                await _sleep_unless(stop, backoff)
                backoff = min(backoff * 2, _CONNECT_BACKOFF_MAX_S)
                continue
            logger.info("ntrip-client: connected to %s", self.settings.nats_url)
            return client
        return None

    async def _on_reconnected(self) -> None:
        self.health.nats_reconnects += 1
        logger.info("ntrip-client: NATS reconnected")

    # -- caster ------------------------------------------------------------------

    async def _caster_loop(self, client: NatsClient, stop: asyncio.Event) -> None:
        subject = f"rtcm.{self.settings.vehicle_id}"
        backoff = _CONNECT_BACKOFF_START_S
        while not stop.is_set():
            try:
                connection = await connect(
                    self.settings.host,
                    self.settings.port,
                    self.settings.mountpoint,
                    self.settings.username,
                    self.settings.password,
                    connect_timeout_s=_CASTER_CONNECT_TIMEOUT_S,
                )
            except NtripAuthError as exc:
                # Wrong credentials won't fix themselves; back off hard
                # rather than hammering the caster in a tight retry loop.
                logger.error("ntrip-client: %s", exc)
                self.health.auth_errors += 1
                await _sleep_unless(stop, _AUTH_ERROR_RETRY_S)
                continue
            except (NtripCasterError, OSError, TimeoutError) as exc:
                logger.warning("ntrip-client: cannot reach caster: %s", exc)
                self.health.reconnects += 1
                await _sleep_unless(stop, backoff)
                backoff = min(backoff * 2, _CONNECT_BACKOFF_MAX_S)
                continue
            backoff = _CONNECT_BACKOFF_START_S
            logger.info(
                "ntrip-client: connected to %s:%d/%s",
                self.settings.host,
                self.settings.port,
                self.settings.mountpoint,
            )
            self.health.connected = True
            try:
                await self._stream(client, subject, connection, stop)
            finally:
                self.health.connected = False
                await connection.close()

    async def _stream(
        self, client: NatsClient, subject: str, connection: NtripConnection, stop: asyncio.Event
    ) -> None:
        gga_due = time.monotonic() + self.settings.gga_interval_s
        while not stop.is_set():
            chunk = await connection.read_chunk(self.settings.idle_timeout_s)
            if not chunk:
                logger.warning(
                    "ntrip-client: no data for %.0fs, treating caster as dead",
                    self.settings.idle_timeout_s,
                )
                return
            await client.publish(subject, chunk)
            self.health.observe_chunk(len(chunk))
            if not self.settings.enable_gga or time.monotonic() < gga_due:
                continue
            position = self._position()
            if position is not None:
                await connection.write_gga(build_gga(*position))
                self.health.gga_sends += 1
            gga_due = time.monotonic() + self.settings.gga_interval_s

    def _position(self) -> tuple[float, float] | None:
        if self._last_lat is None or self._last_lon is None:
            return None
        return self._last_lat, self._last_lon

    # -- GGA position feed --------------------------------------------------------

    async def _track_position(self, client: NatsClient, stop: asyncio.Event) -> None:
        js = client.jetstream()
        cache = RegistryCache()
        backoff = _CONNECT_BACKOFF_START_S
        while not stop.is_set():
            try:
                await self._scan_registry(js, cache, stop)
                subscription = await js.subscribe(
                    f"tele.{self.settings.vehicle_id}.>",
                    stream=self.settings.stream,
                    ordered_consumer=True,
                    deliver_policy=api.DeliverPolicy.NEW,
                )
            except Exception as exc:  # noqa: BLE001 - resubscribe on any failure
                logger.warning("ntrip-client: position feed subscribe failed: %s", exc)
                await _sleep_unless(stop, backoff)
                backoff = min(backoff * 2, _CONNECT_BACKOFF_MAX_S)
                continue
            backoff = _CONNECT_BACKOFF_START_S
            try:
                await self._consume_position(subscription, cache, stop)
            finally:
                try:
                    await subscription.unsubscribe()
                except Exception:  # noqa: BLE001 - it may already be gone
                    pass

    async def _scan_registry(
        self, js: JetStreamContext, cache: RegistryCache, stop: asyncio.Event
    ) -> None:
        """Drain the retained catalog so the first live batch decodes."""
        subject = f"tele.{self.settings.vehicle_id}.{REGISTRY_SOURCE_CLASS}"
        subscription = await js.subscribe(
            subject,
            stream=self.settings.stream,
            ordered_consumer=True,
            deliver_policy=api.DeliverPolicy.ALL,
        )
        try:
            while not stop.is_set():
                try:
                    message = await subscription.next_msg(timeout=_REGISTRY_SCAN_TIMEOUT_S)
                except TimeoutError:
                    break
                cache.add(message.data)
        finally:
            try:
                await subscription.unsubscribe()
            except Exception:  # noqa: BLE001 - the scan is done either way
                pass

    async def _consume_position(
        self,
        subscription: JetStreamContext.PushSubscription,
        cache: RegistryCache,
        stop: asyncio.Event,
    ) -> None:
        while not stop.is_set():
            try:
                message = await subscription.next_msg(timeout=1.0)
            except TimeoutError:
                continue
            header = (message.headers or {}).get(MSG_TYPE_HEADER)
            if header == MSG_TYPE_REGISTRY or message.subject.endswith(f".{REGISTRY_SOURCE_CLASS}"):
                cache.add(message.data)
                continue
            batch = cache.decode(message.data)
            if batch is None:
                continue
            for sample in batch.samples:
                if sample.channel.name == POSITION_LAT_CHANNEL:
                    self._last_lat = float(sample.value)
                elif sample.channel.name == POSITION_LON_CHANNEL:
                    self._last_lon = float(sample.value)

    # -- reporting -----------------------------------------------------------------

    async def _report_loop(self, stop: asyncio.Event) -> None:
        """Log the health line once a second."""
        try:
            while not stop.is_set():
                await asyncio.sleep(_REPORT_INTERVAL_S)
                self.health.roll()
                logger.info("ntrip-client: %s", self.health.log_line())
        except asyncio.CancelledError:
            return


async def _sleep_unless(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, but wake immediately on shutdown."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def run_service(settings: NtripSettings, stop: asyncio.Event) -> None:
    """Run one ntrip-client with its health endpoint until ``stop`` is set."""
    service = NtripService(settings)
    server = serve_health(service.health, settings.health_port)
    try:
        await service.run(stop)
    finally:
        server.shutdown()
