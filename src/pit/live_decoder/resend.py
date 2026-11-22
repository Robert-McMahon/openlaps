"""Re-send each live channel's last value while the car is talking.

Live values are QoS 0 and never retained (a stale value on a gauge is worse
than none), and most channels are sent by the car only when they change. A
panel that subscribes late -- a dashboard just opened, or one Grafana has
just refreshed -- therefore showed "No data" until its channel next moved,
which for gear in the pits or the last lap time is a long wait.

So the newest value of every published channel goes out again once it has
been quiet for ``interval_s``, with its original capture time, for as long
as batches are still arriving from the car. When the car goes quiet the
re-sends stop with it: nothing here can make a dead feed look alive.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from pit.live_decoder.config import LiveConfig
from pit.live_decoder.limiter import LiveUpdate

# The car publishes at least once a second while it is on. Silence this long
# means it is off or out of reach, and its last values are no longer current.
FRESH_S = 5.0


@dataclass(slots=True)
class _Last:
    update: LiveUpdate
    sent: float


class Resender:
    """Remember the last published value per channel and say when to repeat it."""

    def __init__(self) -> None:
        self._last: dict[str, _Last] = {}

    def note(self, update: LiveUpdate, now: float) -> None:
        """Record a value handed to MQTT at ``now``."""
        self._last[update.channel] = _Last(update, now)

    def due(
        self,
        config: LiveConfig,
        now: float,
        last_message: float,
        wanted: Callable[[str], bool] | None = None,
    ) -> list[LiveUpdate]:
        """Values to repeat now; empty when disabled or the car is quiet.

        ``wanted`` says which channels the car is still being asked for
        (direct mode). One it is not sending is forgotten: its last value
        only gets older, and repeating it to the next gauge that subscribes
        would show a reading from whenever somebody last looked.
        """
        interval = config.defaults.resend_s
        if interval <= 0 or now - last_message > FRESH_S:
            return []
        repeats: list[LiveUpdate] = []
        for channel in list(self._last):
            if config.max_hz_for(channel) is None or (wanted is not None and not wanted(channel)):
                # Dropped from the live view by a reload, or nobody watching.
                del self._last[channel]
                continue
            last = self._last[channel]
            if now - last.sent >= interval:
                last.sent = now
                # Conflated whatever the channel's own policy: a repeat must
                # never queue behind, or outlive, a newer value.
                repeats.append(replace(last.update, conflate=True))
        return repeats
