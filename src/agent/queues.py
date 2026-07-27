"""Bounded drop-oldest sample queues between collectors and the pipeline.

``docs/AGENT_DESIGN.md`` -> Process model: queues are bounded (default
10 000 samples per collector); on overflow the oldest sample is dropped and
a per-collector drop counter incremented. A collector thread must never
block on a slow pipeline — a stalled bus reader loses hardware frames,
which is strictly worse than shedding our own oldest samples.
"""

from __future__ import annotations

import threading
from collections import deque

from core.samples import Sample

DEFAULT_MAXLEN = 10_000


class SampleQueue:
    """One bounded queue from one collector into the pipeline thread."""

    __slots__ = ("_dropped", "_lock", "_maxlen", "_queue", "source_class")

    def __init__(self, source_class: str, maxlen: int = DEFAULT_MAXLEN) -> None:
        if maxlen <= 0:
            raise ValueError("maxlen must be positive")
        self.source_class = source_class
        self._maxlen = maxlen
        self._queue: deque[Sample] = deque()
        self._lock = threading.Lock()
        self._dropped = 0

    def put(self, sample: Sample) -> None:
        """Enqueue one sample, shedding the oldest when full (never blocks)."""
        with self._lock:
            if len(self._queue) >= self._maxlen:
                self._queue.popleft()
                self._dropped += 1
            self._queue.append(sample)

    def drain(self) -> list[Sample]:
        """Remove and return everything queued, in arrival order."""
        with self._lock:
            if not self._queue:
                return []
            items = list(self._queue)
            self._queue.clear()
        return items

    def __len__(self) -> int:
        return len(self._queue)

    @property
    def dropped(self) -> int:
        """Cumulative drop-oldest count (feeds ``sys.agent.drops.<collector>``)."""
        return self._dropped
