"""LapTimingApp tests: derived channels from synthetic laps of the fixture track."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import EXAMPLE_PROFILE, lap_path, wanneroo_track

from agent.timing_app import LapTimingApp, build_lap_timing_app
from core.catalog import build_runtime_catalog
from core.samples import Sample


@pytest.fixture
def catalog(tmp_path: Path):
    return build_runtime_catalog(
        EXAMPLE_PROFILE, state_path=tmp_path / "registry-state.json", created_unix_ms=1
    )


@pytest.fixture
def app(catalog):
    return build_lap_timing_app(catalog, "position.*", "Wanneroo", str(EXAMPLE_PROFILE / "tracks"))


def _feed(app: LapTimingApp, catalog, points) -> list[Sample]:
    lat_id = catalog.channel_ids["position.lat"]
    lon_id = catalog.channel_ids["position.lon"]
    derived: list[Sample] = []
    for t_s, lat, lon in points:
        t_mono_ns = int(t_s * 1e9)
        t_wall_ms = 1_780_000_000_000.0 + t_s * 1000.0
        derived += app.observe(lat_id, Sample("serial0:um980.RMC.lat", t_mono_ns, t_wall_ms, lat))
        derived += app.observe(lon_id, Sample("serial0:um980.RMC.lon", t_mono_ns, t_wall_ms, lon))
    return derived


def _by_channel(samples: list[Sample]) -> dict[str, list[Sample]]:
    grouped: dict[str, list[Sample]] = {}
    for sample in samples:
        grouped.setdefault(sample.source_ref.removeprefix("derived:"), []).append(sample)
    return grouped


def test_one_lap_emits_sector_and_lap_events(catalog, app):
    derived = _by_channel(_feed(app, catalog, lap_path(wanneroo_track(), laps=1)))

    events = [json.loads(sample.value) for sample in derived["lap.event"]]
    types = [event["type"] for event in events]
    assert types.count("lap_completed") == 1
    assert types.count("sector_completed") == 3
    lap_event = next(event for event in events if event["type"] == "lap_completed")
    assert lap_event["valid"] is True
    assert lap_event["lap_number"] == 1
    assert lap_event["lap_time"] == pytest.approx(93.0, abs=1.0)

    # 0 = timing not yet started (before the first crossing), then laps 1, 2.
    assert [sample.value for sample in derived["lap.number"]] == [0, 1, 2]
    assert derived["lap.last_time"][0].value == pytest.approx(lap_event["lap_time"])
    # Sector progression: 0 before timing starts, then 1 -> 2 -> 3 as crossings land.
    assert [sample.value for sample in derived["lap.sector"]][:4] == [0, 1, 2, 3]


def test_distance_accumulates_and_resets_on_lap_start(catalog, app):
    derived = _by_channel(_feed(app, catalog, lap_path(wanneroo_track(), laps=1)))
    distances = [sample.value for sample in derived["timing.distance"]]
    assert distances and max(distances) > 1000.0
    # After the lap completes, accumulation restarts near zero.
    assert distances[-1] < max(distances)


def test_second_lap_gets_delta_and_predicted_from_the_reference(catalog, app):
    derived = _by_channel(_feed(app, catalog, lap_path(wanneroo_track(), laps=2)))
    deltas = derived.get("timing.delta_best")
    predicted = derived.get("timing.predicted_lap")
    assert deltas, "second lap should produce a live delta vs the reference"
    assert predicted
    # Identical synthetic laps: mid-lap (clear of the reference curve's edge
    # clamping) the delta stays near zero and the prediction near lap time.
    mid_lap = [s.value for s in deltas if 110.0 <= s.t_mono_ns / 1e9 <= 180.0]
    assert mid_lap and max(abs(value) for value in mid_lap) < 1.0
    mid_predicted = [s.value for s in predicted if 110.0 <= s.t_mono_ns / 1e9 <= 180.0]
    assert mid_predicted[-1] == pytest.approx(93.0, abs=2.0)
    assert derived["lap.best_time"][0].value == pytest.approx(93.0, abs=1.0)


def test_session_identity_is_stamped_onto_events(catalog, app):
    app.apply_session(
        {"session_id": "s-1", "driver": "Alice", "stint_number": 2, "extra": "ignored-key"}
    )
    derived = _by_channel(_feed(app, catalog, lap_path(wanneroo_track(), laps=1)))
    event = json.loads(derived["lap.event"][-1].value)
    assert event["session_id"] == "s-1"
    assert event["driver"] == "Alice"
    assert event["stint_number"] == 2
    assert "extra" not in event


def test_unknown_session_track_is_ignored(catalog, app):
    app.apply_session({"track_name": "NoSuchTrack"})
    assert app.track_name == "Wanneroo"
    assert app.stats.track_switches == 0


def test_mismatched_fix_halves_are_counted_not_processed(catalog, app):
    lat_id = catalog.channel_ids["position.lat"]
    lon_id = catalog.channel_ids["position.lon"]
    assert app.observe(lat_id, Sample("serial0:um980.RMC.lat", 1_000, 1.0, -31.0)) == []
    assert app.observe(lon_id, Sample("serial0:um980.RMC.lon", 2_000, 2.0, 115.0)) == []
    assert app.stats.unpaired_fixes == 1
    assert app.stats.fixes == 0


def test_position_glob_must_resolve_lat_and_lon(catalog):
    with pytest.raises(ValueError, match="matches no channels"):
        LapTimingApp(catalog, "position.nothing_*", wanneroo_track())
    with pytest.raises(ValueError, match="exactly one"):
        LapTimingApp(catalog, "position.speed", wanneroo_track())


def test_missing_track_is_fatal(catalog):
    with pytest.raises(ValueError, match="Wanneroo"):
        build_lap_timing_app(catalog, "position.*", "Suzuka", str(EXAMPLE_PROFILE / "tracks"))
