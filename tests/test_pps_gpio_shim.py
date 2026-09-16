"""tools/pps_gpio_shim.py: a PPS edge becomes a chrony sample, or a counter.

Three things can go wrong here and each is checked separately.

**The ABI.** This tool talks to the kernel's GPIO v2 character device by
hand-built `ctypes` structures, because Debian 12's libgpiod is the v1 API
whose timestamps are CLOCK_MONOTONIC. A struct whose size disagrees with the
kernel's fails the ioctl with EINVAL at the worst possible moment, so the
sizes are pinned here as literals from `include/uapi/linux/gpio.h`.

**The arithmetic.** A PPS edge says when a second starts, never which second
it is; this shim takes that from the system clock by rounding. Getting the
sign wrong disciplines the clock the wrong way, and getting the guard wrong
lets a whole second of error through as a confident sample.

**The kernel path.** `test_gpio_sim_*` drives a simulated GPIO and reads the
events back through the real cdev, so `open_line` and `parse_events` are
exercised against a kernel rather than against my reading of the header. It
needs `gpio-sim` and root and skips without them:

    sudo modprobe gpio-sim && sudo -E env "PATH=$PATH" uv run pytest tests/test_pps_gpio_shim.py
"""

from __future__ import annotations

import ctypes
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import pps_gpio_shim as shim  # noqa: E402
from chrony_sock import SOCK_MAGIC, SOCK_SAMPLE  # noqa: E402

NS_PER_S = 1_000_000_000
CONFIGFS = Path("/sys/kernel/config/gpio-sim")


def _event(timestamp_ns: int, event_id: int = 1, line_seqno: int = 1) -> bytes:
    """One `struct gpio_v2_line_event` as the kernel would write it."""
    return shim.LINE_EVENT.pack(timestamp_ns, event_id, 0, line_seqno, line_seqno, b"\x00" * 24)


def _unpack(sample: bytes) -> tuple[int, int, float, int]:
    tv_sec, tv_usec, offset, _, _, _, magic = SOCK_SAMPLE.unpack(sample)
    return tv_sec, tv_usec, offset, magic


# -- the ABI -----------------------------------------------------------------


def test_the_structures_are_the_sizes_the_kernel_expects():
    """From include/uapi/linux/gpio.h. A mismatch is EINVAL on the ioctl."""
    assert ctypes.sizeof(shim._LineRequest) == 592
    assert ctypes.sizeof(shim._LineConfig) == 272
    assert ctypes.sizeof(shim._LineConfigAttribute) == 24
    assert shim.LINE_EVENT.size == 48


def test_the_ioctl_number_is_the_one_gpio_v2_get_line_uses():
    """_IOWR(0xB4, 0x07, struct gpio_v2_line_request), computed the long way."""
    assert shim.GPIO_V2_GET_LINE_IOCTL == (3 << 30) | (592 << 16) | (0xB4 << 8) | 0x07


def test_the_request_asks_for_realtime_stamps_on_an_input():
    """The whole reason for hand-rolling v2 rather than using libgpiod 1.6."""
    flags = (
        shim.GPIO_V2_LINE_FLAG_INPUT
        | shim.GPIO_V2_LINE_FLAG_EVENT_CLOCK_REALTIME
        | shim.EDGE_FLAGS["rising"]
    )

    assert flags == 4 | 2048 | 16
    assert shim.GPIO_V2_LINE_FLAG_EVENT_CLOCK_REALTIME == 1 << 11


def test_events_are_split_on_the_struct_boundary():
    buffer = _event(1_000_000_000, line_seqno=1) + _event(2_000_000_000, line_seqno=2)

    assert shim.parse_events(buffer) == [(1_000_000_000, 1, 1), (2_000_000_000, 1, 2)]


def test_a_truncated_trailing_event_is_ignored_rather_than_misread():
    buffer = _event(1_000_000_000) + b"\x00" * 7

    assert shim.parse_events(buffer) == [(1_000_000_000, 1, 1)]


# -- the arithmetic ----------------------------------------------------------


def test_an_edge_on_the_second_is_a_zero_offset_sample():
    sent: list[bytes] = []
    pps = shim.PpsShim(sent.append)

    assert pps.process_edge(1_700_000_000 * NS_PER_S) is True

    tv_sec, tv_usec, offset, magic = _unpack(sent[0])
    assert (tv_sec, tv_usec) == (1_700_000_000, 0)
    assert offset == pytest.approx(0.0)
    assert magic == SOCK_MAGIC


def test_a_fast_clock_produces_a_negative_offset():
    """Sign convention, and it is the one thing here that must not be guessed.

    chrony's SOCK offset is the correction to apply: positive means the local
    clock is behind. A clock running 3 ms fast stamps a true second boundary
    at N.003, so the sample must say -3 ms.
    """
    sent: list[bytes] = []
    pps = shim.PpsShim(sent.append)

    pps.process_edge(1_700_000_000 * NS_PER_S + 3_000_000)

    tv_sec, _, offset, _ = _unpack(sent[0])
    assert tv_sec == 1_700_000_000
    assert offset == pytest.approx(-0.003, abs=1e-9)


def test_a_slow_clock_produces_a_positive_offset():
    sent: list[bytes] = []
    pps = shim.PpsShim(sent.append)

    pps.process_edge(1_700_000_001 * NS_PER_S - 3_000_000)

    tv_sec, tv_usec, offset, _ = _unpack(sent[0])
    # `sock_sample` carries the *arrival* time plus the correction, not the
    # reference second: the edge really did arrive at N.997 by this clock,
    # and it belongs to second N+1, so the clock is 3 ms slow.
    assert (tv_sec, tv_usec) == (1_700_000_000, 997_000)
    assert offset == pytest.approx(0.003, abs=1e-9)


