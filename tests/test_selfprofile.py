"""The agent's built-in profiler."""

import cProfile
import threading
import time

from agent import selfprofile


def _spin(seconds: float) -> int:
    end = time.monotonic() + seconds
    total = 0
    while time.monotonic() < end:
        total += sum(range(200))
    return total


def test_the_report_ranks_working_functions_from_every_thread_and_sets_waits_apart():
    profile = cProfile.Profile()
    worker = threading.Thread(target=_spin, args=(0.2,), name="pipeline")
    profile.enable()
    worker.start()
    time.sleep(0.25)  # this thread only waits
    worker.join()
    profile.disable()

    text = selfprofile.report(profile, 1, 0.25)

    assert "self-profile window 1" in text
    assert "_spin" in text  # seen although it ran on another thread
    own_time = text.split("including callees")[0]
    assert "sleep" not in own_time
