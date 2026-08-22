"""Shared machinery for the bench tools (`replay.py`, `lap_simulator.py`).

Both tools drive the real profile/catalog loading, the real `Pipeline` and
`LapTimingApp`, and the real `JetStreamPublisher` -- a bench harness is only
useful if it is the same code the car runs, not a parallel path that merely
looks similar. Kept out of each tool's own file so the two don't drift.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent.pipeline import TickBatch  # noqa: E402
from agent.publisher import DEFAULT_TELE_MAX_BYTES, JetStreamPublisher  # noqa: E402
from agent.timing_app import LapTimingApp, build_lap_timing_app  # noqa: E402
from core.catalog import RuntimeCatalog, build_runtime_catalog  # noqa: E402
from core.config import ProfileConfig, load_profile  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERVER = os.environ.get("OPENLAPS_NATS_URL", "nats://127.0.0.1:4222")
DEFAULT_PROFILE = REPO_ROOT / "profiles" / "example-club-racer"

# A publish-pacing sleep is capped so a single huge gap in a recording (the
# car sitting in the garage between sessions, say) can't stall the tool for
# real minutes -- accelerate through it instead.
MAX_PUBLISH_SLEEP_S = 5.0

# Backpressure bounds. `JetStreamPublisher.submit` sheds oldest-first past its
# byte budget, which is right for a car whose broker is wedged and wrong for a
# replay: an accelerated run would quietly drop the batches JetStream could not
# absorb fast enough, and the loss would surface much later as a hole in the
# database. Pacing on the publisher's own lag makes `--rate 0` mean "as fast as
# the local server will take it" instead of "as fast as memory will take it".
MAX_PUBLISH_LAG_MS = 2_000.0
MAX_PUBLISH_STALL_S = 30.0

# Decimal places of arc-minutes in `rmc_sentence`'s coordinate fields.
#
# Nine, because that is what round-trips the June-2025 dump exactly: rounding
# its decoded coordinates to 8 places reproduces 33% of latitudes and to 7
# places 3%, while 9 reproduces all of them. Those values are not on any NMEA
# text grid -- they behave as continuous float64 -- so there is no receiver
# field width to match, only a width at which this encoder stops contributing
# error. One place here is 1e-9/60 deg, about 1.9 um.
COORD_DECIMALS = 9
_COORD_WIDTH = COORD_DECIMALS + 3  # "mm." plus the decimals


def load_catalog(
    profile_dir: str | Path, *, vehicle: str | None, state_dir: str | Path | None
) -> tuple[ProfileConfig, RuntimeCatalog, str]:
    """Load the profile and build a runtime catalog for it.

    ``state_dir`` picks where the registry-generation counter persists;
    left unset, it defaults to the profile directory like the real agent
    does when ``OPENLAPS_STATE_DIR`` isn't set.
    """
    profile = load_profile(profile_dir)
    state_path = Path(state_dir) / ".registry-state.json" if state_dir else None
    catalog = build_runtime_catalog(profile, state_path=state_path)
    vehicle_id = vehicle or profile.vehicle.vehicle.id
    return profile, catalog, vehicle_id


def build_timing_app(profile: ProfileConfig, catalog: RuntimeCatalog) -> LapTimingApp | None:
    """The profile's configured lap-timing app, or None if it has none."""
    lap_timing = profile.catalog.apps.lap_timing
    if lap_timing is None:
        return None
    return build_lap_timing_app(
        catalog, lap_timing.position, lap_timing.track, str(profile.path / "tracks")
    )


def make_publisher(
    server: str,
    vehicle_id: str,
    catalog: RuntimeCatalog,
    tele_max_bytes: int = DEFAULT_TELE_MAX_BYTES,
) -> JetStreamPublisher:
    """A publisher wired exactly like the agent's, registry included.

    `tele_max_bytes` is the agent's own default. JetStream reserves it up
    front, so a server with a store smaller than that refuses to create TELE
    at all -- worth lowering when replaying into a small throwaway server
    rather than at a real pit.
    """
    return JetStreamPublisher(
        nats_url=server,
        vehicle_id=vehicle_id,
        registry_payload=catalog.registry.SerializeToString(),
        registry_interval_s=3600.0,
        tele_max_bytes=tele_max_bytes,
    )


