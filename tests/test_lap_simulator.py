"""tools/lap_simulator.py: path geometry, and simulated laps through the
real Pipeline/LapTimingApp -- no NATS involved, so no docker required.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import lap_simulator as sim  # noqa: E402

from agent.clock import SteeredClock  # noqa: E402
from core.catalog import build_runtime_catalog  # noqa: E402
from core.config import load_profile  # noqa: E402
from core.pb import telemetry_pb2 as pb  # noqa: E402


@pytest.fixture
def profile():
    return load_profile(sim.DEFAULT_PROFILE)


@pytest.fixture
def track_and_path(profile):
    track = sim.resolve_track(profile, None)
    start_finish = next(line for line in track.lines if line.line_type.value == "start_finish")
    frame = sim.Frame(
        (start_finish.start.lat + start_finish.end.lat) / 2,
        (start_finish.start.lon + start_finish.end.lon) / 2,
    )
    path, length = sim.build_path(track.lines, frame, track.length_m)
    return track, frame, path, length


def _lap_completed_events(batches, catalog) -> list[dict]:
    names = {channel.id: channel.name for channel in catalog.registry.channels}
    events = []
    for batch_meta in batches:
        if batch_meta.source_class != "derived":
            continue
        batch = pb.SampleBatch()
        batch.ParseFromString(batch_meta.payload)
        for sample in batch.samples:
            if names.get(sample.channel_id) == "lap.event":
                payload = json.loads(sample.s)
                if payload["type"] == "lap_completed":
                    events.append(payload)
    return events


def test_dry_run_path_length_matches_the_track_within_a_few_percent(track_and_path):
    track, _frame, _path, length = track_and_path
    assert length == pytest.approx(track.length_m, rel=0.03)


def test_dry_run_cli_exits_clean_without_touching_nats(capsys):
    exit_code = sim.main(["--dry-run"])
    assert exit_code == 0
    assert "path" in capsys.readouterr().err


def test_ten_simulated_laps_produce_ten_valid_varying_lap_completions(
    profile, track_and_path, tmp_path: Path
):
    track, frame, path, _length = track_and_path
    catalog = build_runtime_catalog(profile, state_path=tmp_path / "registry-state.json")
    clock = SteeredClock()

    batches, reported_times = sim.simulate(
        profile,
        catalog,
        clock,
        track=track,
        frame=frame,
        path=path,
        label="Wanneroo_sim",
        laps=10,
        rate_hz=20.0,
        seed=42,
    )

    completed = _lap_completed_events(batches, catalog)
    assert len(completed) == 10
    assert len(reported_times) == 10
    assert all(event["valid"] for event in completed)
    assert all(event["track_name"] == "Wanneroo_sim" for event in completed)

    lap_times = [event["lap_time"] for event in completed]
    assert all(30.0 < t < 120.0 for t in lap_times)
    assert len(set(round(t, 1) for t in lap_times)) > 1  # laps vary, not identical

    lap_numbers = [event["lap_number"] for event in completed]
    assert lap_numbers == sorted(lap_numbers)
    assert len(set(lap_numbers)) == 10
