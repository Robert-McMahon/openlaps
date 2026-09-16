"""Delivery channels, the fan-out policy, and the retry queue.

A channel is one method: deliver this message, or raise `Retryable` if it
might work later. The dispatcher decides who hears what (by severity), when
an unacknowledged alert is repeated, and what happens when a delivery fails
-- it is queued with backoff and a deadline, and the queue depth is visible
on the annunciator, because a queue that is growing is itself the news.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from queue import Empty, Queue
from typing import Protocol

from pit.notifier.alerts import AlertBook, Event, severity_rank

logger = logging.getLogger(__name__)

_BACKOFF_S = (2.0, 5.0, 15.0, 30.0, 60.0)


class Retryable(Exception):
    """A delivery that failed for a reason that may clear: queue it."""


@dataclass(frozen=True, slots=True)
class Message:
    kind: str
    title: str
    body: str
    severity: str
    fingerprint: str
    at: datetime
    url: str | None = None


def render(event: Event) -> Message:
    prefix = {"firing": "FIRING", "resolved": "resolved", "repeat": "STILL FIRING", "ack": "ack"}
    label = prefix.get(event.kind, event.kind)
    body = f"{event.alert.summary} ({event.alert.severity})"
    if event.by:
        body += f" — acknowledged by {event.by}"
    if event.note:
        body += f": {event.note}"
    return Message(
        kind=event.kind,
        title=f"{label}: {event.alert.summary}",
        body=body,
        severity=event.alert.severity,
        fingerprint=event.alert.fingerprint,
        at=event.at,
        url=event.alert.annotations.get("runbook_url"),
    )


class Channel(Protocol):
    name: str
    min_severity: str
    repeat_s: float | None

    def deliver(self, message: Message) -> None: ...


class LogChannel:
    """The channel that always works: the service log."""

    def __init__(self, name: str = "log", min_severity: str = "none") -> None:
        self.name = name
        self.min_severity = min_severity
        self.repeat_s: float | None = None

    def deliver(self, message: Message) -> None:
        level = logging.WARNING if message.severity == "critical" else logging.INFO
        logger.log(level, "notifier: %s", message.title)


class Broadcaster:
    """Fan-out of events to every open server-sent-events client."""

    def __init__(self, max_queue: int = 200) -> None:
        self._clients: set[Queue[dict[str, object]]] = set()
        self._lock = threading.Lock()
        self._max_queue = max_queue

    def subscribe(self) -> Queue[dict[str, object]]:
        client: Queue[dict[str, object]] = Queue(maxsize=self._max_queue)
        with self._lock:
            self._clients.add(client)
        return client

    def unsubscribe(self, client: Queue[dict[str, object]]) -> None:
        with self._lock:
            self._clients.discard(client)

    def publish(self, payload: dict[str, object]) -> None:
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.put_nowait(payload)
            except Exception:  # noqa: BLE001 - a full client queue is that client's problem
                pass

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)


class AnnunciatorChannel:
    """The pit-wall page. Delivery is a push to every open browser."""

    def __init__(
        self,
        broadcaster: Broadcaster,
        name: str = "annunciator",
        min_severity: str = "warning",
        repeat_s: float | None = None,
    ) -> None:
        self.name = name
        self.min_severity = min_severity
        self.repeat_s = repeat_s
        self._broadcaster = broadcaster

    def deliver(self, message: Message) -> None:
        self._broadcaster.publish({"type": "message", "channel": self.name, **asdict(message)})


@dataclass(slots=True)
class _Pending:
    channel: Channel
    message: Message
    attempts: int
    due_at: float
    deadline: float


@dataclass(slots=True)
class DispatchStats:
    delivered: int = 0
    failed: int = 0
    expired: int = 0
    per_channel: dict[str, int] = field(default_factory=dict)


class Dispatcher:
    """Who hears what, when it is repeated, and what happens when it fails."""

    def __init__(
        self,
        channels: list[Channel],
        *,
        retry_deadline_s: float = 1800.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.channels = channels
        self.retry_deadline_s = retry_deadline_s
        self._monotonic = monotonic
        self._queue: deque[_Pending] = deque()
        self._lock = threading.Lock()
        self.stats = DispatchStats()

    # -- fan-out -----------------------------------------------------------

    def announce(self, event: Event, book: AlertBook) -> None:
        """Deliver an event to every channel whose policy wants it."""
        message = render(event)
        for channel in self.channels:
            if severity_rank(event.alert.severity) < severity_rank(channel.min_severity):
                continue
            if self._attempt(channel, message):
                self._note_notified(book, event, channel)

    def repeat_due(self, book: AlertBook, now: datetime) -> list[Event]:
        """Re-deliver unacknowledged alerts whose channel repeat interval has passed."""
        repeated: list[Event] = []
        for entry in book.unacknowledged():
            for channel in self.channels:
                if channel.repeat_s is None:
                    continue
                if severity_rank(entry.alert.severity) < severity_rank(channel.min_severity):
                    continue
                last = entry.last_notified.get(channel.name)
                if last is None or (now - last).total_seconds() >= channel.repeat_s:
                    event = Event("repeat", entry.alert, now)
                    if self._attempt(channel, render(event)):
                        entry.last_notified[channel.name] = now
                        repeated.append(event)
        return repeated

    @staticmethod
    def _note_notified(book: AlertBook, event: Event, channel: Channel) -> None:
        entry = book.active.get(event.alert.fingerprint)
        if entry is not None:
            entry.last_notified[channel.name] = event.at

    # -- delivery and retry --------------------------------------------------

    def _attempt(self, channel: Channel, message: Message, attempts: int = 0) -> bool:
        try:
            channel.deliver(message)
        except Retryable as exc:
            now = self._monotonic()
            backoff = _BACKOFF_S[min(attempts, len(_BACKOFF_S) - 1)]
            deadline = now + self.retry_deadline_s if attempts == 0 else None
            logger.warning(
                "notifier: %s could not deliver %r (%s); retry in %.0fs",
                channel.name,
                message.title,
                exc,
                backoff,
            )
            with self._lock:
                self._queue.append(
                    _Pending(channel, message, attempts + 1, now + backoff, deadline or now)
                )
            return False
        except Exception:  # noqa: BLE001 - one channel's bug must not silence the others
            logger.exception("notifier: %s failed permanently on %r", channel.name, message.title)
            self.stats.failed += 1
            return False
        self.stats.delivered += 1
        self.stats.per_channel[channel.name] = self.stats.per_channel.get(channel.name, 0) + 1
        return True

    def pump(self) -> int:
        """Retry everything that is due; return how many were delivered."""
        now = self._monotonic()
        with self._lock:
            due = [item for item in self._queue if item.due_at <= now]
            for item in due:
                self._queue.remove(item)
        delivered = 0
        for item in due:
            if now > item.deadline:
                self.stats.expired += 1
                logger.error(
                    "notifier: gave up on %r via %s", item.message.title, item.channel.name
                )
                continue
            if self._retry(item):
                delivered += 1
        return delivered

    def _retry(self, item: _Pending) -> bool:
        try:
            item.channel.deliver(item.message)
        except Retryable:
            backoff = _BACKOFF_S[min(item.attempts, len(_BACKOFF_S) - 1)]
            with self._lock:
                self._queue.append(
                    _Pending(
                        item.channel,
                        item.message,
                        item.attempts + 1,
                        self._monotonic() + backoff,
                        item.deadline,
                    )
                )
            return False
        except Exception:  # noqa: BLE001
            logger.exception("notifier: %s failed permanently on retry", item.channel.name)
            self.stats.failed += 1
            return False
        self.stats.delivered += 1
        self.stats.per_channel[item.channel.name] = (
            self.stats.per_channel.get(item.channel.name, 0) + 1
        )
        return True

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)

    def snapshot(self) -> dict[str, object]:
        return {
            "pending": self.pending_count,
            "delivered": self.stats.delivered,
            "failed": self.stats.failed,
            "expired": self.stats.expired,
            "per_channel": dict(self.stats.per_channel),
        }


def drain(client: Queue[dict[str, object]], timeout_s: float) -> dict[str, object] | None:
    """One event from an SSE client's queue, or None after the timeout."""
    try:
        return client.get(timeout=timeout_s)
    except Empty:
        return None
