"""Per-thread CPU accounting for the agent's health channels."""

import threading
import time

from agent.thread_cpu import OTHER, ThreadCpu, role_of


def test_thread_names_map_to_the_role_they_are_reported_under():
    assert role_of("MainThread") == "main"
    assert role_of("can-can0") == "can0"
    assert role_of("serial-serial0") == "serial0"
    assert role_of("pipeline") == "pipeline"


def test_a_busy_thread_shows_under_its_role_and_an_unknown_one_under_other():
    stop = threading.Event()

    def spin() -> None:
        while not stop.is_set():
            sum(range(2000))

    known = threading.Thread(target=spin, name="can-can0", daemon=True)
    stranger = threading.Thread(target=spin, name="somebody-else", daemon=True)
    cpu = ThreadCpu(["can0", "pipeline"])
    known.start()
    stranger.start()
    try:
        assert set(cpu.observe()) == {"can0", "pipeline", OTHER}
        time.sleep(0.5)
        used = cpu.observe()
    finally:
        stop.set()
        known.join()
        stranger.join()

    assert used["can0"] > 5
    assert used[OTHER] > 5
    assert used["pipeline"] == 0


def test_stat_descriptors_are_reused_and_dropped_when_a_thread_ends():
    stop = threading.Event()
    worker = threading.Thread(target=stop.wait, name="can-can0", daemon=True)
    cpu = ThreadCpu(["can0"])
    worker.start()
    try:
        cpu.observe()
        held = dict(cpu._stat_fds)
        cpu.observe()
        assert worker.native_id in cpu._stat_fds
        assert cpu._stat_fds == held  # same descriptors, not reopened
    finally:
        stop.set()
        worker.join()
    cpu.observe()
    assert worker.native_id not in cpu._stat_fds
