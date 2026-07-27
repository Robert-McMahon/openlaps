"""Clock discipline tests: monotonic truth, GNSS step-then-slew wall mapping."""

import time

import pytest

from agent.clock import SOURCE_GNSS, SOURCE_SYSTEM, SteeredClock

NS_PER_S = 1_000_000_000


def test_initial_mapping_tracks_the_system_clock():
    clock = SteeredClock()
    t_mono_ns = time.monotonic_ns()
    expected_wall_ms = time.time() * 1000.0
    assert clock.source == SOURCE_SYSTEM
    assert clock(t_mono_ns) == pytest.approx(expected_wall_ms, abs=50.0)


def test_mapping_is_affine_in_monotonic_time():
    clock = SteeredClock()
    base = time.monotonic_ns()
    assert clock(base + NS_PER_S) - clock(base) == pytest.approx(1000.0)


def test_first_gnss_observation_steps_even_while_running():
    clock = SteeredClock()
    clock.mark_running()
    t_mono_ns = 50 * NS_PER_S
    gnss_ms = 1_780_000_000_000.0
    clock.observe_gnss(t_mono_ns, gnss_ms)
    assert clock.source == SOURCE_GNSS
    assert clock(t_mono_ns) == pytest.approx(gnss_ms)


def test_running_observations_slew_bounded_never_step():
    clock = SteeredClock(max_slew_ppm=500.0)
    clock.observe_gnss(0, 1_000_000.0)  # startup step: offset now exact
    clock.mark_running()
    # One second later GNSS says the wall is a whole second ahead of the
    # mapping. 500 ppm over 1 s allows only 0.5 ms of correction.
    clock.observe_gnss(1 * NS_PER_S, 1_002_000.0)
    assert clock(1 * NS_PER_S) == pytest.approx(1_001_000.5)
    # And the error keeps shrinking monotonically on later observations.
    clock.observe_gnss(2 * NS_PER_S, 1_003_000.0)
    assert clock(2 * NS_PER_S) == pytest.approx(1_002_001.0)


def test_slew_moves_backward_offsets_without_stepping():
    clock = SteeredClock(max_slew_ppm=100.0)
    clock.observe_gnss(0, 5_000_000.0)
    clock.mark_running()
    clock.observe_gnss(10 * NS_PER_S, 5_009_990.0)  # GNSS 10 ms behind mapping
    assert clock(10 * NS_PER_S) == pytest.approx(5_009_999.0)  # moved 1 ms max


def test_small_errors_are_absorbed_completely():
    clock = SteeredClock(max_slew_ppm=500.0)
    clock.observe_gnss(0, 1_000_000.0)
    clock.mark_running()
    clock.observe_gnss(2 * NS_PER_S, 1_002_000.2)  # 0.2 ms error, 1 ms allowed
    assert clock(2 * NS_PER_S) == pytest.approx(1_002_000.2)


def test_non_monotonic_observation_is_held_not_applied():
    clock = SteeredClock()
    clock.observe_gnss(NS_PER_S, 2_000_000.0)
    clock.mark_running()
    before = clock.offset_ms
    clock.observe_gnss(NS_PER_S, 9_999_999.0)  # same capture instant again
    assert clock.offset_ms == before


def test_invalid_slew_rate_rejected():
    with pytest.raises(ValueError):
        SteeredClock(max_slew_ppm=0)
