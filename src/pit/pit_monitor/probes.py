"""The four things the pit knows about itself and nothing else was reporting.

Each probe returns ``(source, metric, value)`` readings and is allowed to
fail on its own: a chronyd that is down, an ntrip-client that is stopped
because this event is not running RTK, a NATS server mid-restart. The
service records the failure and keeps the other three, the same discipline
`HostMetricsReader` already applies per metric group.

`HostProbe` is a thin adapter over the vehicle's `collectors.host` rather
than a second implementation: the psutil polling and the `chronyc tracking`
parser are the same problem at both ends of the link, and a pit-side copy
would be a copy that drifts. It splits the one reader's output into two
sources because `clock_*` is chrony's answer and the rest is psutil's, and
the dashboard asks about them separately.

NATS is scraped only at the pit's own `:8222`. That is not a compromised
version of "both ends": `/leafz` reports the leafnode connection *and its
remote* -- whether the car is attached, its round-trip time, and the bytes
moving each way -- so the pit learns the state of both ends without dialling
the vehicle. The pit dials the car for telemetry and nothing else; a
monitoring scrape across the radio would be a second reason for the pit to
need the car reachable, which is exactly what the leafnode design avoids.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any, Protocol

from collectors.host import HostMetricsReader

logger = logging.getLogger(__name__)

# (source, metric, value). A str value is stored in value_text, a float in value.
Reading = tuple[str, str, float | str]


class Probe(Protocol):
    """One source of pit health. Raising is how a probe reports unavailable."""

    def read(self) -> Iterator[Reading]:
        """Yield this probe's readings for one poll."""


_DURATION = re.compile(r"^\s*([0-9.]+)\s*(ns|us|µs|ms|s|m|h)\s*$")
_DURATION_SCALE = {
    "ns": 1e-9,
    "us": 1e-6,
    "µs": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
}


def sanitize(name: str) -> str:
    """Normalize a server/stream/consumer name into a metric-name fragment."""
    cleaned = "".join(char if char.isalnum() else "_" for char in name.strip().lower())
    return cleaned.strip("_") or "unknown"


def parse_duration_s(text: str) -> float | None:
    """Parse a Go duration string as NATS reports RTT (``"1.53ms"``)."""
    match = _DURATION.match(text)
    if match is None:
        return None
    return float(match.group(1)) * _DURATION_SCALE[match.group(2)]


def fetch_json(url: str, timeout_s: float) -> dict[str, Any]:
    """GET one JSON document. Raises on transport, status or parse errors."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        payload = response.read()
    document = json.loads(payload)
    if not isinstance(document, dict):
        raise ValueError(f"{url} did not return a JSON object")
    return document


def _numeric(value: object) -> float | None:
    """Coerce a JSON scalar to a metric value, or None if it is not one."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int | float):
        return float(value)
    return None


def split_host_reading(source_ref: str) -> tuple[str, str]:
    """Map one `collectors.host` source ref onto ``(source, metric)``.

    `collectors.host` names everything `host:<metric>`, but the four clock
    fields come from `chronyc tracking` rather than psutil, and the dashboard
    asks about the pit's clock and the pit's resources separately. Metric
    names are otherwise passed through unchanged: they are a documented stable
    interface at the vehicle end and there is no reason to rename them here.
    """
    metric = source_ref.removeprefix("host:")
    if metric.startswith("clock_"):
        return "chrony", metric.removeprefix("clock_")
    return "host", metric


class HostProbe:
    """Pit host resources and chrony state, via the vehicle's host collector."""

    def __init__(self, *, disk_path: str = "/", reader: HostMetricsReader | None = None) -> None:
        """Build the underlying psutil/chronyc reader, primed for CPU percent."""
        self._reader = HostMetricsReader(disk_path=disk_path) if reader is None else reader

    @property
    def probe_failures(self) -> int:
        """Metric groups the reader could not read on the last poll."""
        return self._reader.stats.probe_failures

    def read(self) -> Iterator[Reading]:
        """Yield psutil readings under ``host`` and chrony's under ``chrony``.

        `HostMetricsReader` swallows a failing metric group rather than losing
        the whole poll, so a dead chronyd shows up here as four absent
        readings and nothing else. Its own failure counter is emitted as a
        metric so that case is distinguishable from the collector being down,
        which is the opposite conclusion.
        """
        for source_ref, value in self._reader.read():
            source, metric = split_host_reading(source_ref)
            yield source, metric, value
        yield "host", "probe_failures", float(self._reader.stats.probe_failures)


