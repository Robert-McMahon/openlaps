"""In-agent lap timing: pure timing modules fed by the pre-RBE tap.

``docs/AGENT_DESIGN.md`` -> Timing engine integration: the ported pure
modules (``timing_core``, ``distance_model``, ``reference_lap``) run inside
the pipeline thread, fed full-rate position samples *before* RBE. Outputs
are emitted as derived channels (``lap.*``, ``timing.*``) that re-enter the
pipeline at the mapper stage with reserved registry IDs; event-like outputs
travel as ``lap.event`` samples with an encoded JSON payload so the wire
format stays uniform. Session identity from ``cmd.<vehicle>.session`` is
stamped onto those events, mirroring how the predecessor tagged laps per
driver stint.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from dataclasses import dataclass

from core.catalog import RuntimeCatalog
from core.samples import Sample
from timing.distance_model import DistanceModel
from timing.reference_lap import DeltaTracker, LapRecorder
from timing.timing_core import EventType, GPSPoint, TimingEngine, TimingEvent
from timing.tracks import TrackDefinition, load_tracks

logger = logging.getLogger(__name__)

_SESSION_STAMP_KEYS = ("session_id", "driver", "stint_number", "session_type", "track_name")


@dataclass(slots=True)
class TimingAppStats:
    """Counters surfaced through the agent status payload."""

    fixes: int = 0
    unpaired_fixes: int = 0
    events: int = 0
    track_switches: int = 0


class LapTimingApp:
    """Host the timing engine on the pipeline thread and emit derived samples.

    Not thread-safe by design: every call happens on the single pipeline
    thread (``docs/AGENT_DESIGN.md`` -> Process model), including session
    updates, which the agent routes through the pipeline.
    """

    def __init__(
        self,
        catalog: RuntimeCatalog,
        position_glob: str,
        track: TrackDefinition,
        *,
        tracks_by_name: dict[str, TrackDefinition] | None = None,
    ) -> None:
        subscribed = {
            name: channel_id
            for name, channel_id in catalog.channel_ids.items()
            if fnmatch.fnmatchcase(name, position_glob)
        }
        if not subscribed:
            raise ValueError(f"apps.lap_timing.position {position_glob!r} matches no channels")
        lat_ids = {cid for name, cid in subscribed.items() if name.endswith(".lat")}
        lon_ids = {cid for name, cid in subscribed.items() if name.endswith(".lon")}
        if len(lat_ids) != 1 or len(lon_ids) != 1:
            raise ValueError(
                f"apps.lap_timing.position {position_glob!r} must match exactly one "
                "*.lat and one *.lon channel"
            )
        self.subscribed_channel_ids = frozenset(subscribed.values())
        self._lat_id = next(iter(lat_ids))
        self._lon_id = next(iter(lon_ids))
        self._tracks_by_name = tracks_by_name if tracks_by_name is not None else {}
        self._session: dict[str, object] = {}
        self.stats = TimingAppStats()
        self._pending_lat: Sample | None = None
        self._pending_lon: Sample | None = None
        self._last_emitted: dict[str, object] = {}
        self._track: TrackDefinition | None = None
        self._install_track(track)

    @property
    def track_name(self) -> str:
        """Name of the track currently timing."""
        return self._track.name if self._track is not None else ""

    def apply_session(self, session: dict[str, object]) -> None:
        """Adopt new session identity; switch tracks when the session names one."""
        self._session = dict(session)
        requested = session.get("track_name")
        if (
            isinstance(requested, str)
            and requested
            and self._track is not None
            and requested != self._track.name
        ):
            replacement = self._tracks_by_name.get(requested)
            if replacement is None:
                logger.warning(
                    "timing: session requested unknown track %r, staying on %r",
                    requested,
                    self._track.name,
                )
            else:
                logger.info("timing: switching track %r -> %r", self._track.name, requested)
                self._install_track(replacement)
                self.stats.track_switches += 1

    def observe(self, channel_id: int, sample: Sample) -> list[Sample]:
        """Feed one pre-RBE position sample; returns derived samples to re-enter."""
        if channel_id == self._lat_id:
            self._pending_lat = sample
        elif channel_id == self._lon_id:
            self._pending_lon = sample
        else:
            return []

        lat, lon = self._pending_lat, self._pending_lon
        if lat is None or lon is None:
            return []
        if lat.t_mono_ns != lon.t_mono_ns:
            # A fix is one decoded sentence/frame: lat and lon share a capture
            # stamp. A mismatch means one half of a fix went missing.
            if abs(lat.t_mono_ns - lon.t_mono_ns) > 0:
                self.stats.unpaired_fixes += 1
            return []
        self._pending_lat = None
        self._pending_lon = None
        return self._process_fix(lat, lon)

    def _install_track(self, track: TrackDefinition) -> None:
        self._track = track
        self._engine = TimingEngine(track.lines)
        self._distance = DistanceModel(track.length_m, track.mini_sectors)
        self._recorder = LapRecorder()
        self._delta = DeltaTracker()
        self._pending_lat = None
        self._pending_lon = None
        self._last_emitted.clear()

    def _process_fix(self, lat: Sample, lon: Sample) -> list[Sample]:
        self.stats.fixes += 1
        point = GPSPoint(
            lat=float(lat.value), lon=float(lon.value), timestamp=lat.t_wall_ms / 1000.0
        )
        emitted: list[Sample] = []

        def emit(name: str, value: object, *, always: bool = False) -> None:
            if not always and self._last_emitted.get(name) == value:
                return
            self._last_emitted[name] = value
            emitted.append(Sample(f"derived:{name}", lat.t_mono_ns, lat.t_wall_ms, value))

        self._distance.update(point.lat, point.lon)
        state = self._engine.state
        lap_running = state.lap_start_time > 0.0
        if lap_running:
            self._recorder.add(self._distance.lap_distance, point.timestamp - state.lap_start_time)

        events = self._engine.process_point(point)
        if not lap_running and self._engine.state.lap_start_time > 0.0:
            # Timing just started (first crossing, which emits no events):
            # anchor lap distance at the line, not at GPS acquisition, so the
            # reference curve and later laps share the same distance origin.
            self._distance.start_lap()
            self._recorder.reset()
        for event in events:
            self.stats.events += 1
            emit("lap.event", self._encode_event(event), always=True)
            if event.type == EventType.LAP_COMPLETED:
                candidate = self._recorder.complete(event.lap_time)
                self._delta.offer(candidate, event.valid)
                self._recorder.reset()
                self._distance.start_lap()
                emit("lap.last_time", event.lap_time, always=True)
                if state.best_lap_time > 0.0:
                    emit("lap.best_time", state.best_lap_time)

        snapshot = self._engine.snapshot()
        emit("lap.number", int(snapshot["lap_number"]))
        emit("lap.sector", int(snapshot["sector"]))
        if state.lap_start_time > 0.0:
            emit("timing.distance", self._distance.lap_distance, always=True)
            elapsed = point.timestamp - state.lap_start_time
            delta = self._delta.delta(self._distance.lap_distance, elapsed)
            if delta is not None:
                emit("timing.delta_best", delta, always=True)
                predicted = self._delta.predicted(self._distance.lap_distance, elapsed)
                if predicted is not None:
                    emit("timing.predicted_lap", predicted, always=True)
        return emitted

    def _encode_event(self, event: TimingEvent) -> str:
        payload: dict[str, object] = {
            "type": event.type.value,
            "time": event.time,
            "line": event.line,
            "lap_number": event.lap_number,
            "sector": event.sector,
            "split_time": event.split_time,
            "lap_time": event.lap_time,
            "valid": event.valid,
            "direction": event.direction,
            "pit_status": event.pit_status,
            "lat": event.lat,
            "lon": event.lon,
        }
        for key in _SESSION_STAMP_KEYS:
            value = self._session.get(key)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def build_lap_timing_app(
    catalog: RuntimeCatalog,
    position_glob: str,
    track_name: str,
    tracks_dir: str,
) -> LapTimingApp:
    """Load the profile's tracks and build the app on the configured one.

    A track the catalog names but the profile doesn't ship is a config
    error and fatal at startup (``docs/AGENT_DESIGN.md`` -> Failure modes).
    """
    tracks = load_tracks(tracks_dir)
    track = tracks.get(track_name)
    if track is None:
        available = ", ".join(sorted(tracks)) or "none"
        raise ValueError(
            f"apps.lap_timing.track {track_name!r} not found under {tracks_dir} "
            f"(available: {available})"
        )
    return LapTimingApp(catalog, position_glob, track, tracks_by_name=tracks)
