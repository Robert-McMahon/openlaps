"""Conflating per-channel and aggregate rate limiting for live gauges."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from pit.live_decoder.config import LiveConfig
from pit.registry_cache import DecodedSample

_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class LiveUpdate:
    """One decoded value ready for MQTT publication."""

    channel: str
    capture_unix_ms: float
    value: object
    conflate: bool = True


@dataclass(slots=True)
class _ChannelState:
    interval_s: float
    pending: LiveUpdate | None = None
    next_due: float | None = None
    last_published: float | None = None


class ConflatingLimiter:
    """Keep the newest limited value; never queue stale gauge updates."""

    def __init__(self, config: LiveConfig) -> None:
        self.config = config
        self.suppressed: dict[str, int] = defaultdict(int)
        self.published: dict[str, int] = defaultdict(int)
        self.aggregate_sheds = 0
        self._states: dict[str, _ChannelState] = {}
        self._aggregate_at: float | None = None
        self._aggregate_tokens = 0.0

    def reconfigure(self, config: LiveConfig) -> None:
        """Apply a new view immediately, discarding stale pending updates."""
        self.config = config
        self._states.clear()
        self._aggregate_at = None
        self._aggregate_tokens = 0.0

    def offer(self, sample: DecodedSample, now: float) -> list[LiveUpdate]:
        """Observe a value and return anything that is publishable now."""
        max_hz = self.config.max_hz_for(sample.channel.name)
        if max_hz is None:
            return []
        update = LiveUpdate(
            sample.channel.name,
            sample.capture_unix_ms,
            sample.value,
            conflate=max_hz != 0,
        )
        state = self._states.setdefault(sample.channel.name, _ChannelState(interval_s=0.0))
        if max_hz == 0:
            return [update]

        interval = 1.0 / max_hz
        if state is None or state.interval_s != interval:
            state = _ChannelState(interval_s=interval)
            self._states[update.channel] = state

        if state.next_due is None or now + _EPSILON >= state.next_due:
            if state.pending is not None:
                self.suppressed[update.channel] += 1
                state.pending = None
            return [update]

        if state.pending is not None:
            self.suppressed[update.channel] += 1
        state.pending = update
        return []

    def drain(self, now: float) -> list[LiveUpdate]:
        """Emit due pending values, prioritising recently published channels."""
        ready: list[LiveUpdate] = []
        for state in self._states.values():
            if (
                state.pending is not None
                and state.next_due is not None
                and now + _EPSILON >= state.next_due
            ):
                ready.append(state.pending)
                state.pending = None
        ready.sort(
            key=lambda update: (
                self._states[update.channel].last_published
                if self._states[update.channel].last_published is not None
                else float("-inf"),
                update.channel,
            ),
            reverse=True,
        )
        return ready

    def conflate_candidates(self, candidates: list[LiveUpdate]) -> list[LiveUpdate]:
        """Keep only the newest same-batch update for each limited channel."""
        conflated: list[LiveUpdate] = []
        indexes: dict[str, int] = {}
        for update in candidates:
            if not update.conflate:
                conflated.append(update)
                continue
            index = indexes.get(update.channel)
            if index is None:
                indexes[update.channel] = len(conflated)
                conflated.append(update)
                continue
            conflated[index] = update
            self.suppressed[update.channel] += 1
        return conflated

    def admit(self, candidates: list[LiveUpdate], now: float) -> list[LiveUpdate]:
        """Apply the aggregate budget to one collectively ranked batch."""
        candidates.sort(
            key=lambda update: (self._last_published(update), update.channel),
            reverse=True,
        )
        limit = self.config.defaults.total_max_hz
        self._refill_aggregate(now, limit)
        available = (
            len(candidates)
            if limit == 0
            else min(len(candidates), max(0, int(self._aggregate_tokens + _EPSILON)))
        )
        accepted = candidates[:available]
        rejected = candidates[available:]

        for update in accepted:
            self.published[update.channel] += 1
            state = self._states.get(update.channel)
            if state is not None:
                state.last_published = now
                state.next_due = now + state.interval_s
        for update in rejected:
            self.aggregate_sheds += 1
            self.suppressed[update.channel] += 1
        if limit != 0:
            self._aggregate_tokens -= len(accepted)
        return accepted

    def _last_published(self, update: LiveUpdate) -> float:
        value = self._states[update.channel].last_published
        return value if value is not None else float("-inf")

    def _refill_aggregate(self, now: float, limit: float) -> None:
        if limit == 0:
            return
        capacity = max(1.0, limit)
        if self._aggregate_at is None or now < self._aggregate_at:
            self._aggregate_tokens = capacity
        else:
            elapsed = now - self._aggregate_at
            self._aggregate_tokens = min(capacity, self._aggregate_tokens + elapsed * limit)
        self._aggregate_at = now