class NtripProbe:
    """The ntrip-client's `/health`, which was its only reader until now."""

    def __init__(self, url: str, *, timeout_s: float = 3.0) -> None:
        """Scrape ``url`` (the service's `/health`) on each poll."""
        self._url = url
        self._timeout_s = timeout_s

    def read(self) -> Iterator[Reading]:
        """Yield every scalar the snapshot carries, under ``ntrip``.

        Deliberately not a fixed key list: `/health` is the ntrip-client's own
        contract and anything it grows should land here without a second edit.
        `last_byte_age_s` is null before the first correction byte, and a null
        is an absent reading rather than a zero -- "no bytes yet" and "a byte
        this instant" must not draw the same point.
        """
        for key, raw in sorted(fetch_json(self._url, self._timeout_s).items()):
            if raw is None:
                continue
            number = _numeric(raw)
            if number is not None:
                yield "ntrip", key, number
            elif isinstance(raw, str):
                yield "ntrip", key, raw


_VARZ_METRICS = (
    "connections",
    "total_connections",
    "routes",
    "remotes",
    "leafnodes",
    "subscriptions",
    "slow_consumers",
    "in_msgs",
    "out_msgs",
    "in_bytes",
    "out_bytes",
    "mem",
    "cpu",
)
_JSZ_METRICS = ("streams", "consumers", "messages", "bytes", "memory", "storage")
_STREAM_STATE_METRICS = ("messages", "bytes", "consumer_count", "first_seq", "last_seq")
_CONSUMER_METRICS = ("num_pending", "num_ack_pending", "num_redelivered", "num_waiting")
_LEAF_METRICS = ("in_msgs", "out_msgs", "in_bytes", "out_bytes", "subscriptions")


class NatsProbe:
    """The pit NATS server's monitoring endpoints: varz, jsz and leafz."""

    def __init__(self, base_url: str, *, timeout_s: float = 3.0) -> None:
        """Scrape the monitoring port at ``base_url`` (``http://host:8222``)."""
        self._base = base_url.rstrip("/")
        self._timeout_s = timeout_s

    def read(self) -> Iterator[Reading]:
        """Yield server, JetStream and leafnode readings under ``nats``."""
        yield from self._read_varz()
        yield from self._read_jsz()
        yield from self._read_leafz()

    def _get(self, path: str) -> dict[str, Any]:
        return fetch_json(f"{self._base}{path}", self._timeout_s)

    def _read_varz(self) -> Iterator[Reading]:
        varz = self._get("/varz")
        for key in _VARZ_METRICS:
            number = _numeric(varz.get(key))
            if number is not None:
                yield "nats", key, number
        for key in ("server_name", "version"):
            value = varz.get(key)
            if isinstance(value, str) and value:
                yield "nats", key, value

    def _read_jsz(self) -> Iterator[Reading]:
        # streams=1 and consumers=1 are what turn this from a set of totals
        # into per-stream depth and per-consumer lag, which is the question
        # actually being asked ("is the writer keeping up?").
        jsz = self._get("/jsz?streams=1&consumers=1")
        for key in _JSZ_METRICS:
            number = _numeric(jsz.get(key))
            if number is not None:
                yield "nats", f"js.{key}", number
        for account in jsz.get("account_details") or []:
            for stream in account.get("stream_detail") or []:
                yield from self._read_stream(stream)

    def _read_stream(self, stream: dict[str, Any]) -> Iterator[Reading]:
        name = sanitize(str(stream.get("name", "")))
        state = stream.get("state") or {}
        for key in _STREAM_STATE_METRICS:
            number = _numeric(state.get(key))
            if number is not None:
                yield "nats", f"stream.{name}.{key}", number
        for consumer in stream.get("consumer_detail") or []:
            consumer_name = sanitize(str(consumer.get("name", "")))
            for key in _CONSUMER_METRICS:
                number = _numeric(consumer.get(key))
                if number is not None:
                    yield "nats", f"consumer.{name}.{consumer_name}.{key}", number

    def _read_leafz(self) -> Iterator[Reading]:
        leafz = self._get("/leafz")
        leafs = leafz.get("leafs") or []
        yield "nats", "leafz.count", float(len(leafs))
        for index, leaf in enumerate(leafs):
            # A leaf that has not finished its handshake has no name yet, so
            # fall back to the address: an unnamed leaf still needs a stable
            # metric name or its readings collide with the next one's.
            raw_name = str(leaf.get("name") or "")
            label = sanitize(raw_name or f"{leaf.get('ip', '')}_{leaf.get('port', index)}")
            yield "nats", f"leaf.{label}.name", raw_name or "unnamed"
            rtt = leaf.get("rtt")
            if isinstance(rtt, str):
                seconds = parse_duration_s(rtt)
                if seconds is not None:
                    yield "nats", f"leaf.{label}.rtt_s", seconds
            for key in _LEAF_METRICS:
                number = _numeric(leaf.get(key))
                if number is not None:
                    yield "nats", f"leaf.{label}.{key}", number
