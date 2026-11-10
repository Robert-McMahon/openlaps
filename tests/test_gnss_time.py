"""RMC time to chrony SOCK samples: validity gates, sign, and failure isolation."""

import calendar
import sys
import threading
from pathlib import Path

import pytest

from collectors.serial.gnss_time import (
    SOCK_SAMPLE,
    ChronyNmeaFeed,
    pack_time_sample,
    rmc_utc_second,
)
from collectors.serial.transport import SerialCollector
from core.config import SerialConfig

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import chrony_sock  # noqa: E402

NS = 1_000_000_000
UTC_S = calendar.timegm((2026, 11, 10, 13, 16, 21))


def _rmc(clock: str = "131621.00", status: str = "A", date: str = "101126") -> bytes:
    body = f"GNRMC,{clock},{status},3145.8100,S,11548.7500,E,0.05,0.0,{date},,,D,V"
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f"${body}*{checksum:02X}\r\n".encode()


def test_a_valid_top_of_second_rmc_names_its_utc_second():
    assert rmc_utc_second(_rmc()) == UTC_S


@pytest.mark.parametrize(
    "line",
    [
        _rmc(clock="131621.02"),  # one of the other 49 fixes that second
        _rmc(status="V"),  # receiver says the fix is not valid
        _rmc(date="060180"),  # a receiver that has never had a fix
        _rmc(date=""),
        _rmc(clock="256199.00"),
        _rmc()[:-6] + b"*00\r\n",  # checksum mismatch
        b"$GNGGA,131621.00,3145.81,S,11548.75,E,4,30,0.5,20.0,M,-30.0,M,1.0,0000*5B\r\n",
        b"\xff\xfe garbage\r\n",
    ],
)
def test_anything_else_is_not_a_time_source(line):
    assert rmc_utc_second(line) is None


def test_the_sample_is_byte_identical_to_the_shims_packing():
    arrival_ns = UTC_S * NS + 43_210_000

    assert pack_time_sample(UTC_S * NS, arrival_ns) == chrony_sock.pack_sock_sample(
        UTC_S, arrival_ns
    )
    assert SOCK_SAMPLE.format == chrony_sock.SOCK_SAMPLE.format


def test_a_clock_running_twelve_seconds_fast_yields_a_negative_offset():
    """The 2026-11-10 failure: the clock read 12 s ahead of the fix it stamped."""
    arrival_ns = (UTC_S + 12) * NS + 40_000_000

    tv_sec, tv_usec, offset_s, *_ = SOCK_SAMPLE.unpack(pack_time_sample(UTC_S * NS, arrival_ns))

    assert (tv_sec, tv_usec) == (UTC_S + 12, 40_000)
    assert offset_s == pytest.approx(-12.040)


def test_the_feed_sends_one_sample_per_second_and_reports_latency():
    sent: list[bytes] = []
    feed = ChronyNmeaFeed("/nowhere", sink=sent.append)

    assert feed.handle_line(_rmc(), UTC_S * NS + 45_000_000) is True
    assert feed.handle_line(_rmc(clock="131621.02"), UTC_S * NS + 65_000_000) is False

    assert len(sent) == 1
    assert feed.stats.samples_sent == 1
    assert feed.stats.last_latency_ms == pytest.approx(45.0)


def test_an_absent_chrony_is_counted_and_never_raises(tmp_path):
    feed = ChronyNmeaFeed(str(tmp_path / "no-such.sock"))

    assert feed.handle_line(_rmc(), UTC_S * NS) is False
    assert feed.handle_line(_rmc(), UTC_S * NS) is False

    assert feed.stats.send_failures == 2
    assert feed.stats.samples_sent == 0
    feed.close()


def test_the_collector_offers_lines_to_the_feed_and_still_emits_samples():
    stop = threading.Event()
    sent: list[bytes] = []
    emitted = []

    class Port:
        lines = [_rmc()]

        def readline(self, size: int = -1) -> bytes:
            if self.lines:
                return self.lines.pop(0)
            stop.set()
            return b""

        def close(self) -> None:
            pass

    collector = SerialCollector(
        SerialConfig(name="serial0", port="/dev/null", baud=115_200, decoder="nmea"),
        emitted.append,
        serial_factory=lambda config: Port(),
        time_feed=ChronyNmeaFeed("/nowhere", sink=sent.append),
    )
    collector.run(stop)

    assert len(sent) == 1
    assert len(emitted) == 5
    tv_sec, _, offset_s, *_ = SOCK_SAMPLE.unpack(sent[0])
    # Stamped with the realtime clock at arrival, against the RMC's second.
    assert tv_sec + offset_s == pytest.approx(UTC_S, abs=1.0)
