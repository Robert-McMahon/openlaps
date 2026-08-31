"""`tools/bench_gps.py`: sentence construction, cadence, and the pty itself.

The feeder stands in for a receiver that indoors would report a void fix and
so produce no `position.*` at all. Its correctness is therefore load-bearing
for every Phase 4 bandwidth figure: sentences the real NMEA decoder rejects
are indistinguishable, at the far end, from a receiver that is not there.
"""

from __future__ import annotations

import itertools
import sys
import threading
import time
from pathlib import Path

import pytest
import serial

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import bench_gps  # noqa: E402

from collectors.serial.nmea import NmeaDecoder  # noqa: E402
from collectors.serial.transport import SerialCollector  # noqa: E402
from core.config import DriverConfig, SerialConfig, Um980Settings  # noqa: E402

TRACE = Path(__file__).parent / "fixtures" / "gps" / "wanneroo-trace.csv"


def _trace(rows: list[tuple[float, float, float, float, float]]) -> bench_gps.Trace:
    return bench_gps.Trace([row[0] for row in rows], [row[1:] for row in rows])


def _decode(sentence: bytes) -> dict[str, object]:
    return dict(NmeaDecoder("serial0", "um980").decode(sentence))


# -- trace loading and interpolation ------------------------------------------


def test_the_checked_in_trace_loads_and_spans_the_recorded_slice():
    trace = bench_gps.load_trace(TRACE)

    assert len(trace.times) == 2200
    assert trace.duration_s == pytest.approx(109.9, abs=0.5)
    assert all(
        later > earlier for earlier, later in zip(trace.times, trace.times[1:], strict=False)
    )


