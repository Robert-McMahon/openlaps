"""Clock discipline tests: monotonic ordering, system-clock wall authority."""

import time

import pytest

from agent.clock import SystemClock

NS_PER_S = 1_000_000_000


def test_mapping_tracks_the_system_clock():
    clock = SystemClock()
    t_mono_ns = time.monotonic_ns()
    expected_wall_ms = time.time() * 1000.0
    assert clock(t_mono_ns) == pytest.approx(expected_wall_ms, abs=50.0)


def test_mapping_is_affine_in_monotonic_time():
    clock = SystemClock()
    base = time.monotonic_ns()
    assert clock(base + NS_PER_S) - clock(base) == pytest.approx(1000.0)


def test_mapping_follows_a_runtime_system_clock_adjustment(monkeypatch):
    times = iter((10_000_000_000, 10_250_000_000))
    monotonic = iter((5_000_000_000, 6_000_000_000))
    monkeypatch.setattr(time, "time_ns", lambda: next(times))
    monkeypatch.setattr(time, "monotonic_ns", lambda: next(monotonic))
    clock = SystemClock()

    assert clock(5_000_000_000) == pytest.approx(10_000.0)
    # The realtime clock moved 0.25 s while monotonic moved 1 s. A fixed
    # boot-time offset would return 11,000 ms; the system authority returns
    # the chrony-adjusted 10,250 ms.
    assert clock(6_000_000_000) == pytest.approx(10_250.0)