def wait_connected(publisher: JetStreamPublisher, timeout_s: float = 15.0) -> None:
    """Block until the publisher's registry has landed on JetStream."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if publisher.registry_publishes >= 1:
            return
        time.sleep(0.05)
    raise TimeoutError("publisher never reached JetStream")


def publish_paced(publisher: JetStreamPublisher, batches: Iterable[TickBatch], rate: float) -> int:
    """Submit ``batches`` spaced to reproduce their recorded cadence / ``rate``.

    ``rate`` of 1.0 reproduces wall-clock pacing between consecutive batches'
    capture times; higher values compress it and 0 removes the pacing
    entirely. Batches already come out of `Pipeline.flush` in capture-time
    order, and an *iterable* is accepted rather than a list so a long replay
    can stream them instead of materialising the whole event first.

    Returns how many batches were submitted.
    """
    previous_mono_ns: int | None = None
    submitted = 0
    for batch in batches:
        if previous_mono_ns is not None and rate > 0:
            delay_s = (batch.epoch_mono_ns - previous_mono_ns) / 1e9 / rate
            if delay_s > 0:
                time.sleep(min(delay_s, MAX_PUBLISH_SLEEP_S))
        _await_publisher(publisher)
        publisher.submit(batch)
        submitted += 1
        previous_mono_ns = batch.epoch_mono_ns
    return submitted


def _await_publisher(publisher: JetStreamPublisher) -> None:
    """Hold off while the publisher's queue is behind, but never indefinitely.

    A broker that is down rather than merely busy would otherwise stall the
    replay forever. Past the stall bound the batch is submitted anyway and
    the loss becomes visible in ``publish_drops``, which is the honest
    outcome: a silent wait is indistinguishable from a slow one.
    """
    deadline = time.monotonic() + MAX_PUBLISH_STALL_S
    while publisher.publish_lag_ms() > MAX_PUBLISH_LAG_MS and time.monotonic() < deadline:
        time.sleep(0.01)


def rmc_sentence(lat: float, lon: float, speed_kmh: float, heading_deg: float) -> bytes:
    """Encode one ``$GPRMC`` fix (speed converts km/h -> knots per the format).

    The coordinate fields carry `COORD_DECIMALS` places of arc-minutes, which
    is deliberately finer than any receiver emits: this encoder's job is to
    carry a *recorded* position into the real NMEA decoder without adding
    error of its own, not to imitate a receiver's field width.

    That distinction was learned the expensive way. The fields were 4 decimal
    places -- one place is 0.185 m in latitude -- and P4.6's parity replay
    then disagreed with the predecessor's replay of the same event by p50
    1.2 ms of lap time, essentially all of it this rounding. Modelling a
    receiver's own quantisation is a worthwhile thing to do *on purpose*; it
    is a bad thing to inherit by accident from a format string.
    """
    ns, alat = ("N", lat) if lat >= 0 else ("S", -lat)
    ew, alon = ("E", lon) if lon >= 0 else ("W", -lon)
    lat_field = f"{int(alat):02d}{(alat - int(alat)) * 60:0{_COORD_WIDTH}.{COORD_DECIMALS}f}"
    lon_field = f"{int(alon):03d}{(alon - int(alon)) * 60:0{_COORD_WIDTH}.{COORD_DECIMALS}f}"
    speed_kn = speed_kmh / 1.852
    body = (
        f"GPRMC,000000.00,A,{lat_field},{ns},{lon_field},{ew},"
        f"{speed_kn:.2f},{heading_deg % 360:.1f},010126,,,A"
    )
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f"${body}*{checksum:02X}\r\n".encode("ascii")