def test_a_trace_whose_time_goes_backwards_is_rejected(tmp_path: Path):
    path = tmp_path / "backwards.csv"
    path.write_text(
        "t_s,lat,lon,speed_kmh,heading_deg\n0,1,2,3,4\n0.1,1,2,3,4\n0.05,1,2,3,4\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="strictly increase"):
        bench_gps.load_trace(path)


def test_a_single_fix_cannot_be_interpolated(tmp_path: Path):
    path = tmp_path / "one.csv"
    path.write_text("t_s,lat,lon,speed_kmh,heading_deg\n0,1,2,3,4\n", encoding="utf-8")
    with pytest.raises(ValueError, match="at least two fixes"):
        bench_gps.load_trace(path)


def test_interpolation_blends_between_the_bracketing_fixes():
    trace = _trace([(0.0, 10.0, 20.0, 100.0, 10.0), (2.0, 12.0, 24.0, 200.0, 30.0)])

    assert bench_gps.interpolate(trace, 1.0) == pytest.approx((11.0, 22.0, 150.0, 20.0))
    assert bench_gps.interpolate(trace, 0.0) == pytest.approx((10.0, 20.0, 100.0, 10.0))


def test_interpolation_clamps_outside_the_trace():
    trace = _trace([(0.0, 10.0, 20.0, 100.0, 10.0), (2.0, 12.0, 24.0, 200.0, 30.0)])

    assert bench_gps.interpolate(trace, -5.0) == trace.fixes[0]
    assert bench_gps.interpolate(trace, 99.0) == trace.fixes[-1]


def test_heading_takes_the_short_way_round_north():
    """359 deg to 1 deg is two degrees of turn, not 358 pointing south."""
    trace = _trace([(0.0, 0.0, 0.0, 0.0, 359.0), (1.0, 0.0, 0.0, 0.0, 1.0)])

    assert bench_gps.interpolate(trace, 0.5)[3] == pytest.approx(0.0)
    assert bench_gps.interpolate(trace, 0.25)[3] == pytest.approx(359.5)


# -- cadence and wrap-around ---------------------------------------------------


def test_fixes_are_spaced_along_the_traces_own_timeline():
    """`--rate-hz` sets offered load; it must not change how fast the car moves."""
    trace = _trace([(0.0, 0.0, 0.0, 0.0, 0.0), (1.0, 10.0, 0.0, 0.0, 0.0)])

    fixes = list(bench_gps.fixes_at(trace, 4.0, loop=False))

    assert [fix[0] for fix in fixes] == pytest.approx([0.0, 2.5, 5.0, 7.5, 10.0])


def test_a_bounded_run_stops_at_the_end_of_the_trace():
    trace = bench_gps.load_trace(TRACE)

    count = sum(1 for _ in bench_gps.fixes_at(trace, 50.0, loop=False))

    assert count == pytest.approx(trace.duration_s * 50.0, abs=2)


def test_looping_wraps_to_the_start_without_losing_a_tick():
    """The seam is a position jump, but never a cadence glitch."""
    trace = _trace([(0.0, 0.0, 0.0, 0.0, 0.0), (1.0, 10.0, 0.0, 0.0, 0.0)])

    generator = bench_gps.fixes_at(trace, 4.0, loop=True)
    fixes = [next(generator)[0] for _ in range(11)]

    # One full pass is five fixes at 0, 2.5, 5, 7.5, 10; the sixth carries
    # the 0.25 s remainder across the seam rather than restarting at zero.
    assert fixes[:5] == pytest.approx([0.0, 2.5, 5.0, 7.5, 10.0])
    assert fixes[5:10] == pytest.approx([2.5, 5.0, 7.5, 10.0, 2.5])
    assert len(fixes) == 11


# -- sentence construction ------------------------------------------------------


def test_every_sentence_the_feeder_would_send_decodes_cleanly():
    """Against the real decoder, including its checksum and range checks."""
    trace = bench_gps.load_trace(TRACE)
    decoder = NmeaDecoder("serial0", "um980")

    for fix in list(bench_gps.fixes_at(trace, 50.0, loop=False))[:500]:
        assert len(decoder.decode(bench_gps.rmc_sentence(*fix))) == 5

    assert decoder.stats.rmc_sentences == 500
    assert decoder.stats.malformed_sentences == 0


def test_a_corrupted_checksum_is_rejected_by_the_same_decoder():
    """The check above only means something if the decoder is really checking."""
    sentence = bench_gps.rmc_sentence(-31.6725, 115.7815, 120.0, 90.0)
    corrupted = sentence[:-4] + b"00\r\n"

    assert NmeaDecoder("serial0", "um980").decode(corrupted) == []


def test_the_decoded_fix_is_the_fix_that_went_in():
    values = _decode(bench_gps.rmc_sentence(-31.6725, 115.7815, 120.0, 271.5))

    assert values["serial0:um980.RMC.lat"] == pytest.approx(-31.6725, abs=1e-4)
    assert values["serial0:um980.RMC.lon"] == pytest.approx(115.7815, abs=1e-4)
    assert values["serial0:um980.RMC.speed"] == pytest.approx(120.0, abs=0.05)
    assert values["serial0:um980.RMC.heading"] == pytest.approx(271.5, abs=0.05)
    assert values["serial0:um980.RMC.mode"] == "A"


def test_a_heading_of_exactly_360_is_emitted_as_zero():
    """RMC heading is 0 <= h < 360; the decoder rejects 360.0 outright."""
    assert _decode(bench_gps.rmc_sentence(0.0, 0.0, 0.0, 360.0))["serial0:um980.RMC.heading"] == 0.0


# -- the pty ---------------------------------------------------------------------


def test_the_symlink_points_at_the_pty_and_is_removed_on_close(tmp_path: Path):
    link = tmp_path / "gps"
    feeder = bench_gps.PtyFeeder(link)
    try:
        assert link.is_symlink()
        assert Path(link).resolve() == Path(feeder.slave_path)
    finally:
        feeder.close()
    assert not link.exists()


def test_an_existing_symlink_is_replaced(tmp_path: Path):
    link = tmp_path / "gps"
    link.symlink_to(tmp_path / "some-old-pty")

    feeder = bench_gps.PtyFeeder(link)
    try:
        assert Path(link).resolve() == Path(feeder.slave_path)
    finally:
        feeder.close()


def test_a_real_file_is_never_clobbered(tmp_path: Path):
    link = tmp_path / "not-a-link"
    link.write_text("important", encoding="utf-8")

    with pytest.raises(ValueError, match="refusing to replace"):
        bench_gps.PtyFeeder(link)

    assert link.read_text(encoding="utf-8") == "important"


def test_the_feeder_keeps_its_cadence_with_no_reader_at_all(tmp_path: Path):
    """A bench started before the agent must not die, block, or lose the beat.

    Nobody is holding the slave open here, which is the normal state between
    starting the feeder and starting the agent. The feeder must still attempt
    exactly one sentence per tick -- one that blocked, or that caught up in a
    burst afterwards, would offer the wrong load for as long as the agent
    took to come up -- and whatever it does write must be whole sentences,
    since a split one reaches the decoder as a malformed line rather than as
    nothing at all.
    """
    feeder = bench_gps.PtyFeeder(tmp_path / "gps")
    try:
        stats = bench_gps.feed(
            feeder,
            bench_gps.fixes_at(bench_gps.load_trace(TRACE), 200.0, loop=True),
            rate_hz=200.0,
            seconds=1.0,
            stop=threading.Event(),
            status_interval_s=0.0,
        )
    finally:
        feeder.close()

    assert stats.sentences + stats.dropped == pytest.approx(200, rel=0.2)

    # Every byte that made it out belongs to one of the first `sentences`
    # sentences, plus at most the one still in flight when the run ended.
    lengths = [
        len(bench_gps.rmc_sentence(*fix))
        for fix in itertools.islice(
            bench_gps.fixes_at(bench_gps.load_trace(TRACE), 200.0, loop=True),
            stats.sentences + 1,
        )
    ]
    assert sum(lengths[: stats.sentences]) <= stats.bytes_written < sum(lengths)


@pytest.mark.parametrize("rate_hz", [20.0, 50.0])
def test_the_real_serial_collector_reads_the_feeder_at_the_configured_rate(
    tmp_path: Path, rate_hz: float
):
    """End to end over a real pty: feeder -> pyserial -> NMEA decoder -> samples.

    This is the path the agent takes on the bench, hardware aside, and it is
    the one that proves the pty's line discipline does not mangle the "\\r\\n"
    an NMEA sentence ends with.
    """
    link = tmp_path / "gps"
    feeder = bench_gps.PtyFeeder(link)
    stop = threading.Event()
    samples: list[object] = []
    collector = SerialCollector(
        SerialConfig(
            name="serial0",
            port=str(link),
            baud=115_200,
            decoder="nmea",
            driver=DriverConfig(
                name="um980",
                config=Um980Settings(rate_hz=50, sentences=["RMC"], configure_on_start=False),
            ),
        ),
        samples.append,
    )
    collector.start()
    try:
        # Give the collector a moment to open the port; anything written
        # before that is buffered by the pty, not lost.
        time.sleep(0.3)
        started = time.monotonic()
        bench_gps.feed(
            feeder,
            bench_gps.fixes_at(bench_gps.load_trace(TRACE), rate_hz, loop=True),
            rate_hz=rate_hz,
            seconds=2.0,
            stop=stop,
            status_interval_s=0.0,
        )
        elapsed_s = time.monotonic() - started
        time.sleep(0.2)
    finally:
        collector.stop()
        feeder.close()

    assert feeder.stats.dropped == 0
    assert collector.decoder.stats.malformed_sentences == 0
    # Five position.* values per fix; allow a generous margin for a loaded CI
    # host, since the claim under test is "the configured rate", not jitter.
    assert len(samples) / elapsed_s == pytest.approx(5 * rate_hz, rel=0.25)


def test_write_back_from_the_agent_is_drained_rather_than_wedging_the_pty(tmp_path: Path):
    """RTCM the agent forwards has nowhere to go; unread, it would fill the pty."""
    link = tmp_path / "gps"
    feeder = bench_gps.PtyFeeder(link)
    port = serial.Serial(port=str(link), baudrate=115_200, timeout=0.2)
    try:
        port.write(b"\xd3\x00\x13" + b"\x00" * 4096)
        port.flush()
        deadline = time.monotonic() + 5.0
        while feeder.stats.drained_bytes < 4099 and time.monotonic() < deadline:
            feeder.drain()
            time.sleep(0.01)
    finally:
        port.close()
        feeder.close()

    assert feeder.stats.drained_bytes >= 4099
