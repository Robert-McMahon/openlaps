"""tools/lap_simulator.py: path geometry, and simulated laps through the
real Pipeline/LapTimingApp -- no NATS involved, so no docker required.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import lap_simulator as sim  # noqa: E402

from collectors.clock import MonotonicWallClock  # noqa: E402
from core.catalog import build_runtime_catalog  # noqa: E402
from core.config import load_profile  # noqa: E402
from core.pb import telemetry_pb2 as pb  # noqa: E402
from timing import timing_core  # noqa: E402


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
    return [event for event in _lap_events(batches, catalog) if event["type"] == "lap_completed"]


def _lap_events(batches, catalog) -> list[dict]:
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
                events.append(payload)
    return events


def test_dry_run_path_length_matches_the_track_within_a_few_percent(track_and_path):
    track, _frame, _path, length = track_and_path
    assert length == pytest.approx(track.length_m, rel=0.03)


def test_the_loop_does_not_begin_on_the_start_finish_line(track_and_path):
    """A first fix sitting *on* the line is a degenerate intersection.

    `segment_intersection` needs 0 <= t <= 1, and a segment starting on the
    line puts t at 0 give or take float64 noise -- so whether the opening
    crossing registers comes down to rounding. It was masked for a long time
    by `rmc_sentence` quantising coordinates to 0.185 m; once that encoder was
    widened for P4.6 the crossing began to be missed and lap 1 came out
    invalid. The loop now starts short of the line so the first segment spans
    it outright.
    """
    track, frame, path, _length = track_and_path
    start_finish = next(line for line in track.lines if line.line_type.value == "start_finish")
    midpoint = frame.to_xy(
        (start_finish.start.lat + start_finish.end.lat) / 2,
        (start_finish.start.lon + start_finish.end.lon) / 2,
    )

    assert math.dist(path[0], midpoint) >= 1.0
    # ...and the line is still a few points along the loop, not skipped past.
    assert min(math.dist(point, midpoint) for point in path[:20]) < 1.0


def test_the_opening_crossing_starts_timing_on_the_start_finish_line(
    profile, track_and_path, tmp_path: Path
):
    """Timing must begin at start/finish, not mid-lap at a sector.

    `_handle_lap_point` marks the first partial lap invalid when timing starts
    anywhere but the line -- correct behaviour, and exactly what a missed
    opening crossing looks like from the outside.
    """
    track, frame, path, _length = track_and_path
    catalog = build_runtime_catalog(profile, state_path=tmp_path / "registry-state.json")

    accepted: list[str] = []
    original = timing_core.TimingEngine._accept

    def record(self, line, hit):
        taken = original(self, line, hit)
        if taken:
            accepted.append(line.name)
        return taken

    timing_core.TimingEngine._accept = record
    try:
        sim.simulate(
            profile,
            catalog,
            MonotonicWallClock(),
            track=track,
            frame=frame,
            path=path,
            label="Wanneroo_sim",
            laps=2,
            rate_hz=20.0,
            seed=42,
        )
    finally:
        timing_core.TimingEngine._accept = original

    assert accepted[0] == "StartFinish"


def test_dry_run_cli_exits_clean_without_touching_nats(capsys):
    exit_code = sim.main(["--dry-run"])
    assert exit_code == 0
    assert "path" in capsys.readouterr().err


def _simulated_pit_events(
    profile, track, frame, tmp_path: Path, pit_stop: str, laps: int
) -> list[tuple[str, str]]:
    """Ordered (type, line) pit events from a pit run wired exactly as the CLI wires it."""
    path, _length, closing_path = sim.build_paths(track.lines, frame, track.length_m, pit_stop)
    catalog = build_runtime_catalog(profile, state_path=tmp_path / f"registry-{pit_stop}.json")

    batches, _reported_times = sim.simulate(
        profile,
        catalog,
        MonotonicWallClock(),
        track=track,
        frame=frame,
        path=path,
        label="Wanneroo_sim",
        laps=laps,
        rate_hz=20.0,
        seed=42,
        closing_path=closing_path,
    )
    return [
        (event["type"], event["line"])
        for event in _lap_events(batches, catalog)
        if event["type"] in {"pit_entry", "pit_exit"}
    ]


@pytest.mark.parametrize("pit_stop", ["refuel", "service"])
def test_simulated_pit_stops_emit_only_the_selected_exact_line_names(
    profile, track_and_path, tmp_path: Path, pit_stop: str
):
    track, frame, _path, _length = track_and_path

    pit_events = _simulated_pit_events(profile, track, frame, tmp_path, pit_stop, laps=1)

    expected = {
        ("pit_entry", f"PitEntry{pit_stop.title()}"),
        ("pit_exit", f"PitExit{pit_stop.title()}"),
    }
    assert set(pit_events) == expected


@pytest.mark.parametrize("pit_stop", ["refuel", "service"])
def test_the_closing_loop_exits_the_pit_without_reentering_it(
    profile, track_and_path, tmp_path: Path, pit_stop: str
):
    """A pit run must close every stop it opens.

    An entry crossing is only ever closed by the *next* loop's exit crossing,
    so the closing loop -- driven purely to supply the final crossings --
    drives an out-lap: through the pit exit, never back through the entry.
    When it drove the full pit path instead, its own entry crossing ended
    every `--pit-stop` run with a dangling open stop, which `v_pit_stops`
    then paired with whatever exit the *next* bench run produced.
    """
    track, frame, _path, _length = track_and_path

    pit_events = _simulated_pit_events(profile, track, frame, tmp_path, pit_stop, laps=2)

    entry = ("pit_entry", f"PitEntry{pit_stop.title()}")
    exit_ = ("pit_exit", f"PitExit{pit_stop.title()}")
    # Loop 0 exits the (never-entered) pit just after start/finish; each timed
    # lap then enters just before the line and the following loop exits just
    # after it. The closing loop supplies the final exit and nothing more.
    assert pit_events == [exit_, entry, exit_, entry, exit_]


def test_cli_accepts_distinct_refuel_and_service_stop_modes():
    parser = sim.build_arg_parser()

    assert parser.parse_args(["--pit-stop", "refuel"]).pit_stop == "refuel"
    assert parser.parse_args(["--pit-stop", "service"]).pit_stop == "service"


def test_ten_simulated_laps_produce_ten_valid_varying_lap_completions(
    profile, track_and_path, tmp_path: Path
):
    track, frame, path, _length = track_and_path
    catalog = build_runtime_catalog(profile, state_path=tmp_path / "registry-state.json")
    clock = MonotonicWallClock()

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
