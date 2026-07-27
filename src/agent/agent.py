"""The vehicle agent process: collectors -> queues -> pipeline -> JetStream.

Wiring exactly per ``docs/AGENT_DESIGN.md``: collector threads feed bounded
drop-oldest queues; a single pipeline thread runs mapper -> pre-RBE timing
tap -> RBE -> batcher on a fixed tick; an async publisher thread lands
batches on the local JetStream. The health loop supervises collector
threads and publishes all agent introspection as ordinary ``sys.agent.*``
telemetry — no side channel.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent.clock import SteeredClock
from agent.pipeline import DERIVED_SOURCE_CLASS, Pipeline
from agent.publisher import (
    DEFAULT_BUFFER_BYTES,
    DEFAULT_REGISTRY_INTERVAL_S,
    DEFAULT_TELE_MAX_AGE_S,
    DEFAULT_TELE_MAX_BYTES,
    DEFAULT_WINDOW,
    JetStreamPublisher,
)
from agent.queues import DEFAULT_MAXLEN, SampleQueue
from agent.session import SessionStore
from agent.timing_app import LapTimingApp, build_lap_timing_app
from collectors.can import CanCollector
from collectors.host import HostCollector
from collectors.serial.transport import SerialCollector
from core.catalog import (
    DEFAULT_DERIVED_CHANNELS,
    DerivedChannel,
    RuntimeCatalog,
    build_runtime_catalog,
)
from core.config import ProfileConfig, load_profile
from core.pb import telemetry_pb2 as pb
from core.samples import Sample

logger = logging.getLogger(__name__)

_RESTART_BACKOFF_START_S = 1.0
_RESTART_BACKOFF_MAX_S = 30.0


@dataclass(frozen=True, slots=True)
class AgentSettings:
    """Deploy-time knobs (``example.env``); the profile describes the car."""

    profile_dir: Path
    nats_url: str = "nats://127.0.0.1:4222"
    nats_creds: str | None = None
    vehicle_id: str | None = None
    tick_ms: int = 20
    queue_maxlen: int = DEFAULT_MAXLEN
    publish_window: int = DEFAULT_WINDOW
    publish_buffer_bytes: int = DEFAULT_BUFFER_BYTES
    tele_max_age_s: float = DEFAULT_TELE_MAX_AGE_S
    tele_max_bytes: int = DEFAULT_TELE_MAX_BYTES
    registry_interval_s: float = DEFAULT_REGISTRY_INTERVAL_S
    state_dir: Path | None = None
    health_interval_s: float = 1.0

    @classmethod
    def from_env(
        cls, environ: dict[str, str] | None = None, profile_dir: str | Path | None = None
    ) -> AgentSettings:
        """Build settings from the ``OPENLAPS_*`` environment set.

        Unset or blank variables (``example.env`` templates them blank) fall
        back to the dataclass defaults. Note ``slots=True`` means the class
        attributes are descriptors, so defaults must come from the dataclass
        machinery, never from ``cls.<field>``.
        """
        env = dict(os.environ) if environ is None else environ
        profile = profile_dir if profile_dir is not None else env.get("OPENLAPS_PROFILE")
        if not profile:
            raise ValueError("no profile: pass a profile directory or set OPENLAPS_PROFILE")
        kwargs: dict[str, object] = {"profile_dir": Path(profile)}

        def take(env_key: str, field: str, parse) -> None:
            value = env.get(env_key)
            if value:
                try:
                    kwargs[field] = parse(value)
                except ValueError as exc:
                    raise ValueError(f"{env_key}={value!r}: {exc}") from exc

        take("OPENLAPS_NATS_URL", "nats_url", str)
        take("OPENLAPS_NATS_CREDS", "nats_creds", str)
        take("OPENLAPS_VEHICLE_ID", "vehicle_id", str)
        take("OPENLAPS_TICK_MS", "tick_ms", int)
        take("OPENLAPS_QUEUE_SIZE", "queue_maxlen", int)
        take("OPENLAPS_PUBLISH_WINDOW", "publish_window", int)
        take("OPENLAPS_PUBLISH_BUFFER_BYTES", "publish_buffer_bytes", int)
        take("OPENLAPS_TELE_MAX_AGE_H", "tele_max_age_s", lambda hours: float(hours) * 3600.0)
        take("OPENLAPS_TELE_MAX_BYTES", "tele_max_bytes", int)
        take("OPENLAPS_REGISTRY_REPUBLISH_S", "registry_interval_s", float)
        take("OPENLAPS_STATE_DIR", "state_dir", Path)
        return cls(**kwargs)


@dataclass(slots=True)
class _Supervised:
    """One collector under health-loop supervision."""

    collector: CanCollector | SerialCollector | HostCollector
    queue: SampleQueue
    restarts: int = 0
    backoff_s: float = _RESTART_BACKOFF_START_S
    next_restart_mono: float = 0.0
    expected: bool = True


@dataclass(slots=True)
class _SessionInbox:
    """Hands session updates from the publisher thread to the pipeline thread."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    pending: dict[str, object] | None = None

    def put(self, session: dict[str, object]) -> None:
        with self.lock:
            self.pending = session

    def take(self) -> dict[str, object] | None:
        with self.lock:
            pending, self.pending = self.pending, None
            return pending


