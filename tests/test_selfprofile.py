"""The agent's built-in sampling profiler."""

import sys
import threading

from agent.selfprofile import _Window


def test_a_window_separates_working_samples_from_parked_ones():
    parked = threading.Event()
    release = threading.Event()

    def worker() -> None:
        parked.set()
        release.wait()

    thread = threading.Thread(target=worker, name="can-can0")
    thread.start()
    parked.wait()
    try:
        window = _Window()
        frames = sys._current_frames()
        for _ in range(3):
            window.add("can0", frames[thread.ident])  # in Event.wait: idle
            window.add("pipeline", sys._getframe())  # this test: working
    finally:
        release.set()
        thread.join()

    report = window.report(1, 30.0)
    assert window.idle["can0"] == 3
    assert "[pipeline] busy in 100.0% of 3 samples" in report
    assert "test_a_window_separates_working_samples_from_parked_ones" in report
    assert "[can0]" not in report
