"""Which live channels somebody is actually looking at.

The live view config says which channels *may* be shown. Most of the time
most of them are on no screen: a dashboard that is closed, a panel on
another page. The car should not spend radio on those, so in ``direct``
mode the pit asks it only for channels that have a subscriber on the local
MQTT broker, and for nothing at all when no dashboard is open.

The broker is the one place that knows, and it tells us in two ways:

- **Nobody is listening** is the answer to a publish. An MQTT 5 broker
  acknowledges a QoS 1 publish with "no matching subscribers" when that is
  the case, so the sink sends one publish per channel every couple of
  seconds at QoS 1 and reports the verdict here. Two such verdicts in a row
  make a channel *idle*: it leaves the car's selection.
- **Somebody started listening** is an event. With ``log_dest topic`` and
  ``log_type subscribe`` (deploy/mosquitto/mosquitto.conf) the broker
  publishes every SUBSCRIBE it receives, and any idle channel the new
  subscription matches is wanted again at once.

Nothing is remembered that cannot be re-learned. A service that has just
started, or has just reconnected to the broker, wants every configured
channel and lets the publish verdicts prune the list again within seconds,
so a subscription made while it was not watching is never missed. And if
the broker is not publishing its SUBSCRIBE log -- an older config -- the
tracker never arms and the selection stays the whole configured list, as it
was before this existed: idle channels could then never come back.
"""

from __future__ import annotations

from collections import deque

from paho.mqtt.client import topic_matches_sub

SUBSCRIBE_LOG_TOPIC = "$SYS/broker/log/M/subscribe"
# One publish per channel this often goes out at QoS 1 to ask the question.
PROBE_INTERVAL_S = 2.0
_MISSES_TO_IDLE = 2
# A verdict that crossed a SUBSCRIBE on its way here must not undo it.
_HOLD_S = 3.0


class Demand:
    """Channels with no subscriber, learned from the broker and unlearned on doubt."""

    def __init__(self, vehicle: str) -> None:
        self._prefix = f"openlaps/{vehicle}/"
        self.armed = False
        self.version = 0
        self._idle: set[str] = set()
        self._misses: dict[str, int] = {}
        self._recent: deque[tuple[float, str]] = deque(maxlen=64)

    @property
    def idle(self) -> frozenset[str]:
        return frozenset(self._idle)

    def wanted(self, channel: str) -> bool:
        """Whether the car should be asked for this channel."""
        return channel not in self._idle

    def reset(self) -> None:
        """Forget everything: a broker connection has just been (re)made."""
        self.armed = False
        self._misses.clear()
        self._recent.clear()
        if self._idle:
            self._idle.clear()
            self.version += 1

    def arm(self) -> None:
        """The broker is publishing its SUBSCRIBE log; pruning is safe."""
        self.armed = True

    def published(self, channel: str, matched: bool, now: float) -> None:
        """The broker's verdict on one QoS 1 publish of ``channel``."""
        if matched:
            self._misses.pop(channel, None)
            return
        if not self.armed or channel in self._idle:
            return
        topic = self._prefix + channel
        if any(now < until and topic_matches_sub(sub, topic) for until, sub in self._recent):
            return
        misses = self._misses.get(channel, 0) + 1
        if misses < _MISSES_TO_IDLE:
            self._misses[channel] = misses
            return
        self._misses.pop(channel, None)
        self._idle.add(channel)
        self.version += 1

    def subscribed(self, topic_filter: str, now: float) -> None:
        """A client subscribed to ``topic_filter``; wake whatever it matches."""
        try:
            woken = {c for c in self._idle if topic_matches_sub(topic_filter, self._prefix + c)}
        except ValueError:  # not a valid filter; nothing of ours matches it
            return
        self._recent.append((now + _HOLD_S, topic_filter))
        for channel in list(self._misses):
            if topic_matches_sub(topic_filter, self._prefix + channel):
                del self._misses[channel]
        if woken:
            self._idle -= woken
            self.version += 1


def parse_subscribe_log(payload: bytes) -> tuple[str, str] | None:
    """``b"<time>: <client> <qos> <filter>"`` -> (client id, topic filter)."""
    try:
        _, client, _, topic_filter = payload.decode("utf-8").split(" ", 3)
    except (UnicodeDecodeError, ValueError):
        return None
    return client, topic_filter
