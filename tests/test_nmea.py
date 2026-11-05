"""NMEA RMC decoding tests using recorded receiver sentences."""

from pathlib import Path

import pytest

from collectors.serial.nmea import NmeaDecoder

RMC_FIXTURE = Path(__file__).parent / "fixtures" / "nmea" / "rmc-samples.nmea"


def test_recorded_rmc_sentences_decode_to_namespaced_normalized_values():
    decoder = NmeaDecoder("serial0", "um980")
    lines = RMC_FIXTURE.read_bytes().splitlines()

    first = dict(decoder.decode(lines[0]))
    second = dict(decoder.decode(lines[1]))

    assert first == {
        "serial0:um980.RMC.lat": pytest.approx(-31.7635),
        "serial0:um980.RMC.lon": pytest.approx(115.8125),
        "serial0:um980.RMC.speed": pytest.approx(0.0926),
        "serial0:um980.RMC.heading": pytest.approx(0.0),
        "serial0:um980.RMC.mode": "D",
    }
    assert second["serial0:um980.RMC.lat"] == pytest.approx(53.3613366667)
    assert second["serial0:um980.RMC.lon"] == pytest.approx(-6.50562)
    assert second["serial0:um980.RMC.speed"] == pytest.approx(0.11112)
    assert second["serial0:um980.RMC.mode"] == "A"
    assert decoder.stats.rmc_sentences == 2
    assert decoder.stats.malformed_sentences == 0


@pytest.mark.parametrize(
    "line",
    [
        b"not nmea",
        b"$GNRMC,too,few,fields*00",
        RMC_FIXTURE.read_bytes().splitlines()[0][:-2] + b"00",
        b"\xff\xfe",
    ],
)
def test_malformed_sentences_are_counted_and_skipped(line: bytes):
    decoder = NmeaDecoder("serial0", "um980")

    assert decoder.decode(line) == []
    assert decoder.stats.malformed_sentences == 1


def test_unsupported_sentences_are_ignored_without_being_malformed():
    decoder = NmeaDecoder("serial0", "um980")

    assert decoder.decode(b"$GNGSV,ignored") == []
    assert decoder.stats.ignored_sentences == 1
    assert decoder.stats.malformed_sentences == 0


def _sentence(body: str) -> bytes:
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f"${body}*{checksum:02X}".encode()


@pytest.mark.parametrize(
    "body",
    [
        "GNRMC,061912.00,V,3145.8100,S,11548.7500,E,0.05,0.0,130626,,,N,V",
        "GNRMC,061912.00,D,3145.8100,S,11548.7500,E,0.05,0.0,130626,,,D,V",
        "GNRMC,061912.00,A,9145.8100,S,11548.7500,E,0.05,0.0,130626,,,A,V",
        "GNRMC,061912.00,A,3145.8100,S,18148.7500,E,0.05,0.0,130626,,,A,V",
        "GNRMC,061912.00,A,3145.8100,W,11548.7500,E,0.05,0.0,130626,,,A,V",
        "GNRMC,061912.00,A,3145.8100,S,11548.7500,N,0.05,0.0,130626,,,A,V",
        "GNRMC,061912.00,A,3145.8100,S,11548.7500,E,nan,0.0,130626,,,A,V",
        "GNRMC,061912.00,A,3145.8100,S,11548.7500,E,0.05,inf,130626,,,A,V",
    ],
)
def test_invalid_or_void_rmc_fixes_are_not_emitted(body: str):
    decoder = NmeaDecoder("serial0", "um980")

    assert decoder.decode(_sentence(body)) == []
    assert decoder.stats.malformed_sentences == 1


def test_gga_decodes_altitude_quality_and_dop():
    decoder = NmeaDecoder("serial0", "um980")

    values = dict(
        decoder.decode(
            _sentence(
                "GNGGA,061912.00,3145.8100,S,11548.7500,E,4,32,0.6,52.314,M,-29.912,M,1.0,0000"
            )
        )
    )

    assert values == {
        "serial0:um980.GGA.alt": pytest.approx(52.314),
        "serial0:um980.GGA.geoid_sep": pytest.approx(-29.912),
        "serial0:um980.GGA.sats": 32,
        "serial0:um980.GGA.hdop": pytest.approx(0.6),
        "serial0:um980.GGA.quality": 4,
    }
    assert decoder.stats.gga_sentences == 1


def test_gga_without_a_fix_is_not_emitted():
    decoder = NmeaDecoder("serial0", "um980")

    assert decoder.decode(_sentence("GNGGA,061912.00,,,,,0,00,99.9,,M,,M,,")) == []
    assert decoder.stats.malformed_sentences == 1


def test_gst_decodes_one_sigma_position_errors():
    decoder = NmeaDecoder("serial0", "um980")

    values = dict(
        decoder.decode(_sentence("GNGST,061912.00,0.8,0.015,0.011,45.0,0.012,0.014,0.025"))
    )

    assert values == {
        "serial0:um980.GST.lat_std": pytest.approx(0.012),
        "serial0:um980.GST.lon_std": pytest.approx(0.014),
        "serial0:um980.GST.alt_std": pytest.approx(0.025),
    }
    assert decoder.stats.gst_sentences == 1


@pytest.mark.parametrize(
    "body",
    [
        "GNGST,061912.00,0.8,0.015,0.011,45.0,,,",
        "GNGST,061912.00,0.8,0.015,0.011,45.0,-1,0.014,0.025",
        "GNGST,061912.00,0.8,0.015,0.011,45.0,nan,0.014,0.025",
    ],
)
def test_gst_without_usable_error_estimates_is_not_emitted(body: str):
    decoder = NmeaDecoder("serial0", "um980")

    assert decoder.decode(_sentence(body)) == []
    assert decoder.stats.malformed_sentences == 1
