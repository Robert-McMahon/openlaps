"""RP2040 GNSS receiver interface protocol and chrony SOCK shim tests."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import gnss_receiver_interface_shim as shim  # noqa: E402


def test_valid_message_becomes_a_full_chrony_offset_sample():
    sink: list[bytes] = []
    clock = shim.GnssReceiverInterfaceShim(sink.append)
    arrival_ns = 1_780_000_000_020_000_000

    assert clock.process_line("TH1 42 1780000000 9000000 250 1", arrival_ns) is True

    assert len(sink) == 1
    tv_sec, tv_usec, offset, pulse, leap, padding, magic = struct.unpack("@lldiiii", sink[0])
    assert (tv_sec, tv_usec) == (1_780_000_000, 19_750)
    assert offset == pytest.approx(-0.01975)
    assert (pulse, leap, padding, magic) == (0, 0, 0, shim.SOCK_MAGIC)
    assert clock.stats.accepted == 1


def test_bad_invalid_and_stale_lines_degrade_to_silence():
    sink: list[bytes] = []
    clock = shim.GnssReceiverInterfaceShim(sink.append)
    now = 1_780_000_000_020_000_000

    assert clock.process_line("not a timing message", now) is False
    assert clock.process_line("TH1 1 1780000000 9000000 250 0", now) is False
    assert clock.process_line("TH1 2 1780000000 9000000 250 1", now) is True
    assert clock.process_line("TH1 2 1780000001 10000000 250 1", now) is False
    assert clock.process_line("TH1 1 1780000002 11000000 250 1", now) is False

    assert len(sink) == 1
    assert clock.stats.malformed == 1
    assert clock.stats.invalid == 1
    assert clock.stats.stale_sequence == 2


def test_sequence_wrap_is_newer_but_impossible_firmware_delay_is_rejected():
    sink: list[bytes] = []
    clock = shim.GnssReceiverInterfaceShim(sink.append)
    now = 1_780_000_000_020_000_000

    assert clock.process_line("TH1 4294967295 1780000000 1 1 1", now) is True
    assert clock.process_line("TH1 0 1780000001 2 1 1", now) is True
    assert clock.process_line("TH1 1 1780000002 3 900001 1", now) is False
    assert clock.stats.invalid == 1
