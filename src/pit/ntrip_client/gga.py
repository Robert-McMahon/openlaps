"""Synthesised ``$GPGGA`` sentences for VRS / network-RTK mountpoints.

Some casters will not stream corrections for a mountpoint until the rover
reports an approximate position, which the NTRIP protocol expects as a GGA
sentence sent upstream over the same connection. This module only
synthesises that sentence from the vehicle's last known lat/lon — it never
reads GNSS state on its own.
"""

from __future__ import annotations

from datetime import UTC, datetime


def build_gga(lat: float, lon: float, *, when: datetime | None = None) -> bytes:
    """Encode one ``$GPGGA`` sentence for ``(lat, lon)``, fix quality 1 (GPS)."""
    when = when if when is not None else datetime.now(UTC)
    time_field = when.strftime("%H%M%S") + f".{when.microsecond // 10_000:02d}"
    ns, alat = ("N", lat) if lat >= 0 else ("S", -lat)
    ew, alon = ("E", lon) if lon >= 0 else ("W", -lon)
    lat_field = f"{int(alat):02d}{(alat - int(alat)) * 60:07.4f}"
    lon_field = f"{int(alon):03d}{(alon - int(alon)) * 60:07.4f}"
    body = f"GPGGA,{time_field},{lat_field},{ns},{lon_field},{ew},1,08,1.0,0.0,M,0.0,M,,"
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f"${body}*{checksum:02X}\r\n".encode("ascii")
