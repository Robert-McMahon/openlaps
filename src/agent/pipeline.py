"""The single-consumer sample pipeline: mapper -> timing tap -> RBE -> batcher.

``docs/AGENT_DESIGN.md`` -> Sample lifecycle steps 2-5. Everything
ordering-sensitive lives here and runs on one thread, so no shared mutable
state needs locking beyond the collector queues. The pipeline is pure with
respect to time: callers drive it with ``ingest`` and ``flush`` — it owns
no thread and reads no clock of its own.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from agent.timing_app import LapTimingApp
from collectors.clock import WallClock
from core.batcher import Batcher
from core.catalog import RuntimeCatalog
from core.rbe import RbeFilter
from core.samples import Sample

logger = logging.getLogger(__name__)

DERIVED_SOURCE_CLASS = "derived"
GNSS_TIME_CHANNEL = "position.time_unix_ms"
"""Mapped channel name that, when present, steers the agent's wall clock."""

_MAX_TRACKED_UNMAPPED_REFS = 1024

GnssTimeObserver = Callable[[int, float], None]


@dataclass(frozen=True, slots=True)
class TickBatch:
    """One serialized SampleBatch ready for the publisher."""

    source_class: str
    payload: bytes
    epoch_unix_ms: int
    epoch_mono_ns: int
    msg_id: str


class Pipeline:
    """Map, tap, filter and batch samples on the pipeline thread."""

    def __init__(
        self,
        catalog: RuntimeCatalog,
        *,
        tick_ms: int = 20,
        timing_app: LapTimingApp | None = None,
        on_gnss_time: GnssTimeObserver | None = None,
    ) -> None:
        self._source_map = catalog.source_map
        self._rbe = RbeFilter()
        self._batcher = Batcher(catalog.registry.registry_seq, catalog.policies_by_id, tick_ms)
        self._tick_ns = tick_ms * 1_000_000
        self._timing_app = timing_app
        self._on_gnss_time = on_gnss_time
        self._gnss_time_id = catalog.channel_ids.get(GNSS_TIME_CHANNEL)
        self._staged: list[tuple[str, int, Sample]] = []
        self._reported_unmapped: set[str] = set()
        self._reported_failures: set[str] = set()
        self.unmapped_refs = 0
        # Cumulative discarded tick windows (feeds ``sys.agent.encode_failures``).
        self.encode_failures = 0

    @property
    def rbe_suppressed(self) -> int:
        """Cumulative RBE suppressions (feeds ``sys.agent.rbe_suppressed``)."""
        return self._rbe.suppressed_count

    def ingest(self, source_class: str, sample: Sample) -> None:
        """Run one sample through map -> timing tap -> RBE and stage it."""
        mapping = self._source_map.get(sample.source_ref)
        if mapping is None:
            # Normal, not an error: a car broadcasts plenty a profile
            # deliberately ignores (docs/AGENT_DESIGN.md -> Sample lifecycle).
            self.unmapped_refs += 1
            if (
                sample.source_ref not in self._reported_unmapped
                and len(self._reported_unmapped) < _MAX_TRACKED_UNMAPPED_REFS
            ):
                self._reported_unmapped.add(sample.source_ref)
                logger.info("pipeline: no catalog mapping for %s", sample.source_ref)
            return
        channel_id, policy = mapping

        if channel_id == self._gnss_time_id and self._on_gnss_time is not None:
            try:
                self._on_gnss_time(sample.t_mono_ns, float(sample.value))
            except (TypeError, ValueError):
                self._note_failure(f"gnss-time:{sample.source_ref}", "non-numeric GNSS time")

        timing = self._timing_app
        if timing is not None and channel_id in timing.subscribed_channel_ids:
            # Pre-RBE is non-negotiable: timing interpolates line crossings
            # between consecutive fixes and must never see decimated data.
            for derived in timing.observe(channel_id, sample):
                self.ingest(DERIVED_SOURCE_CLASS, derived)

        try:
            keep = self._rbe.accept(channel_id, sample, policy.rbe)
        except TypeError as exc:
            self._note_failure(f"rbe:{policy.name}", str(exc))
            return
        if keep:
            self._staged.append((source_class, channel_id, sample))

    def flush(self, wall_clock: WallClock) -> list[TickBatch]:
        """Partition staged samples into tick windows and serialize batches.

        Windows are anchored on the earliest staged capture time, so a
        pipeline that fell behind (or a replay driven faster than realtime)
        emits one valid batch per elapsed tick window instead of failing the
        batcher's window check. Empty ticks emit nothing.
        """
        staged = self._staged
        self._staged = []
        if not staged:
            return []
        staged.sort(key=lambda item: item[2].t_mono_ns)

        batches: list[TickBatch] = []
        index = 0
        total = len(staged)
        while index < total:
            epoch_mono_ns = staged[index][2].t_mono_ns
            epoch_unix_ms = round(wall_clock(epoch_mono_ns))
            while index < total and staged[index][2].t_mono_ns - epoch_mono_ns < self._tick_ns:
                source_class, channel_id, sample = staged[index]
                self._batcher.add(source_class, channel_id, sample)
                index += 1
            try:
                emitted = self._batcher.tick(epoch_unix_ms, epoch_mono_ns)
            except (TypeError, ValueError) as exc:
                # A mis-typed value must cost one tick window, never the
                # pipeline thread; the batcher already discarded the window.
                self._note_failure(f"encode:{exc}", str(exc))
                self.encode_failures += 1
                continue
            for source_class, payload in emitted.items():
                batches.append(
                    TickBatch(
                        source_class=source_class,
                        payload=payload,
                        epoch_unix_ms=epoch_unix_ms,
                        epoch_mono_ns=epoch_mono_ns,
                        msg_id=f"{source_class}:{epoch_unix_ms}",
                    )
                )
        return batches

    def _note_failure(self, key: str, message: str) -> None:
        if key in self._reported_failures or len(self._reported_failures) >= 256:
            return
        self._reported_failures.add(key)
        logger.warning("pipeline: %s: %s", key, message)
