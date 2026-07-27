"""Small, dependency-free NMEA decoder for GNSS serial sources."""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.samples import SampleValue

KNOTS_TO_KMH = 1.852


@dataclass(slots=True)
class NmeaDecodeStats:
    """Sentence counters surfaced by the serial collector."""

    sentences: int = 0
    rmc_sentences: int = 0
    ignored_sentences: int = 0
    malformed_sentences: int = 0


class NmeaDecoder:
    """Decode NMEA RMC sentences into source-native telemetry values."""

    def __init__(self, source_name: str, device: str) -> None:
        self.stats = NmeaDecodeStats()
        self._prefix = f"{source_name}:{device}.RMC."

    def decode(self, sentence: bytes | str) -> list[tuple[str, SampleValue]]:
        """Decode one complete sentence, returning no values for bad input."""
        self.stats.sentences += 1
        try:
            text = (
                sentence.decode("ascii", errors="strict")
                if isinstance(sentence, bytes)
                else sentence
            ).strip()
            body = _validated_body(text)
            fields = body.split(",")
            if not fields or not fields[0].endswith("RMC"):
                self.stats.ignored_sentences += 1
                return []
            if len(fields) < 12:
                raise ValueError("RMC sentence has too few fields")
            if fields[2] != "A":
                raise ValueError("RMC fix is not active")

            latitude = _coordinate(fields[3], fields[4], 2)
            longitude = _coordinate(fields[5], fields[6], 3)
            speed = float(fields[7]) * KNOTS_TO_KMH
            heading = float(fields[8])
            if not all(math.isfinite(value) for value in (latitude, longitude, speed, heading)):
                raise ValueError("RMC contains a non-finite number")
            if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                raise ValueError("RMC coordinate is out of range")
            if speed < 0 or not 0 <= heading < 360:
                raise ValueError("RMC movement value is out of range")
            mode = fields[12] if len(fields) > 12 and fields[12] else fields[2]
        except (UnicodeDecodeError, ValueError, IndexError):
            self.stats.malformed_sentences += 1
            return []

        self.stats.rmc_sentences += 1
        return [
            (self._prefix + "lat", latitude),
            (self._prefix + "lon", longitude),
            (self._prefix + "speed", speed),
            (self._prefix + "heading", heading),
            (self._prefix + "mode", mode),
        ]


def _validated_body(sentence: str) -> str:
    if not sentence.startswith("$"):
        raise ValueError("NMEA sentence must begin with '$'")
    payload = sentence[1:]
    body, separator, supplied_checksum = payload.partition("*")
    if separator:
        if len(supplied_checksum) != 2:
            raise ValueError("invalid NMEA checksum field")
        checksum = 0
        for character in body:
            checksum ^= ord(character)
        if supplied_checksum.upper() != f"{checksum:02X}":
            raise ValueError("NMEA checksum mismatch")
    return body


def _coordinate(value: str, hemisphere: str, degree_digits: int) -> float:
    if len(value) <= degree_digits:
        raise ValueError("invalid NMEA coordinate")
    degrees = int(value[:degree_digits])
    minutes = float(value[degree_digits:])
    if minutes < 0 or minutes >= 60:
        raise ValueError("invalid NMEA coordinate minutes")
    coordinate = degrees + minutes / 60.0
    positive, negative = ("N", "S") if degree_digits == 2 else ("E", "W")
    if hemisphere == negative:
        return -coordinate
    if hemisphere != positive:
        raise ValueError("invalid NMEA hemisphere")
    return coordinate
