"""Receiver-epoch stamping of NMEA sentences (``collectors.serial.fix_time``)."""

from __future__ import annotations

import random

from collectors.serial.fix_time import ReceiverClock, time_of_day_s
from collectors.serial.transport import SerialCollector
from core.config import SerialConfig
from core.samples import Sample

MS = 1_000_000
EPOCH_NS = 20 * MS
LATENCY_NS = 25 * MS
T0_S = 3 * 3600 + 25 * 60 + 10  # 03:25:10 UTC
BOOT_NS = 987_654_321_000  # what the monotonic clock read at T0


def _jittered(n: int, seed: int = 1) -> list[tuple[float, int]]:
    """(epoch seconds of day, arrival ns) for a 50 Hz stream read with jitter."""
    rng = random.Random(seed)
    out = []
    for k in range(n):
        jitter = 0 if k % 40 == 0 else rng.randrange(0, 30 * MS)
        out.append((T0_S + k * 0.02, BOOT_NS + k * EPOCH_NS + LATENCY_NS + jitter))
    return out


def test_time_of_day_field():
    assert time_of_day_s("032510.00") == T0_S
    assert time_of_day_s("235959.98") == 86399.98
    assert time_of_day_s("000000") == 0.0
    for bad in ("", "0325", "246000.00", "03251a.00", "032510.x"):
        assert time_of_day_s(bad) is None


def test_jitter_is_removed_and_the_fixed_latency_kept():
    clock = ReceiverClock()
    stamps = [clock.stamp(tod, arrival) for tod, arrival in _jittered(500)]
    truth = [BOOT_NS + k * EPOCH_NS + LATENCY_NS for k in range(500)]
    assert stamps == truth  # the first arrival here has no jitter
    assert all(s <= a for s, (_, a) in zip(stamps, _jittered(500), strict=True))
    assert clock.stats.max_delay_ms > 20.0


def test_a_late_start_is_corrected_without_reordering():
    clock = ReceiverClock()
    true = [BOOT_NS + k * EPOCH_NS + LATENCY_NS for k in range(50)]
    # The reader was held off for the first three sentences and got them at once.
    arrivals = [max(t, true[0] + 45 * MS) for t in true]
    stamps = [clock.stamp(T0_S + k * 0.02, a) for k, a in enumerate(arrivals)]
    assert stamps == sorted(stamps)
    assert all(s <= a for s, a in zip(stamps, arrivals, strict=True))
    assert stamps[3:] == true[3:]


def test_sentences_of_one_epoch_share_a_stamp():
    clock = ReceiverClock()
    clock.stamp(T0_S, BOOT_NS + LATENCY_NS)
    rmc = clock.stamp(T0_S + 0.02, BOOT_NS + EPOCH_NS + LATENCY_NS + 3 * MS)
    gga = clock.stamp(T0_S + 0.02, BOOT_NS + EPOCH_NS + LATENCY_NS + 9 * MS)
    assert rmc == gga == BOOT_NS + EPOCH_NS + LATENCY_NS


def test_midnight_does_not_break_the_mapping():
    clock = ReceiverClock()
    start = 86400 - 1.0
    stamps = [
        clock.stamp((start + k * 0.02) % 86400, BOOT_NS + k * EPOCH_NS + LATENCY_NS)
        for k in range(100)
    ]
    assert [b - a for a, b in zip(stamps, stamps[1:], strict=False)] == [EPOCH_NS] * 99
    assert clock.stats.resets == 0


def test_receiver_time_stepping_back_starts_again():
    clock = ReceiverClock()
    clock.stamp(T0_S, BOOT_NS + LATENCY_NS)
    arrival = BOOT_NS + 5_000 * MS
    assert clock.stamp(T0_S - 30.0, arrival) == arrival
    assert clock.stats.resets == 1


def test_slow_clock_drift_is_followed():
    clock = ReceiverClock(window_s=10.0)
    drift = 20e-6  # the board's oscillator runs 20 ppm fast against GPS
    worst = 0
    for k in range(50 * 120):
        true = BOOT_NS + round(k * EPOCH_NS * (1 + drift)) + LATENCY_NS
        jitter = 0 if k % 25 == 0 else 7 * MS
        worst = max(worst, abs(clock.stamp(T0_S + k * 0.02, true + jitter) - true))
    assert worst < 0.3 * MS


def _rmc(tod: str) -> bytes:
    body = f"GNRMC,{tod},A,3343.5000,S,11614.7000,E,54.0,90.0,131126,,,R"
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f"${body}*{checksum:02X}\r\n".encode()


def _collector(samples: list[Sample], **overrides) -> SerialCollector:
    config = SerialConfig(
        name="serial0", port="/dev/ttyUSB9", baud=115_200, decoder="nmea", **overrides
    )
    return SerialCollector(config, samples.append, wall_clock=lambda ns: ns / 1e6)


def test_collector_stamps_fixes_at_their_epoch():
    samples: list[Sample] = []
    collector = _collector(samples)
    for k, jitter_ms in enumerate((0, 11, 4, 17, 0)):
        collector.handle_line(
            _rmc(f"0325{10 + k // 50:02d}.{(k % 50) * 2:02d}"),
            t_mono_ns=BOOT_NS + k * EPOCH_NS + LATENCY_NS + jitter_ms * MS,
        )
    stamps = sorted({s.t_mono_ns for s in samples})
    assert stamps == [BOOT_NS + k * EPOCH_NS + LATENCY_NS for k in range(5)]
    assert {s.t_wall_ms for s in samples} == {ns / 1e6 for ns in stamps}


def test_collector_keeps_arrival_stamps_when_receiver_time_is_off():
    samples: list[Sample] = []
    collector = _collector(samples, receiver_time=False)
    arrivals = [BOOT_NS + k * EPOCH_NS + LATENCY_NS + j * MS for k, j in enumerate((0, 11, 4))]
    for k, arrival in enumerate(arrivals):
        collector.handle_line(_rmc(f"032510.{k * 2:02d}"), t_mono_ns=arrival)
    assert sorted({s.t_mono_ns for s in samples}) == arrivals
