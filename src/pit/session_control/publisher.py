"""Latest-value JetStream publisher for ``cmd.<vehicle>.session``."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any

import nats
from nats.aio.client import Client as NatsClient

logger = logging.getLogger(__name__)

_CONNECT_BACKOFF_START_S = 0.5
_CONNECT_BACKOFF_MAX_S = 15.0
_PUBLISH_TIMEOUT_S = 5.0


class LatestSessionPublisher:
    """Retry publication while retaining only the newest desired state."""

    def __init__(
        self,
        nats_url: str,
        vehicle_id: str,
        *,
        domain: str | None = "veh",
        creds_path: str | None = None,
    ) -> None:
        self._nats_url = nats_url
        self._subject = f"cmd.{vehicle_id}.session"
        self._domain = domain
        self._creds_path = creds_path
        self._pending: bytes | None = None
        self._wake = asyncio.Event()
        self.connected = False
        self.published = 0
        self.reconnects = 0
        self.errors = 0

    @property
    def pending_count(self) -> int:
        return int(self._pending is not None)

    def pending_payload(self) -> bytes:
        return self._pending or b""

    def submit(self, payload: Mapping[str, object]) -> None:
        """Replace any queued retry with ``payload`` and wake the sender."""
        self._pending = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        self._wake.set()

    async def run(self, stop: asyncio.Event) -> None:
        backoff = _CONNECT_BACKOFF_START_S
        first_connection = True
        while not stop.is_set():
            client = await self._connect(stop)
            if client is None:
                return
            if not first_connection:
                self.reconnects += 1
            first_connection = False
            self.connected = True
            logger.info("session-control: connected to NATS at %s", self._nats_url)
            published_before = self.published
            try:
                failed = await self._publish_loop(client, stop)
            finally:
                self.connected = False
                try:
                    await client.close()
                except Exception:  # noqa: BLE001 - closing a failed connection
                    pass
            if failed and not stop.is_set():
                backoff = retry_backoff(backoff, made_progress=self.published > published_before)
                await _sleep_unless(stop, backoff)

    async def _connect(self, stop: asyncio.Event) -> NatsClient | None:
        backoff = _CONNECT_BACKOFF_START_S
        options: dict[str, Any] = {
            "servers": [self._nats_url],
            "connect_timeout": 2,
            "max_reconnect_attempts": 0,
        }
        if self._creds_path:
            options["user_credentials"] = self._creds_path
        while not stop.is_set():
            try:
                return await nats.connect(**options)
            except Exception as exc:  # noqa: BLE001 - all connection failures retry
                self.errors += 1
                logger.warning("session-control: cannot connect to NATS: %s", exc)
                await _sleep_unless(stop, backoff)
                backoff = min(backoff * 2, _CONNECT_BACKOFF_MAX_S)
        return None

    async def _publish_loop(self, client: NatsClient, stop: asyncio.Event) -> bool:
        js = client.jetstream(domain=self._domain) if self._domain else client.jetstream()
        while not stop.is_set():
            payload = self._pending
            if payload is None:
                self._wake.clear()
                if self._pending is not None:
                    continue
                await _wait_for_wake_or_stop(self._wake, stop)
                continue
            try:
                await js.publish(
                    self._subject,
                    payload,
                    timeout=_PUBLISH_TIMEOUT_S,
                )
            except Exception as exc:  # noqa: BLE001 - link failures reconnect and retry
                self.errors += 1
                logger.warning("session-control: session publish failed, retrying latest: %s", exc)
                return True
            if self._pending == payload:
                self._pending = None
            self.published += 1
            logger.info("session-control: published current session state")
        return False


async def _wait_for_wake_or_stop(wake: asyncio.Event, stop: asyncio.Event) -> None:
    wake_task = asyncio.create_task(wake.wait())
    stop_task = asyncio.create_task(stop.wait())
    done, pending = await asyncio.wait((wake_task, stop_task), return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        await task


async def _sleep_unless(stop: asyncio.Event, delay: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except TimeoutError:
        pass


def retry_backoff(current: float, *, made_progress: bool) -> float:
    """Advance bounded publish retry delay, resetting after a successful send."""
    if made_progress:
        return _CONNECT_BACKOFF_START_S
    return min(current * 2, _CONNECT_BACKOFF_MAX_S)
