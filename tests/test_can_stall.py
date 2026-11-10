"""collectors.can_stall: the definition of a stalled CAN controller.

The 2026-11-08 hang on the Luckfox: the kernel counted frames on can0 for
hours while delivering none of them to any socket. The detector must call
that a stall, must *not* call a quiet bus (both counters flat) a stall, and
must clear the instant a frame gets through.
"""

from __future__ import annotations

from pathlib import Path

from collectors.can_stall import StallDetector, read_rx_packets


def test_kernel_counting_frames_nobody_receives_is_a_stall():
    detector = StallDetector()
    assert detector.observe(1000, 500, 10.0) == 0.0  # baseline
    assert detector.observe(1100, 500, 11.0) == 1.0
    assert detector.observe(1200, 500, 12.0) == 2.0
    assert detector.stalled


def test_a_quiet_bus_is_not_a_stall():
    detector = StallDetector()
    detector.observe(1000, 500, 10.0)
    assert detector.observe(1000, 500, 11.0) == 0.0
    assert detector.observe(1000, 500, 12.0) == 0.0
    assert not detector.stalled


def test_a_delivered_frame_ends_the_stall():
    detector = StallDetector()
    detector.observe(1000, 500, 10.0)
    detector.observe(1100, 500, 11.0)
    detector.observe(1200, 500, 12.0)
    assert detector.observe(1300, 501, 13.0) == 0.0
    assert not detector.stalled
    # ...and a new stall starts counting from zero again.
    assert detector.observe(1400, 501, 14.0) == 1.0


def test_a_healthy_bus_where_both_counters_rise_never_stalls():
    detector = StallDetector()
    for second in range(60):
        assert detector.observe(1000 + 1500 * second, 1000 + 1500 * second, float(second)) == 0.0


def test_an_unreadable_counter_cannot_be_judged():
    detector = StallDetector()
    detector.observe(1000, 500, 10.0)
    detector.observe(1100, 500, 11.0)
    assert detector.observe(None, 500, 12.0) == 0.0
    assert not detector.stalled
    # And it does not resume the old stall from a stale baseline.
    assert detector.observe(1200, 500, 13.0) == 0.0
    assert detector.observe(1300, 500, 14.0) == 1.0


def test_a_counter_that_went_backwards_resets_rather_than_stalls():
    """An interface re-created by the driver starts its counters at zero."""
    detector = StallDetector()
    detector.observe(5000, 500, 10.0)
    assert detector.observe(10, 500, 11.0) == 0.0


def test_reset_forgets_the_baseline():
    detector = StallDetector()
    detector.observe(1000, 500, 10.0)
    detector.observe(1100, 500, 11.0)
    detector.reset()
    assert detector.observe(1200, 500, 12.0) == 0.0


def test_read_rx_packets_reads_the_interface_s_sysfs_counter(tmp_path: Path):
    stats = tmp_path / "class/net/can0/statistics"
    stats.mkdir(parents=True)
    (stats / "rx_packets").write_text("12345\n")
    assert read_rx_packets("can0", tmp_path) == 12345
    assert read_rx_packets("can1", tmp_path) is None
    (stats / "rx_packets").write_text("not a number")
    assert read_rx_packets("can0", tmp_path) is None