def agent_derived_channels(collector_names: list[str]) -> tuple[DerivedChannel, ...]:
    """The derived-channel set this agent adds to the registry."""
    channels = list(DEFAULT_DERIVED_CHANNELS)
    channels += [
        DerivedChannel("sys.agent.status", pb.STRING),
        DerivedChannel("sys.agent.unmapped_refs", pb.INT64),
        DerivedChannel("sys.agent.rbe_suppressed", pb.INT64),
        DerivedChannel("sys.agent.publish_drops", pb.INT64),
        DerivedChannel("sys.agent.publish_lag_ms", pb.DOUBLE, "ms"),
        DerivedChannel("sys.agent.clock_offset_ms", pb.DOUBLE, "ms"),
        DerivedChannel("sys.agent.clock_source", pb.STRING),
    ]
    channels += [DerivedChannel(f"sys.agent.drops.{name}", pb.INT64) for name in collector_names]
    return tuple(channels)


class VehicleAgent:
    """One process turning collector output into batched JetStream telemetry."""

    def __init__(
        self,
        settings: AgentSettings,
        *,
        bus_factory=None,
        serial_factory=None,
    ) -> None:
        """Load and validate everything fatal-at-startup; start() spins threads.

        A malformed profile raises ``ConfigError`` here — config errors are
        the one thing that *should* stop the agent (``docs/AGENT_DESIGN.md``
        -> Failure modes).
        """
        self.settings = settings
        self.profile: ProfileConfig = load_profile(settings.profile_dir)
        self.vehicle_id = settings.vehicle_id or self.profile.vehicle.vehicle.id

        collector_names = [bus.name for bus in self.profile.vehicle.buses]
        collector_names += [source.name for source in self.profile.vehicle.serial]
        if self.profile.vehicle.host.enabled:
            collector_names.append("host")

        state_dir = settings.state_dir
        registry_state = state_dir / "registry-state.json" if state_dir else None
        session_state = (
            state_dir / "session-state.json"
            if state_dir
            else self.profile.path / ".session-state.json"
        )
        self.catalog: RuntimeCatalog = build_runtime_catalog(
            self.profile,
            state_path=registry_state,
            derived_channels=agent_derived_channels(collector_names),
        )
        self.clock = SteeredClock()
        self.session_store = SessionStore(session_state)
        self._session_inbox = _SessionInbox()

        self.timing_app: LapTimingApp | None = None
        lap_timing = self.profile.catalog.apps.lap_timing
        if lap_timing is not None:
            self.timing_app = build_lap_timing_app(
                self.catalog,
                lap_timing.position,
                lap_timing.track,
                str(self.profile.path / "tracks"),
            )
            self.timing_app.apply_session(self.session_store.current())

        self.pipeline = Pipeline(
            self.catalog,
            tick_ms=settings.tick_ms,
            timing_app=self.timing_app,
            on_gnss_time=self.clock.observe_gnss,
        )
        self.publisher = JetStreamPublisher(
            nats_url=settings.nats_url,
            vehicle_id=self.vehicle_id,
            registry_payload=self.catalog.registry.SerializeToString(),
            creds_path=settings.nats_creds,
            window=settings.publish_window,
            buffer_bytes=settings.publish_buffer_bytes,
            tele_max_age_s=settings.tele_max_age_s,
            tele_max_bytes=settings.tele_max_bytes,
            registry_interval_s=settings.registry_interval_s,
            on_session=self._on_session_payload,
            on_rtcm=self._on_rtcm_payload,
        )

        self._supervised: list[_Supervised] = []
        self._rtcm_collectors: list[SerialCollector] = []
        for bus in self.profile.vehicle.buses:
            queue = SampleQueue(bus.name, settings.queue_maxlen)
            collector = CanCollector(
                bus,
                self.profile.path,
                queue.put,
                wall_clock=self.clock,
                bus_factory=bus_factory,
            )
            self._supervised.append(_Supervised(collector, queue))
        for source in self.profile.vehicle.serial:
            queue = SampleQueue(source.name, settings.queue_maxlen)
            serial_collector = SerialCollector(
                source,
                queue.put,
                wall_clock=self.clock,
                serial_factory=serial_factory,
            )
            self._supervised.append(_Supervised(serial_collector, queue))
            if source.driver is not None:
                self._rtcm_collectors.append(serial_collector)
        if self.profile.vehicle.host.enabled:
            queue = SampleQueue("host", settings.queue_maxlen)
            self._supervised.append(
                _Supervised(
                    HostCollector(self.profile.vehicle.host, queue.put, wall_clock=self.clock),
                    queue,
                )
            )

        self._agent_queue = SampleQueue(DERIVED_SOURCE_CLASS, settings.queue_maxlen)
        self._stop = threading.Event()
        self._pipeline_thread: threading.Thread | None = None
        self._health_thread: threading.Thread | None = None
        self._started_mono = 0.0
        self._stopped = False

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        """Bring the process up in the spec's startup order."""
        logger.info(
            "agent: starting vehicle %s (%d channels, registry_seq %d)",
            self.vehicle_id,
            len(self.catalog.registry.channels),
            self.catalog.registry.registry_seq,
        )
        self._started_mono = time.monotonic()
        self._stop.clear()
        # Publisher first: it connects, provisions streams, and publishes the
        # registry — retrying forever if the local NATS is still booting.
        self.publisher.start()
        self._pipeline_thread = threading.Thread(
            target=self._pipeline_main, name="pipeline", daemon=True
        )
        self._pipeline_thread.start()
        for supervised in self._supervised:
            supervised.collector.start()
        self.clock.mark_running()
        self._health_thread = threading.Thread(target=self._health_main, name="health", daemon=True)
        self._health_thread.start()
        logger.info("agent: running with %d collector(s)", len(self._supervised))

    def stop(self) -> None:
        """Shut down in the spec's order; bounded, idempotent."""
        if self._stopped:
            return
        self._stopped = True
        logger.info("agent: stopping")
        self._stop.set()
        if self._health_thread is not None:
            self._health_thread.join(timeout=5.0)
        for supervised in self._supervised:
            supervised.expected = False
            supervised.collector.stop()
        # The pipeline thread drains every queue and flushes final partial
        # batches on its way out.
        if self._pipeline_thread is not None:
            self._pipeline_thread.join(timeout=10.0)
        self.publisher.drain(timeout_s=5.0)
        self._publish_final_status()
        self.publisher.stop()
        logger.info("agent: stopped")

    def run_forever(self, stop_event: threading.Event) -> None:
        """Start, then block until ``stop_event`` fires, then stop."""
        self.start()
        stop_event.wait()
        self.stop()

    # -- pipeline thread ---------------------------------------------------------

    def _pipeline_main(self) -> None:
        tick_s = self.settings.tick_ms / 1000.0
        next_tick = time.monotonic() + tick_s
        try:
            while not self._stop.is_set():
                delay = next_tick - time.monotonic()
                if delay <= 0:
                    next_tick = time.monotonic()
                else:
                    if self._stop.wait(delay):
                        break
                next_tick += tick_s
                self._drain_and_flush()
        finally:
            # Shutdown: one final drain so nothing captured is left behind.
            self._drain_and_flush()

    def _drain_and_flush(self) -> None:
        session = self._session_inbox.take()
        if session is not None and self.timing_app is not None:
            self.timing_app.apply_session(session)
        for supervised in self._supervised:
            queue = supervised.queue
            for sample in queue.drain():
                self.pipeline.ingest(queue.source_class, sample)
        for sample in self._agent_queue.drain():
            self.pipeline.ingest(DERIVED_SOURCE_CLASS, sample)
        for batch in self.pipeline.flush(self.clock):
            self.publisher.submit(batch)

    # -- health loop ---------------------------------------------------------------

    def _health_main(self) -> None:
        interval = self.settings.health_interval_s
        while not self._stop.wait(interval):
            try:
                self._supervise_collectors()
                self._emit_health_samples()
            except Exception:
                logger.exception("agent: health loop iteration failed")

    def _supervise_collectors(self) -> None:
        now = time.monotonic()
        for supervised in self._supervised:
            if not supervised.expected or supervised.collector.is_running():
                if supervised.collector.is_running():
                    supervised.backoff_s = _RESTART_BACKOFF_START_S
                continue
            if now < supervised.next_restart_mono:
                continue
            name = supervised.queue.source_class
            logger.warning("agent: collector %s is down, restarting", name)
            supervised.restarts += 1
            supervised.next_restart_mono = now + supervised.backoff_s
            supervised.backoff_s = min(supervised.backoff_s * 2, _RESTART_BACKOFF_MAX_S)
            try:
                supervised.collector.start()
            except Exception:
                logger.exception("agent: collector %s restart failed", name)

    def _emit_health_samples(self, status: str = "running") -> None:
        t_mono_ns = time.monotonic_ns()
        t_wall_ms = self.clock(t_mono_ns)

        def emit(name: str, value: object) -> None:
            self._agent_queue.put(Sample(f"derived:{name}", t_mono_ns, t_wall_ms, value))

        emit("sys.agent.status", self._status_payload(status))
        emit("sys.agent.unmapped_refs", self.pipeline.unmapped_refs)
        emit("sys.agent.rbe_suppressed", self.pipeline.rbe_suppressed)
        emit("sys.agent.publish_drops", self.publisher.publish_drops)
        emit("sys.agent.publish_lag_ms", self.publisher.publish_lag_ms())
        emit("sys.agent.clock_offset_ms", self.clock.offset_ms)
        emit("sys.agent.clock_source", self.clock.source)
        for supervised in self._supervised:
            emit(f"sys.agent.drops.{supervised.queue.source_class}", supervised.queue.dropped)

    def _status_payload(self, state: str) -> str:
        collectors = {
            supervised.queue.source_class: {
                "running": supervised.collector.is_running(),
                "restarts": supervised.restarts,
            }
            for supervised in self._supervised
        }
        return json.dumps(
            {
                "state": state,
                "uptime_s": round(time.monotonic() - self._started_mono, 1),
                "nats_connected": self.publisher.connected,
                "encode_failures": self.pipeline.encode_failures,
                "collectors": collectors,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def _publish_final_status(self) -> None:
        """One last ``sys.agent.status = stopping`` sample, best-effort."""
        self._emit_health_samples(status="stopping")
        # The pipeline thread is stopped and joined: it is safe to drive the
        # pipeline from here, the only remaining user.
        self._drain_and_flush()
        self.publisher.drain(timeout_s=2.0)

    # -- publisher-thread callbacks -----------------------------------------------

    def _on_session_payload(self, payload: bytes) -> None:
        session = self.session_store.update_from_bytes(payload)
        if session is not None:
            self._session_inbox.put(session)
            logger.info("agent: session update: %s", self.session_store.current())

    def _on_rtcm_payload(self, payload: bytes) -> None:
        for collector in self._rtcm_collectors:
            collector.write_rtcm(payload)
