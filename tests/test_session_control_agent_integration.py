"""Session-control to vehicle timing integration through real JetStream."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from conftest import EXAMPLE_PROFILE, lap_path, wanneroo_track

from agent.publisher import JetStreamPublisher
from agent.timing_app import build_lap_timing_app
from core.catalog import build_runtime_catalog
from core.samples import Sample
from pit.session_control.publisher import LatestSessionPublisher

VEHICLE = "example-club-racer"


def test_published_session_is_stamped_onto_lap_events(nats_url, tmp_path: Path):
    catalog = build_runtime_catalog(
        EXAMPLE_PROFILE,
        state_path=tmp_path / "registry-state.json",
        created_unix_ms=1,
    )
    app = build_lap_timing_app(
        catalog,
        "position.*",
        "Wanneroo",
        str(EXAMPLE_PROFILE / "tracks"),
    )
    received: list[dict[str, object]] = []

    def apply(payload: bytes) -> None:
        state = json.loads(payload)
        received.append(state)
        app.apply_session(state)

    agent = JetStreamPublisher(
        nats_url=nats_url,
        vehicle_id=VEHICLE,
        registry_payload=catalog.registry.SerializeToString(),
        registry_interval_s=3600,
        on_session=apply,
    )
    agent.start()
    try:
        _wait_until(lambda: agent.registry_publishes >= 1)
        asyncio.run(_publish_session(nats_url, received))
        events = _feed_lap(app, catalog)
    finally:
        agent.stop()

    lap = next(event for event in events if event["type"] == "lap_completed")
    assert lap["session_id"] == "session-7"
    assert lap["driver"] == "Driver A"
    assert lap["stint_number"] == 1
    assert lap["session_type"] == "race"
    assert lap["track_name"] == "Wanneroo"


async def _publish_session(nats_url: str, received: list[dict[str, object]]) -> None:
    publisher = LatestSessionPublisher(nats_url, VEHICLE, domain=None)
    stop = asyncio.Event()
    task = asyncio.create_task(publisher.run(stop))
    publisher.submit(
        {
            "session_id": "session-7",
            "session_type": "race",
            "driver": "Driver A",
            "track_name": "Wanneroo",
            "car": "Car 7",
            "stint_number": 1,
            "session_start": 1_782_900_000_000,
            "stint_start": 1_782_900_000_000,
            "status": "active",
            "timestamp": 1_782_900_000_000,
        }
    )
    try:
        deadline = asyncio.get_running_loop().time() + 5
        while not received:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("agent did not receive session")
            await asyncio.sleep(0.01)
    finally:
        stop.set()
        await task


def _feed_lap(app, catalog) -> list[dict[str, object]]:
    lat_id = catalog.channel_ids["position.lat"]
    lon_id = catalog.channel_ids["position.lon"]
    events: list[dict[str, object]] = []
    for t_s, lat, lon in lap_path(wanneroo_track(), laps=1):
        t_ns = int(t_s * 1e9)
        wall_ms = 1_780_000_000_000 + t_s * 1000
        samples = app.observe(lat_id, Sample("serial0:um980.RMC.lat", t_ns, wall_ms, lat))
        samples += app.observe(lon_id, Sample("serial0:um980.RMC.lon", t_ns, wall_ms, lon))
        events.extend(
            json.loads(sample.value)
            for sample in samples
            if sample.source_ref == "derived:lap.event"
        )
    return events


def _wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("condition not met")
        time.sleep(0.02)
