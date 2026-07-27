"""Async JetStream publisher with a bounded window and shed-visibly buffering.

``docs/AGENT_DESIGN.md`` -> Sample lifecycle step 6 and Failure modes: the
publisher runs an asyncio loop on its own thread, publishes batches to
``tele.<vehicle>.<source-class>`` with a bounded unacked window, and — when
the local NATS is down or wedged — buffers up to a byte budget then drops
whole batches oldest-first, counting them. Capture is never blocked by
storage being unavailable, and the agent never exits because the broker is
rebooting.

It also owns every other NATS-facing concern of the agent: idempotent
TELE/CMD stream provisioning, registry publish on start plus a slow
republish interval, ``cmd.<vehicle>.session`` consumption (last-value), and
the ``rtcm.<vehicle>`` core-NATS subscription forwarded to the GNSS driver
(fire-and-forget by design — a stale correction is worse than none, see
``docs/WIRE_FORMAT.md``).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import nats
import nats.errors
import nats.js.errors
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.js import JetStreamContext, api

from agent.pipeline import TickBatch

logger = logging.getLogger(__name__)

MSG_TYPE_HEADER = "Openlaps-Msg-Type"
MSG_TYPE_REGISTRY = "registry"
MSG_TYPE_BATCH = "batch"
REGISTRY_SOURCE_CLASS = "catalog"

DEFAULT_WINDOW = 1_000
DEFAULT_BUFFER_BYTES = 32 * 1024 * 1024
DEFAULT_TELE_MAX_AGE_S = 72 * 3600
DEFAULT_TELE_MAX_BYTES = 8 * 1024 * 1024 * 1024
DEFAULT_REGISTRY_INTERVAL_S = 300.0
_PUBLISH_TIMEOUT_S = 5.0
_RETRY_BACKOFF_S = 1.0
_CONNECT_BACKOFF_START_S = 0.5
_CONNECT_BACKOFF_MAX_S = 15.0

PayloadHandler = Callable[[bytes], None]


@dataclass(slots=True)
class _QueuedBatch:
    subject: str
    payload: bytes
    msg_id: str
    submit_mono_ns: int


class JetStreamPublisher:
    """Publish telemetry to the vehicle's local JetStream from its own thread."""

    def __init__(
        self,
        *,
        nats_url: str,
        vehicle_id: str,
        registry_payload: bytes,
        creds_path: str | None = None,
        window: int = DEFAULT_WINDOW,
        buffer_bytes: int = DEFAULT_BUFFER_BYTES,
        tele_max_age_s: float = DEFAULT_TELE_MAX_AGE_S,
        tele_max_bytes: int = DEFAULT_TELE_MAX_BYTES,
        registry_interval_s: float = DEFAULT_REGISTRY_INTERVAL_S,
        on_session: PayloadHandler | None = None,
        on_rtcm: PayloadHandler | None = None,
    ) -> None:
        if window <= 0:
            raise ValueError("window must be positive")
        self._nats_url = nats_url
        self._vehicle_id = vehicle_id
        self._registry_payload = registry_payload
        self._creds_path = creds_path
        self._window = window
        self._buffer_budget = buffer_bytes
        self._tele_max_age_s = tele_max_age_s
        self._tele_max_bytes = tele_max_bytes
        self._registry_interval_s = registry_interval_s
        self._on_session = on_session
        self._on_rtcm = on_rtcm

        self._lock = threading.Lock()
        self._buffer: deque[_QueuedBatch] = deque()
        self._buffer_bytes = 0
        self._oldest_pending_mono_ns: int | None = None
        self._publish_drops = 0
        self._published = 0
        self._registry_publishes = 0
        self._connected = False

        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    # -- thread-safe surface --------------------------------------------------

    def start(self) -> None:
        """Run the publisher loop on a daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._thread_main, name="publisher", daemon=True)
        self._thread.start()

    def submit(self, batch: TickBatch) -> None:
        """Queue one tick batch; sheds oldest-first past the byte budget."""
        item = _QueuedBatch(
            subject=f"tele.{self._vehicle_id}.{batch.source_class}",
            payload=batch.payload,
            msg_id=batch.msg_id,
            submit_mono_ns=time.monotonic_ns(),
        )
        with self._lock:
            self._buffer.append(item)
            self._buffer_bytes += len(item.payload)
            while self._buffer_bytes > self._buffer_budget and len(self._buffer) > 1:
                shed = self._buffer.popleft()
                self._buffer_bytes -= len(shed.payload)
                self._publish_drops += 1
        self._wake_loop()

    def drain(self, timeout_s: float = 5.0) -> bool:
        """Block until everything queued is acked (or the bound expires)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                idle = not self._buffer and self._oldest_pending_mono_ns is None
            if idle:
                return True
            time.sleep(0.02)
        return False

    def stop(self, timeout_s: float = 5.0) -> None:
        """Stop the loop and join the thread; queued work is best-effort."""
        self._stopping.set()
        self._wake_loop()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout_s)

    @property
    def connected(self) -> bool:
        """Whether the NATS connection is currently up."""
        return self._connected

    @property
    def publish_drops(self) -> int:
        """Batches shed because the local NATS was unavailable or backlogged."""
        return self._publish_drops

    @property
    def published(self) -> int:
        """Batches acked by JetStream."""
        return self._published

    @property
    def registry_publishes(self) -> int:
        """Registry snapshots published so far."""
        return self._registry_publishes

    def publish_lag_ms(self) -> float:
        """Age of the oldest unacked publish (feeds ``sys.agent.publish_lag_ms``)."""
        with self._lock:
            oldest = self._oldest_pending_mono_ns
            if oldest is None and self._buffer:
                oldest = self._buffer[0].submit_mono_ns
        if oldest is None:
            return 0.0
        return (time.monotonic_ns() - oldest) / 1e6

    # -- loop internals ---------------------------------------------------------

    def _wake_loop(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None and loop.is_running():
            loop.call_soon_threadsafe(wake.set)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception:
            logger.exception("publisher: loop failed")

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        backoff = _CONNECT_BACKOFF_START_S
        while not self._stopping.is_set():
            try:
                client = await self._connect()
            except Exception as exc:
                # Local NATS down at startup: retry with backoff, keep
                # capturing meanwhile (docs/AGENT_DESIGN.md -> Failure modes).
                logger.warning("publisher: cannot connect to %s: %s", self._nats_url, exc)
                await self._sleep(backoff)
                backoff = min(backoff * 2, _CONNECT_BACKOFF_MAX_S)
                continue
            backoff = _CONNECT_BACKOFF_START_S
            self._connected = True
            logger.info("publisher: connected to %s", self._nats_url)
            try:
                await self._serve(client)
            except Exception:
                logger.exception("publisher: session failed, reconnecting")
            finally:
                self._connected = False
                try:
                    await client.close()
                except Exception:  # noqa: BLE001 - closing a dead client
                    pass

    async def _connect(self) -> NatsClient:
        options: dict[str, object] = {
            "servers": [self._nats_url],
            "max_reconnect_attempts": -1,
            "disconnected_cb": self._on_disconnected,
            "reconnected_cb": self._on_reconnected,
        }
        if self._creds_path:
            options["user_credentials"] = self._creds_path
        return await nats.connect(**options)

    async def _on_disconnected(self) -> None:
        self._connected = False
        logger.warning("publisher: NATS disconnected")

    async def _on_reconnected(self) -> None:
        self._connected = True
        logger.info("publisher: NATS reconnected")

    async def _serve(self, client: NatsClient) -> None:
        js = client.jetstream()
        await self._ensure_streams(js)
        await self._publish_registry(js)

        subscriptions = []
        if self._on_rtcm is not None:
            subscriptions.append(
                await client.subscribe(f"rtcm.{self._vehicle_id}", cb=self._handle_rtcm)
            )
        if self._on_session is not None:
            subscriptions.append(
                await js.subscribe(
                    f"cmd.{self._vehicle_id}.session",
                    stream="CMD",
                    ordered_consumer=True,
                    deliver_policy=api.DeliverPolicy.LAST,
                    cb=self._handle_session,
                )
            )
        registry_task = asyncio.create_task(self._registry_loop(js))
        try:
            await self._send_loop(js)
        finally:
            registry_task.cancel()
            for subscription in subscriptions:
                try:
                    await subscription.unsubscribe()
                except Exception:  # noqa: BLE001 - connection may already be gone
                    pass

    async def _ensure_streams(self, js: JetStreamContext) -> None:
        """Create or converge the TELE and CMD streams (idempotent)."""
        tele = api.StreamConfig(
            name="TELE",
            subjects=[f"tele.{self._vehicle_id}.>"],
            storage=api.StorageType.FILE,
            retention=api.RetentionPolicy.LIMITS,
            max_age=self._tele_max_age_s,
            max_bytes=self._tele_max_bytes,
            num_replicas=1,
        )
        cmd = api.StreamConfig(
            name="CMD",
            subjects=[f"cmd.{self._vehicle_id}.>"],
            storage=api.StorageType.FILE,
            retention=api.RetentionPolicy.LIMITS,
            max_msgs_per_subject=1,
            num_replicas=1,
        )
        for config in (tele, cmd):
            try:
                await js.add_stream(config)
            except nats.js.errors.BadRequestError:
                # Exists with a different configuration: converge it.
                await js.update_stream(config)

    async def _publish_registry(self, js: JetStreamContext) -> None:
        subject = f"tele.{self._vehicle_id}.{REGISTRY_SOURCE_CLASS}"
        await js.publish(
            subject,
            self._registry_payload,
            timeout=_PUBLISH_TIMEOUT_S,
            headers={MSG_TYPE_HEADER: MSG_TYPE_REGISTRY},
        )
        self._registry_publishes += 1

    async def _registry_loop(self, js: JetStreamContext) -> None:
        """Republish the registry so a trimmed pit stream always holds one copy."""
        while True:
            await asyncio.sleep(self._registry_interval_s)
            try:
                await self._publish_registry(js)
            except Exception as exc:  # noqa: BLE001 - retried next interval
                logger.warning("publisher: registry republish failed: %s", exc)

    async def _send_loop(self, js: JetStreamContext) -> None:
        wake = self._wake
        assert wake is not None
        pending: deque[tuple[asyncio.Task[api.PubAck], _QueuedBatch]] = deque()
        try:
            while True:
                self._fill_window(js, pending)
                if not pending:
                    if self._stopping.is_set():
                        return
                    wake.clear()
                    with self._lock:
                        buffered = bool(self._buffer)
                    if buffered:
                        continue
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=0.25)
                    except TimeoutError:
                        pass
                    continue

                task, item = pending[0]
                try:
                    await task
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Publish failed (broker down/wedged). Later pipelined
                    # publishes are now unknown: requeue everything in order
                    # and retry — JetStream dedupe on msg-id makes any
                    # actually-landed redelivery idempotent.
                    await self._requeue(pending)
                    if self._stopping.is_set():
                        return
                    logger.warning("publisher: publish failed, retrying: %s", exc)
                    await self._sleep(_RETRY_BACKOFF_S)
                    continue
                pending.popleft()
                self._published += 1
                with self._lock:
                    self._oldest_pending_mono_ns = pending[0][1].submit_mono_ns if pending else None
        finally:
            await self._requeue(pending)

    def _fill_window(
        self,
        js: JetStreamContext,
        pending: deque[tuple[asyncio.Task[api.PubAck], _QueuedBatch]],
    ) -> None:
        while len(pending) < self._window:
            with self._lock:
                if not self._buffer:
                    return
                item = self._buffer.popleft()
                self._buffer_bytes -= len(item.payload)
                if self._oldest_pending_mono_ns is None:
                    self._oldest_pending_mono_ns = item.submit_mono_ns
            task = asyncio.create_task(
                js.publish(
                    item.subject,
                    item.payload,
                    timeout=_PUBLISH_TIMEOUT_S,
                    stream="TELE",
                    headers={
                        MSG_TYPE_HEADER: MSG_TYPE_BATCH,
                        "Nats-Msg-Id": item.msg_id,
                    },
                )
            )
            pending.append((task, item))

    async def _requeue(self, pending: deque[tuple[asyncio.Task[api.PubAck], _QueuedBatch]]) -> None:
        items: list[_QueuedBatch] = []
        while pending:
            task, item = pending.popleft()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014, BLE001
                pass
            items.append(item)
        with self._lock:
            for item in reversed(items):
                self._buffer.appendleft(item)
                self._buffer_bytes += len(item.payload)
            while self._buffer_bytes > self._buffer_budget and len(self._buffer) > 1:
                shed = self._buffer.pop()
                self._buffer_bytes -= len(shed.payload)
                self._publish_drops += 1
            self._oldest_pending_mono_ns = None

    async def _handle_rtcm(self, msg: Msg) -> None:
        handler = self._on_rtcm
        if handler is None:
            return
        # Best-effort fire-and-forget: corrections are stateless and the
        # serial write is blocking, so keep it off the event loop.
        await asyncio.to_thread(self._call_handler, handler, msg.data, "rtcm")

    async def _handle_session(self, msg: Msg) -> None:
        handler = self._on_session
        if handler is None:
            return
        await asyncio.to_thread(self._call_handler, handler, msg.data, "session")

    @staticmethod
    def _call_handler(handler: PayloadHandler, payload: bytes, kind: str) -> None:
        try:
            handler(payload)
        except Exception:
            logger.exception("publisher: %s handler failed", kind)

    async def _sleep(self, seconds: float) -> None:
        """Sleep in small slices so a stop request interrupts promptly."""
        deadline = time.monotonic() + seconds
        while not self._stopping.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.1, remaining))