def test_an_edge_past_the_guard_is_dropped_rather_than_sent():
    """Past +/-0.5 s the rounding picks the wrong second entirely.

    The sample would then be wrong by a whole second while looking perfectly
    well-formed, which is the one failure mode that would step a race car's
    clock mid-session. Dropping it shows up in `out_of_range` instead.
    """
    sent: list[bytes] = []
    pps = shim.PpsShim(sent.append, max_offset_s=0.2)

    assert pps.process_edge(1_700_000_000 * NS_PER_S + 300_000_000) is False
    assert sent == []
    assert pps.stats.out_of_range == 1
    assert pps.stats.accepted == 0


def test_the_guard_is_symmetric():
    pps = shim.PpsShim(lambda _: None, max_offset_s=0.2)

    assert pps.process_edge(1_700_000_001 * NS_PER_S - 300_000_000) is False
    assert pps.stats.out_of_range == 1


def test_the_other_edge_is_counted_not_used():
    """A line configured for rising edges must never sample a falling one."""
    pps = shim.PpsShim(lambda _: None, edge="rising")

    assert pps.process_edge(1_700_000_000 * NS_PER_S, shim.GPIO_V2_LINE_EVENT_FALLING_EDGE) is False
    assert pps.stats.wrong_edge == 1


def test_a_falling_edge_shim_accepts_falling_edges():
    pps = shim.PpsShim(lambda _: None, edge="falling")

    assert pps.process_edge(1_700_000_000 * NS_PER_S, shim.GPIO_V2_LINE_EVENT_FALLING_EDGE) is True
    assert pps.stats.accepted == 1


def test_a_dead_chrony_socket_is_a_counter_not_a_crash():
    """chronyd restarting must not take the shim down with it."""

    def refuse(_: bytes) -> None:
        raise OSError("no such file or directory")

    pps = shim.PpsShim(refuse)

    assert pps.process_edge(1_700_000_000 * NS_PER_S) is False
    assert pps.stats.sink_errors == 1
    assert pps.stats.accepted == 0


# -- the kernel path ---------------------------------------------------------


def _gpio_sim_available() -> bool:
    if os.geteuid() != 0:
        return False
    if not CONFIGFS.is_dir():
        subprocess.run(["modprobe", "gpio-sim"], check=False, capture_output=True)
    return CONFIGFS.is_dir()


needs_gpio_sim = pytest.mark.skipif(
    not _gpio_sim_available(), reason="needs root and the gpio-sim module"
)


@pytest.fixture
def simulated_line():
    """A live one-line gpio-sim chip, yielded as ``(chip device, offset)``."""
    device = CONFIGFS / f"openlaps{os.getpid()}"
    bank = device / "bank0"
    bank.mkdir(parents=True)
    (bank / "num_lines").write_text("1")
    (device / "live").write_text("1")
    try:
        chip_name = (bank / "chip_name").read_text().strip()
        platform = next(Path("/sys/devices/platform").glob("gpio-sim.*"))
        pull = next(platform.glob("*/sim_gpio0/pull"))
        yield f"/dev/{chip_name}", 0, pull
    finally:
        (device / "live").write_text("0")
        bank.rmdir()
        device.rmdir()


@needs_gpio_sim
def test_gpio_sim_edges_arrive_as_realtime_stamped_events(simulated_line):
    """`open_line` and `parse_events` against a real kernel, not a header read."""
    chip, offset, pull = simulated_line
    fd = shim.open_line(chip, offset, edge="rising", bias="as-is")
    try:
        before = time.clock_gettime_ns(time.CLOCK_REALTIME)
        pull.write_text("pull-up")
        buffer = os.read(fd, shim.LINE_EVENT.size * 4)
        after = time.clock_gettime_ns(time.CLOCK_REALTIME)
    finally:
        os.close(fd)

    events = shim.parse_events(buffer)
    assert len(events) == 1
    timestamp_ns, event_id, _seqno = events[0]
    assert event_id == shim.GPIO_V2_LINE_EVENT_RISING_EDGE
    # The stamp is realtime, not monotonic: it lands inside the window we
    # just measured with CLOCK_REALTIME. Monotonic would be wildly outside.
    assert before <= timestamp_ns <= after


@needs_gpio_sim
def test_gpio_sim_edges_become_chrony_samples(simulated_line):
    chip, offset, pull = simulated_line
    sent: list[bytes] = []
    pps = shim.PpsShim(sent.append, max_offset_s=0.5)
    fd = shim.open_line(chip, offset, edge="rising")
    try:
        pull.write_text("pull-up")
        for timestamp_ns, event_id, _ in shim.parse_events(os.read(fd, shim.LINE_EVENT.size * 4)):
            pps.process_edge(timestamp_ns, event_id)
    finally:
        os.close(fd)

    # A synthetic edge lands wherever it lands within the second, so the only
    # claim is that a well-formed sample was produced -- the offsets above
    # pin the arithmetic.
    assert pps.stats.accepted + pps.stats.out_of_range == 1
    if sent:
        assert _unpack(sent[0])[3] == SOCK_MAGIC


def test_the_shim_is_importable_without_the_kernel_interface():
    """It has to load on a workstation for the tests above to mean anything."""
    assert struct.calcsize("=QIIII24s") == shim.LINE_EVENT.size
