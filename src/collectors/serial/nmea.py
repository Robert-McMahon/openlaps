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
    gga_sentences: int = 0
    gst_sentences: int = 0
    ignored_sentences: int = 0
    malformed_sentences: int = 0


class NmeaDecoder:
    """Decode NMEA RMC, GGA and GST sentences into source-native values.

    RMC carries the high-rate fix (position, speed over ground, course).
    GGA adds altitude, satellite count, HDOP and the numeric fix quality;
    GST adds the receiver's own 1-sigma position error estimates. The
    sensor-fusion app weights fixes by GST and takes altitude from GGA.
    """

    def __init__(self, source_name: str, device: str) -> None:
        self.stats = NmeaDecodeStats()
        self._prefix = f"{source_name}:{device}.RMC."
        self._gga_prefix = f"{source_name}:{device}.GGA."
        self._gst_prefix = f"{source_name}:{device}.GST."

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
            talker_type = fields[0][-3:] if fields else ""
            if talker_type == "GGA":
                return self._decode_gga(fields)
            if talker_type == "GST":
                return self._decode_gst(fields)
            if talker_type != "RMC":
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

    def _decode_gga(self, fields: list[str]) -> list[tuple[str, SampleValue]]:
        # $xxGGA,time,lat,N,lon,E,quality,numSV,HDOP,alt,M,sep,M,diffAge,diffStation
        try:
            if len(fields) < 12:
                raise ValueError("GGA sentence has too few fields")
            quality = int(fields[6])
            if quality == 0:
                raise ValueError("GGA reports no fix")
            satellites = int(fields[7])
            hdop = float(fields[8])
            altitude = float(fields[9])
            separation = float(fields[11]) if fields[11] else 0.0
            if not all(math.isfinite(v) for v in (hdop, altitude, separation)):
                raise ValueError("GGA contains a non-finite number")
            if satellites < 0 or hdop < 0:
                raise ValueError("GGA value is out of range")
        except (ValueError, IndexError):
            self.stats.malformed_sentences += 1
            return []
        self.stats.gga_sentences += 1
        prefix = self._gga_prefix
        return [
            (prefix + "alt", altitude),
            (prefix + "geoid_sep", separation),
            (prefix + "sats", satellites),
            (prefix + "hdop", hdop),
            (prefix + "quality", quality),
        ]

    def _decode_gst(self, fields: list[str]) -> list[tuple[str, SampleValue]]:
        # $xxGST,time,rms,stdMajor,stdMinor,orient,stdLat,stdLon,stdAlt
        try:
            if len(fields) < 9:
                raise ValueError("GST sentence has too few fields")
            if not (fields[6] and fields[7] and fields[8]):
                raise ValueError("GST has no error estimate")
            std_lat = float(fields[6])
            std_lon = float(fields[7])
            std_alt = float(fields[8])
            values = (std_lat, std_lon, std_alt)
            if not all(math.isfinite(v) and v >= 0 for v in values):
                raise ValueError("GST error estimate is out of range")
        except (ValueError, IndexError):
            self.stats.malformed_sentences += 1
            return []
        self.stats.gst_sentences += 1
        prefix = self._gst_prefix
        return [
            (prefix + "lat_std", std_lat),
            (prefix + "lon_std", std_lon),
            (prefix + "alt_std", std_alt),
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
