#!/usr/bin/env python3
"""Bench instrument for the vehicle -> pit HaLow link (P4.0).

Everything Phase 4 measures is read through this tool, so it samples raw
counters and defers *all* arithmetic to analysis time. A CSV row is a set of
readings taken at one instant; rates, ratios and backlogs are computed by
``--summary`` and ``--merge`` from the deltas between rows. That split is
deliberate: a derived value baked into a sample can never be recomputed once
its inputs are gone, and a bench run is expensive enough that "re-run it, the
arithmetic was wrong" is not an acceptable answer.

**One sampler per host** (``docs/plan/PHASE4.md`` locked decision 3).
``--role vehicle`` polls only vehicle-local endpoints, ``--role pit`` only
pit-local ones, and ``--merge`` joins the two CSVs afterwards. A single probe
reaching across the radio would add its own load to the link it is measuring
and would lose exactly the samples that matter during a sever.

Sources, per role:

* **NATS monitoring** on the local server's ``:8222`` (enabled in both
  ``deploy/nats/vehicle.conf`` and ``deploy/nats/pit.conf``) --- ``/varz``
  for server totals and restart detection, ``/leafz`` for the link itself
  (this is where the forward/reverse split ``LINK_BUDGET.md`` §8 asks about
  lives), ``/jsz`` for stream and consumer state.
* **Wire counters** --- named ``nft`` counters on the leafnode port, created
  by the P4.2 runbook, read here with ``nft -j list counters``. These include
  TLS, TCP acknowledgements and retransmits, so wire bytes ÷ NATS bytes *is*
  the framing overhead §2 estimates at 6-13% and explicitly does not model.
  Falls back to ``/proc/net/dev`` for the interface when no counter is found,
  and records which source it used --- a run must never be silently less
  precise than it looks.
* **Pit service health** (``--role pit``) --- the five ``/health`` endpoints
  ``deploy/README.md`` publishes, reduced to the keys that matter.
* **Radio**, via a pluggable adapter against the **local** router only. The
  HaLowLink units are OpenWrt boxes; the default adapter shells out to the
  system ``ssh`` (so ``~/.ssh/config`` and ``ssh-agent`` apply --- there is
  no password option and never should be) and parses ``iw dev <iface>
  station dump``, falling back to ``ubus call iwinfo assoclist`` where the
  vendor driver does not populate ``iw``.

**Unreachable endpoints null a row's columns and record a reason; they never
raise.** An empty CSV cell means "could not ask" and a ``0`` means "asked,
and the answer was zero". Conflating those is how a dropout gets written up
as an idle link, so the distinction is load-bearing throughout.

Usage::

    # on the vehicle SBC
    uv run tools/link_probe.py --role vehicle --out runs/steady-veh.csv \\
        --radio-adapter auto --radio-host halow-vehicle --radio-iface wlan0 \\
        --tick-ms 20 --signal-source canplayer+bench_gps

    # on the pit machine
    uv run tools/link_probe.py --role pit --out runs/steady-pit.csv

    # afterwards, on either
    uv run tools/link_probe.py --merge runs/steady-veh.csv runs/steady-pit.csv \\
        --out runs/steady-merged.csv
    uv run tools/link_probe.py --summary runs/steady-merged.csv --tick-ms 20
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

# docs/LINK_BUDGET.md §3, NATS-framed offered load from tools/size_batch.py
# against the default signal mix. --summary reports measured forward rate
# against these so a run reconciles against the model without a second step.
MODEL_KBIT_S = {10: 552.8, 20: 544.4}

# The telemetry stream this role owns. The vehicle's TELE and the pit's
# sourced TELE_VEHICLE are the same messages at two points on the link, which
# is what makes `vehicle last_seq - pit last_seq` the sourcing backlog.
DEFAULT_STREAM = {"vehicle": "TELE", "pit": "TELE_VEHICLE"}
DEFAULT_CONSUMER = "ingest-writer"

DEFAULT_MONITOR = "http://127.0.0.1:8222"
DEFAULT_HEALTH_HOST = "http://127.0.0.1"
HEALTH_PORTS = {
    "session": 8080,
    "ingest": 8081,
    "live": 8082,
    "ntrip": 8083,
    "timing": 8084,
    "notifier": 8086,
}

DEFAULT_NFT_COUNTER_OUT = "openlaps_leaf_out"
DEFAULT_NFT_COUNTER_IN = "openlaps_leaf_in"
DEFAULT_NFT_COMMAND = "nft -j list counters"

DEFAULT_INTERVAL_S = 1.0
DEFAULT_HTTP_TIMEOUT_S = 2.0
DEFAULT_RADIO_TIMEOUT_S = 5.0

# Sampled columns: raw readings only. Derived series are appended by
# `derive()` at analysis time and are not written by the sampler.
SAMPLE_COLUMNS: tuple[str, ...] = (
    "t_unix",
    "t_iso",
    "role",
    "host",
    # NATS /varz -- server totals, and `varz_start` for restart detection.
    "varz_ok",
    "varz_start",
    "varz_in_msgs",
    "varz_out_msgs",
    "varz_in_bytes",
    "varz_out_bytes",
    "varz_slow_consumers",
    # NATS /leafz -- the application-layer view of the link. Direction is
    # relative to *this* server: on the vehicle `leaf_out_*` is
    # vehicle -> pit; on the pit `leaf_in_*` is. `link_direction()` resolves
    # that so nothing downstream has to remember it.
    "leafz_ok",
    "leaf_count",
    "leaf_rtt_ms",
    "leaf_in_msgs",
    "leaf_out_msgs",
    "leaf_in_bytes",
    "leaf_out_bytes",
    # NATS /jsz -- stream and consumer state for this role's telemetry stream.
    "jsz_ok",
    "stream_name",
    "stream_messages",
    "stream_bytes",
    "stream_first_seq",
    "stream_last_seq",
    "stream_consumer_count",
    "source_lag",
    "source_active_ns",
    "consumer_name",
    "consumer_num_pending",
    "consumer_num_ack_pending",
    "consumer_num_redelivered",
    "consumer_delivered_stream_seq",
    "consumer_ack_floor_stream_seq",
    # Wire counters -- everything NATS does not see (TLS, TCP acks, retries).
    "wire_ok",
    "wire_source",
    "wire_out_bytes",
    "wire_in_bytes",
    "wire_out_packets",
    "wire_in_packets",
    # Pit service health (--role pit).
    "session_ok",
    "session_db_connected",
    "session_db_errors",
    "session_db_pending",
    "ingest_ok",
    "ingest_rows_per_s",
    "ingest_flushes_per_s",
    "ingest_lag_ms",
    "ingest_wall_lag_ms",
    "ingest_last_stream_seq",
    "ingest_unknown_seq_batches",
    "ingest_bad_version_batches",
    "ingest_dropped_flushes",
    "live_ok",
    "live_publish_rate",
    "live_aggregate_sheds",
    "live_mqtt_drops",
    "live_nats_reconnects",
    "ntrip_ok",
    "ntrip_bytes_per_s",
    "ntrip_reconnects",
    "ntrip_last_byte_age_s",
    "timing_ok",
    "timing_mqtt_connected",
    "timing_publishes",
    "timing_degraded_channels",
    "timing_gate_reason",
    "notifier_ok",
    "notifier_heartbeat_ok",
    "notifier_heartbeat_age_s",
    "notifier_active",
    "notifier_unacknowledged",
    "notifier_queue_pending",
    "notifier_ledger_errors",
    # Radio, local router only.
    "radio_ok",
    "radio_source",
    "radio_signal_dbm",
    "radio_tx_bitrate_mbps",
    "radio_rx_bitrate_mbps",
    "radio_tx_retries",
    "radio_tx_failed",
    "radio_expected_throughput_mbps",
    # `endpoint=reason` pairs for everything that failed this sample.
    "reasons",
)

DERIVED_COLUMNS: tuple[str, ...] = (
    "dt_s",
    "nats_restarted",
    "fwd_nats_kbit_s",
    "rev_nats_kbit_s",
    "fwd_nats_msgs_per_s",
    "reverse_ratio",
    "fwd_wire_kbit_s",
    "rev_wire_kbit_s",
    "framing_overhead",
    "airtime_efficiency",
)

# 64-bit is right for NATS (Go int64) and nft (u64). /proc/net/dev is an
# unsigned long and really does wrap at 2**32 on a 32-bit SBC, which is why
# the width is an option rather than a constant.
COUNTER_WIDTH_BITS = 64


class ProbeError(Exception):
    """A configuration or CLI error worth exiting on. Never a sample failure."""


# --- parsers -----------------------------------------------------------------
#
# Each takes the raw bytes/text an endpoint returned and produces a flat dict
# of the columns it owns. They are pure so tests can drive them from the
# fixtures in tests/fixtures/probe/ without a server.


def parse_varz(payload: dict[str, Any]) -> dict[str, Any]:
    """Server totals and the start time a restart is detected from."""
    return {
        "varz_ok": 1,
        "varz_start": payload.get("start"),
        "varz_in_msgs": payload.get("in_msgs"),
        "varz_out_msgs": payload.get("out_msgs"),
        "varz_in_bytes": payload.get("in_bytes"),
        "varz_out_bytes": payload.get("out_bytes"),
        "varz_slow_consumers": payload.get("slow_consumers"),
    }


def parse_leafz(payload: dict[str, Any]) -> dict[str, Any]:
    """Aggregate the leaf connections into one link view.

    The deployed topology has exactly one leafnode per server, but summing
    rather than indexing means a second one (a test client, a mistake)
    inflates the numbers visibly instead of being silently ignored. RTT is
    taken from the first connection because it has no meaningful sum.
    """
    leafs = payload.get("leafs") or []
    row: dict[str, Any] = {
        "leafz_ok": 1,
        "leaf_count": payload.get("leafnodes", len(leafs)),
        "leaf_rtt_ms": None,
        "leaf_in_msgs": None,
        "leaf_out_msgs": None,
        "leaf_in_bytes": None,
        "leaf_out_bytes": None,
    }
    if not leafs:
        # A severed link is a real, reportable state: zero connections, and
        # counters that genuinely have no value rather than a value of zero.
        return row
    totals = {"in_msgs": 0, "out_msgs": 0, "in_bytes": 0, "out_bytes": 0}
    for leaf in leafs:
        for key in totals:
            totals[key] += int(leaf.get(key) or 0)
    row["leaf_in_msgs"] = totals["in_msgs"]
    row["leaf_out_msgs"] = totals["out_msgs"]
    row["leaf_in_bytes"] = totals["in_bytes"]
    row["leaf_out_bytes"] = totals["out_bytes"]
    row["leaf_rtt_ms"] = parse_duration_ms(leafs[0].get("rtt"))
    return row


_DURATION_UNITS = {"ns": 1e-6, "us": 1e-3, "µs": 1e-3, "μs": 1e-3, "ms": 1.0, "s": 1000.0}
_DURATION_RE = re.compile(r"([0-9.]+)\s*(ns|us|µs|μs|ms|s)")


def parse_duration_ms(value: object) -> float | None:
    """Go duration strings (``787µs``, ``1.5ms``, ``2s``) to milliseconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    total = 0.0
    matched = False
    for magnitude, unit in _DURATION_RE.findall(str(value)):
        try:
            total += float(magnitude) * _DURATION_UNITS[unit]
        except ValueError:
            return None
        matched = True
    return round(total, 4) if matched else None


def parse_jsz(payload: dict[str, Any], stream: str, consumer: str) -> dict[str, Any]:
    """Stream and consumer state for one named stream.

    ``last_seq`` here is half of the sourcing backlog: the vehicle's ``TELE``
    minus the pit's ``TELE_VEHICLE``. ``--merge`` computes the difference; a
    single-role CSV carries only its own end of it.
    """
    row: dict[str, Any] = {
        "jsz_ok": 1,
        "stream_name": stream,
        "stream_messages": None,
        "stream_bytes": None,
        "stream_first_seq": None,
        "stream_last_seq": None,
        "stream_consumer_count": None,
        "source_lag": None,
        "source_active_ns": None,
        "consumer_name": None,
        "consumer_num_pending": None,
        "consumer_num_ack_pending": None,
        "consumer_num_redelivered": None,
        "consumer_delivered_stream_seq": None,
        "consumer_ack_floor_stream_seq": None,
    }
    detail = _find_stream(payload, stream)
    if detail is None:
        # The server answered and does not have this stream. That is a fact
        # about the run (provisioning missed, or the stream was purged), not
        # a failed sample, so jsz_ok stays 1 and the columns stay null.
        return row
    state = detail.get("state") or {}
    row["stream_messages"] = state.get("messages")
    row["stream_bytes"] = state.get("bytes")
    row["stream_first_seq"] = state.get("first_seq")
    row["stream_last_seq"] = state.get("last_seq")
    row["stream_consumer_count"] = state.get("consumer_count")
    sources = detail.get("sources") or []
    if sources:
        row["source_lag"] = sources[0].get("lag")
        row["source_active_ns"] = sources[0].get("active")
    found = _find_consumer(detail, consumer)
    if found is not None:
        row["consumer_name"] = found.get("name")
        row["consumer_num_pending"] = found.get("num_pending")
        row["consumer_num_ack_pending"] = found.get("num_ack_pending")
        row["consumer_num_redelivered"] = found.get("num_redelivered")
        row["consumer_delivered_stream_seq"] = (found.get("delivered") or {}).get("stream_seq")
        row["consumer_ack_floor_stream_seq"] = (found.get("ack_floor") or {}).get("stream_seq")
    return row


def _find_stream(payload: dict[str, Any], stream: str) -> dict[str, Any] | None:
    for account in payload.get("account_details") or []:
        for detail in account.get("stream_detail") or []:
            if detail.get("name") == stream:
                return detail
    return None


def _find_consumer(detail: dict[str, Any], consumer: str) -> dict[str, Any] | None:
    consumers = detail.get("consumer_detail") or []
    for found in consumers:
        if found.get("name") == consumer:
            return found
    # A run with one consumer under a different durable name is still
    # measurable; naming the wrong one should not silently report nothing.
    return consumers[0] if len(consumers) == 1 else None


def parse_nft_counters(payload: dict[str, Any], out_name: str, in_name: str) -> dict[str, Any]:
    """Named counters from ``nft -j list counters``.

    Missing counters return nulls rather than zeros, which is what makes the
    caller fall through to ``/proc/net/dev`` instead of recording a link that
    carried nothing.
    """
    counters: dict[str, dict[str, Any]] = {}
    for entry in payload.get("nftables") or []:
        counter = entry.get("counter")
        if isinstance(counter, dict) and "name" in counter:
            counters[str(counter["name"])] = counter
    out = counters.get(out_name)
    inbound = counters.get(in_name)
    if out is None and inbound is None:
        return {}
    return {
        "wire_source": "nft",
        "wire_out_bytes": None if out is None else out.get("bytes"),
        "wire_out_packets": None if out is None else out.get("packets"),
        "wire_in_bytes": None if inbound is None else inbound.get("bytes"),
        "wire_in_packets": None if inbound is None else inbound.get("packets"),
    }


_PROC_NET_DEV_FIELDS = (
    "rx_bytes",
    "rx_packets",
    "rx_errs",
    "rx_drop",
    "rx_fifo",
    "rx_frame",
    "rx_compressed",
    "rx_multicast",
    "tx_bytes",
    "tx_packets",
    "tx_errs",
    "tx_drop",
    "tx_fifo",
    "tx_colls",
    "tx_carrier",
    "tx_compressed",
)


def parse_proc_net_dev(text: str, interface: str) -> dict[str, Any]:
    """Interface totals from ``/proc/net/dev``.

    Coarser than the nft counters --- it is every byte on the interface, not
    just the leafnode's --- which is exactly why the row records
    ``wire_source`` and the summary refuses to treat the two as equivalent.
    """
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name.strip() != interface:
            continue
        values = rest.split()
        if len(values) < len(_PROC_NET_DEV_FIELDS):
            return {}
        fields = dict(zip(_PROC_NET_DEV_FIELDS, (int(value) for value in values), strict=False))
        return {
            "wire_source": "procnetdev",
            "wire_out_bytes": fields["tx_bytes"],
            "wire_out_packets": fields["tx_packets"],
            "wire_in_bytes": fields["rx_bytes"],
            "wire_in_packets": fields["rx_packets"],
        }
    return {}


_STATION_RE = re.compile(r"^Station\s+([0-9a-fA-F:]{17})")
_SIGNAL_RE = re.compile(r"(-?\d+(?:\.\d+)?)")
_BITRATE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([MmKk])Bit/s")
_THROUGHPUT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([MmKk])bps", re.IGNORECASE)


def parse_iw_station_dump(text: str) -> list[dict[str, Any]]:
    """Parse ``iw dev <iface> station dump`` into one dict per station.

    ``iw`` prints ``key:\\tvalue`` under a ``Station <mac> (on <iface>)``
    header, and which keys appear depends on the driver --- the HaLow vendor
    drivers populate a subset. Everything is therefore optional and absent
    keys stay null rather than defaulting to zero.
    """
    stations: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        header = _STATION_RE.match(line.strip())
        if header:
            current = {"mac": header.group(1).lower()}
            stations.append(current)
            continue
        if current is None or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "signal":
            # "-52 [-55, -58] dBm" -- the first number is the combined value.
            match = _SIGNAL_RE.search(value)
            if match:
                current["signal_dbm"] = float(match.group(1))
        elif key in ("tx bitrate", "rx bitrate"):
            rate = _bitrate_mbps(value)
            if rate is not None:
                current["tx_bitrate_mbps" if key.startswith("tx") else "rx_bitrate_mbps"] = rate
        elif key in ("tx retries", "tx failed"):
            digits = _SIGNAL_RE.search(value)
            if digits:
                current[key.replace(" ", "_")] = int(float(digits.group(1)))
        elif key == "expected throughput":
            match = _THROUGHPUT_RE.search(value)
            if match:
                scale = 1.0 if match.group(2).upper() == "M" else 1e-3
                current["expected_throughput_mbps"] = round(float(match.group(1)) * scale, 4)
    return stations


def _bitrate_mbps(value: str) -> float | None:
    match = _BITRATE_RE.search(value)
    if not match:
        return None
    scale = 1.0 if match.group(2).upper() == "M" else 1e-3
    return round(float(match.group(1)) * scale, 4)


def parse_ubus_assoclist(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse ``ubus call iwinfo assoclist`` --- the fallback where ``iw`` is absent.

    iwinfo reports rates in kbit/s; everything else here is in Mbit/s, so
    they are converted at the boundary rather than left for the analyst.
    """
    stations: list[dict[str, Any]] = []
    for entry in payload.get("results") or []:
        station: dict[str, Any] = {"mac": str(entry.get("mac", "")).lower()}
        if entry.get("signal") is not None:
            station["signal_dbm"] = float(entry["signal"])
        for direction, column in (("tx", "tx_bitrate_mbps"), ("rx", "rx_bitrate_mbps")):
            rate = (entry.get(direction) or {}).get("rate")
            if rate is not None:
                station[column] = round(float(rate) / 1000.0, 4)
        transmit = entry.get("tx") or {}
        if transmit.get("retries") is not None:
            station["tx_retries"] = int(transmit["retries"])
        if transmit.get("failed") is not None:
            station["tx_failed"] = int(transmit["failed"])
        stations.append(station)
    return stations


def radio_row(stations: Sequence[dict[str, Any]], source: str, peer: str | None) -> dict[str, Any]:
    """Reduce a station list to the one peer that is the far end of the link."""
    if not stations:
        return {"radio_ok": 1, "radio_source": source}
    station = stations[0]
    if peer:
        wanted = peer.lower()
        station = next((s for s in stations if s.get("mac") == wanted), stations[0])
    return {
        "radio_ok": 1,
        "radio_source": source,
        "radio_signal_dbm": station.get("signal_dbm"),
        "radio_tx_bitrate_mbps": station.get("tx_bitrate_mbps"),
        "radio_rx_bitrate_mbps": station.get("rx_bitrate_mbps"),
        "radio_tx_retries": station.get("tx_retries"),
        "radio_tx_failed": station.get("tx_failed"),
        "radio_expected_throughput_mbps": station.get("expected_throughput_mbps"),
    }


def parse_ingest_health(payload: dict[str, Any]) -> dict[str, Any]:
    """The ingest-writer keys P4.3-P4.5 read (src/pit/ingest_writer/health.py)."""
    return {
        "ingest_ok": 1,
        "ingest_rows_per_s": payload.get("rows_per_s"),
        "ingest_flushes_per_s": payload.get("flushes_per_s"),
        "ingest_lag_ms": payload.get("lag_ms"),
        "ingest_wall_lag_ms": payload.get("wall_lag_ms"),
        "ingest_last_stream_seq": payload.get("last_stream_seq"),
        "ingest_unknown_seq_batches": payload.get("unknown_seq_batches"),
        "ingest_bad_version_batches": payload.get("bad_version_batches"),
        "ingest_dropped_flushes": payload.get("dropped_flushes"),
    }


def parse_live_health(payload: dict[str, Any]) -> dict[str, Any]:
    """The live-decoder keys (src/pit/live_decoder/health.py)."""
    return {
        "live_ok": 1,
        "live_publish_rate": payload.get("publish_rate"),
        "live_aggregate_sheds": payload.get("aggregate_sheds"),
        "live_mqtt_drops": payload.get("mqtt_drops"),
        "live_nats_reconnects": payload.get("nats_reconnects"),
    }


def parse_ntrip_health(payload: dict[str, Any]) -> dict[str, Any]:
    """The ntrip-client keys (src/pit/ntrip_client/health.py)."""
    return {
        "ntrip_ok": 1,
        "ntrip_bytes_per_s": payload.get("bytes_per_s"),
        "ntrip_reconnects": payload.get("reconnects"),
        "ntrip_last_byte_age_s": payload.get("last_byte_age_s"),
    }


def parse_timing_health(payload: dict[str, Any]) -> dict[str, Any]:
    """The pit timing extrapolator's gate/degradation state."""
    channels = payload.get("channels") or {}
    channel_states = [state for state in channels.values() if isinstance(state, dict)]
    reasons = sorted({str(state["reason"]) for state in channel_states if state.get("reason")})
    return {
        "timing_ok": 1,
        "timing_mqtt_connected": _as_int(payload.get("mqtt_connected")),
        "timing_publishes": sum(int(state.get("published") or 0) for state in channel_states),
        "timing_degraded_channels": sum(
            state.get("status") in {"gated", "degraded"} for state in channel_states
        ),
        "timing_gate_reason": ",".join(reasons) or None,
    }


def parse_notifier_health(payload: dict[str, Any]) -> dict[str, Any]:
    """The notifier's ``/health`` (src/pit/notifier/service.py)."""
    heartbeat = payload.get("heartbeat") or {}
    queue = payload.get("queue") or {}
    ledger = payload.get("ledger") or {}
    return {
        "notifier_ok": 1,
        "notifier_heartbeat_ok": _as_int(heartbeat.get("ok")),
        "notifier_heartbeat_age_s": heartbeat.get("age_s"),
        "notifier_active": payload.get("active"),
        "notifier_unacknowledged": payload.get("unacknowledged"),
        "notifier_queue_pending": queue.get("pending"),
        "notifier_ledger_errors": ledger.get("errors"),
    }


def parse_session_health(payload: dict[str, Any]) -> dict[str, Any]:
    """session-control's ``/health`` (src/pit/session_control/service.py)."""
    database = payload.get("database") or {}
    return {
        "session_ok": 1,
        "session_db_connected": _as_int(database.get("connected")),
        "session_db_errors": database.get("errors"),
        "session_db_pending": database.get("pending"),
    }


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    return int(bool(value)) if isinstance(value, bool) else int(value)


# --- collectors --------------------------------------------------------------


def http_json(url: str, timeout_s: float) -> dict[str, Any]:
    """GET one JSON document. Raises; every caller turns that into a reason."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _short_reason(exc: BaseException) -> str:
    """One line, no newlines, bounded --- it has to fit in a CSV cell."""
    text = str(exc).strip() or exc.__class__.__name__
    return " ".join(text.split())[:120]


@dataclass
class RadioAdapter:
    """Poll the *local* router over ssh. No password ever enters this process.

    ``BatchMode=yes`` is not a convenience: it guarantees ssh fails fast
    instead of blocking a 1 Hz sampler on a password prompt for the rest of
    the run, and it means the only credentials in play are the agent's.
    """

    adapter: str
    host: str
    iface: str
    peer: str | None = None
    ssh_binary: str = "ssh"
    timeout_s: float = DEFAULT_RADIO_TIMEOUT_S

    def sample(self) -> tuple[dict[str, Any], str | None]:
        order = ("iw", "ubus") if self.adapter == "auto" else (self.adapter,)
        reasons = []
        for kind in order:
            try:
                if kind == "iw":
                    text = self._run(["iw", "dev", self.iface, "station", "dump"])
                    stations = parse_iw_station_dump(text)
                else:
                    text = self._run(
                        ["ubus", "call", "iwinfo", "assoclist", f'{{"device":"{self.iface}"}}']
                    )
                    stations = parse_ubus_assoclist(json.loads(text))
            except Exception as exc:  # noqa: BLE001 - a probe never dies on a source
                reasons.append(f"{kind}:{_short_reason(exc)}")
                continue
            if not stations and kind != order[-1]:
                # An empty dump from a driver that does not populate this
                # interface is exactly the case the fallback exists for.
                reasons.append(f"{kind}:no stations")
                continue
            return radio_row(stations, kind, self.peer), None
        return {}, "; ".join(reasons)

    def _run(self, remote: list[str]) -> str:
        command = [
            self.ssh_binary,
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={int(max(1, self.timeout_s))}",
            self.host,
            # One shell-quoted string, not loose argv. ssh joins whatever
            # follows the host with spaces and hands the result to the *remote*
            # shell, which would otherwise eat the quotes in ubus's JSON
            # argument and leave it parsing `{device:wlan1}`.
            shlex.join(remote),
        ]
        completed = subprocess.run(  # noqa: S603
            command, capture_output=True, text=True, timeout=self.timeout_s + 2.0
        )
        if completed.returncode != 0:
            raise ProbeError(f"exit {completed.returncode}: {completed.stderr.strip()[:80]}")
        return completed.stdout


@dataclass
class WireCounters:
    """nft named counters, falling back to /proc/net/dev for the interface."""

    nft_command: Sequence[str]
    counter_out: str
    counter_in: str
    interface: str | None
    proc_path: Path = Path("/proc/net/dev")
    timeout_s: float = DEFAULT_RADIO_TIMEOUT_S

    def sample(self) -> tuple[dict[str, Any], str | None]:
        reasons = []
        if self.nft_command:
            try:
                completed = subprocess.run(  # noqa: S603
                    list(self.nft_command), capture_output=True, text=True, timeout=self.timeout_s
                )
                if completed.returncode != 0:
                    raise ProbeError(
                        f"exit {completed.returncode}: {completed.stderr.strip()[:80]}"
                    )
                row = parse_nft_counters(
                    json.loads(completed.stdout), self.counter_out, self.counter_in
                )
                if row:
                    return {"wire_ok": 1, **row}, None
                reasons.append(f"nft:no counter named {self.counter_out}/{self.counter_in}")
            except Exception as exc:  # noqa: BLE001 - fall through to the interface
                reasons.append(f"nft:{_short_reason(exc)}")
        if self.interface:
            try:
                row = parse_proc_net_dev(self.proc_path.read_text(encoding="utf-8"), self.interface)
                if row:
                    return {"wire_ok": 1, **row}, "; ".join(reasons) or None
                reasons.append(f"procnetdev:no interface {self.interface}")
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"procnetdev:{_short_reason(exc)}")
        return {}, "; ".join(reasons) or "no wire counter source configured"


@dataclass
class Sampler:
    """Builds one CSV row per tick from whichever sources the role has."""

    role: str
    monitor_url: str
    stream: str
    consumer: str
    wire: WireCounters | None
    radio: RadioAdapter | None
    health_base: str
    health_ports: dict[str, int]
    http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S
    host: str = field(default_factory=socket.gethostname)

    def sample(self, now: float | None = None) -> dict[str, Any]:
        moment = time.time() if now is None else now
        row: dict[str, Any] = dict.fromkeys(SAMPLE_COLUMNS)
        row["t_unix"] = round(moment, 3)
        row["t_iso"] = datetime.fromtimestamp(moment, UTC).isoformat()
        row["role"] = self.role
        row["host"] = self.host
        reasons: list[str] = []

        self._collect(row, reasons, "varz", lambda: parse_varz(self._monitor("/varz")))
        self._collect(row, reasons, "leafz", lambda: parse_leafz(self._monitor("/leafz")))
        self._collect(
            row,
            reasons,
            "jsz",
            lambda: parse_jsz(
                self._monitor("/jsz?streams=1&consumers=1"), self.stream, self.consumer
            ),
        )
        for key in ("varz", "leafz", "jsz"):
            if row[f"{key}_ok"] is None:
                row[f"{key}_ok"] = 0

        if self.wire is not None:
            values, reason = self.wire.sample()
            row.update(values)
            row["wire_ok"] = 1 if values else 0
            if reason:
                reasons.append(f"wire={reason}")

        if self.role == "pit":
            self._collect_health(row, reasons)

        if self.radio is not None:
            values, reason = self.radio.sample()
            row.update(values)
            row["radio_ok"] = 1 if values else 0
            if reason:
                reasons.append(f"radio={reason}")

        row["reasons"] = "; ".join(reasons)
        return row

    def _monitor(self, path: str) -> dict[str, Any]:
        return http_json(f"{self.monitor_url.rstrip('/')}{path}", self.http_timeout_s)

    def _collect_health(self, row: dict[str, Any], reasons: list[str]) -> None:
        parsers = {
            "session": parse_session_health,
            "ingest": parse_ingest_health,
            "live": parse_live_health,
            "ntrip": parse_ntrip_health,
            "timing": parse_timing_health,
            "notifier": parse_notifier_health,
        }
        for name, parser in parsers.items():
            port = self.health_ports.get(name)
            if port is None:
                continue
            url = f"{self.health_base.rstrip('/')}:{port}/health"
            self._collect(row, reasons, name, lambda p=parser, u=url: p(self._get(u)))
            if row[f"{name}_ok"] is None:
                row[f"{name}_ok"] = 0

    def _get(self, url: str) -> dict[str, Any]:
        return http_json(url, self.http_timeout_s)

    @staticmethod
    def _collect(row: dict[str, Any], reasons: list[str], name: str, produce) -> None:
        """Run one source; a failure nulls its columns and records why.

        This is the ground rule the whole instrument turns on: a probe that
        raises when the link degrades measures nothing about the interesting
        part of a range walk.
        """
        try:
            row.update(produce())
        except Exception as exc:  # noqa: BLE001 - deliberately total
            reasons.append(f"{name}={_short_reason(exc)}")


# --- counter arithmetic ------------------------------------------------------


def counter_delta(
    previous: object,
    current: object,
    *,
    width_bits: int = COUNTER_WIDTH_BITS,
    reset: bool = False,
) -> float | None:
    """Bytes/messages accumulated between two readings, or ``None``.

    ``None`` --- not zero --- for every case where the answer is genuinely
    unknown: a missing reading at either end, a counter reset (a restarted
    server zeroes its totals, and pretending the delta was ``current`` would
    invent traffic), or a decrease too large to be a plausible wrap. A wrap
    is only accepted when the pre-wrap reading was near the top of the
    counter's range, which is the one shape a real wrap has.
    """
    if previous is None or current is None or reset:
        return None
    try:
        before = float(previous)
        after = float(current)
    except (TypeError, ValueError):
        return None
    if after >= before:
        return after - before
    width = float(2**width_bits)
    if before > width * 0.5:
        return (width - before) + after
    # A decrease from the bottom half of the range is a reset, a
    # reconfiguration or a different counter under the same name. None of
    # those is a rate, so none of them gets reported as one.
    return None


def _num(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def link_direction(role: str) -> tuple[str, str]:
    """The (forward, reverse) leaf byte columns for a role.

    ``/leafz`` reports direction relative to the server being asked, so
    vehicle -> pit is ``leaf_out_bytes`` on the vehicle and ``leaf_in_bytes``
    on the pit. Getting this backwards would invert §8's reverse-channel
    result, so it lives in one function that both derive paths call.
    """
    if role == "vehicle":
        return "leaf_out_bytes", "leaf_in_bytes"
    if role == "pit":
        return "leaf_in_bytes", "leaf_out_bytes"
    raise ProbeError(f"unknown role {role!r}")


def derive(rows: Sequence[dict[str, Any]], role: str | None = None) -> list[dict[str, Any]]:
    """Add the interval-derived series ``LINK_BUDGET.md`` §8 asks for.

    Every rate divides by the *measured* elapsed time between the two rows,
    not the nominal interval, so a sampler that was delayed (or a host that
    was too busy to sample during the interesting part of a sever) reports a
    correct average over the real gap instead of an inflated one.
    """
    if not rows:
        return []
    resolved = role or str(rows[0].get("role") or "")
    forward_column, reverse_column = link_direction(resolved)
    output: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for row in rows:
        derived: dict[str, Any] = {column: None for column in DERIVED_COLUMNS}
        merged = {**row, **derived}
        if previous is not None:
            elapsed = (_num(row.get("t_unix")) or 0.0) - (_num(previous.get("t_unix")) or 0.0)
            merged["dt_s"] = round(elapsed, 3) if elapsed > 0 else None
            restarted = _restarted(previous, row)
            merged["nats_restarted"] = int(restarted)
            if elapsed > 0:
                _fill_rates(
                    merged, previous, row, elapsed, forward_column, reverse_column, restarted
                )
        output.append(merged)
        previous = row
    return output


def _restarted(previous: dict[str, Any], row: dict[str, Any]) -> bool:
    """A changed ``/varz`` ``start`` means the counters below it began again."""
    before = previous.get("varz_start")
    after = row.get("varz_start")
    return bool(before and after and before != after)


def _fill_rates(
    merged: dict[str, Any],
    previous: dict[str, Any],
    row: dict[str, Any],
    elapsed: float,
    forward_column: str,
    reverse_column: str,
    restarted: bool,
) -> None:
    forward = counter_delta(previous.get(forward_column), row.get(forward_column), reset=restarted)
    reverse = counter_delta(previous.get(reverse_column), row.get(reverse_column), reset=restarted)
    messages_column = "leaf_out_msgs" if forward_column == "leaf_out_bytes" else "leaf_in_msgs"
    messages = counter_delta(
        previous.get(messages_column), row.get(messages_column), reset=restarted
    )
    if forward is not None:
        merged["fwd_nats_kbit_s"] = round(forward * 8.0 / 1000.0 / elapsed, 3)
    if reverse is not None:
        merged["rev_nats_kbit_s"] = round(reverse * 8.0 / 1000.0 / elapsed, 3)
    if messages is not None:
        merged["fwd_nats_msgs_per_s"] = round(messages / elapsed, 3)
    if forward is not None and reverse is not None and forward > 0:
        # §8's third caveat, reported as a first-class number rather than
        # left for a reader to divide two columns by hand.
        merged["reverse_ratio"] = round(reverse / forward, 6)

    # Wire counters are only comparable across an interval that used the same
    # source; nft and /proc/net/dev count different things.
    same_source = previous.get("wire_source") == row.get("wire_source") and row.get("wire_source")
    wire_forward = wire_reverse = None
    if same_source:
        wire_forward = counter_delta(previous.get("wire_out_bytes"), row.get("wire_out_bytes"))
        wire_reverse = counter_delta(previous.get("wire_in_bytes"), row.get("wire_in_bytes"))
        if forward_column == "leaf_in_bytes":
            # On the pit, vehicle -> pit arrives on the interface's rx side.
            wire_forward, wire_reverse = wire_reverse, wire_forward
        if wire_forward is not None:
            merged["fwd_wire_kbit_s"] = round(wire_forward * 8.0 / 1000.0 / elapsed, 3)
        if wire_reverse is not None:
            merged["rev_wire_kbit_s"] = round(wire_reverse * 8.0 / 1000.0 / elapsed, 3)
    if wire_forward is not None and forward is not None and forward > 0:
        # §2 estimates TCP/IP + 802.11 framing at 6-13% and does not model
        # it; this ratio is that estimate meeting a measurement.
        merged["framing_overhead"] = round(wire_forward / forward, 6)

    phy_mbps = _num(row.get("radio_tx_bitrate_mbps"))
    if phy_mbps and wire_forward is not None and wire_reverse is not None:
        # §5 assumes 0.5. Goodput here is both directions over the radio,
        # because both share the airtime the PHY rate describes.
        goodput_mbit_s = (wire_forward + wire_reverse) * 8.0 / 1e6 / elapsed
        merged["airtime_efficiency"] = round(goodput_mbit_s / phy_mbps, 6)


# --- merge -------------------------------------------------------------------


def merge(
    vehicle_rows: Sequence[dict[str, Any]],
    pit_rows: Sequence[dict[str, Any]],
    *,
    tolerance_s: float = DEFAULT_INTERVAL_S / 2.0,
) -> list[dict[str, Any]]:
    """Join two single-role runs into the analysis frame.

    The vehicle run is the spine --- it is where offered load is generated,
    so every vehicle sample should appear --- and each pit sample is matched
    to the nearest vehicle sample within ``tolerance_s``. Two probes started
    by hand never share a phase, and during a sever one of them stops
    answering entirely; both cases end with pit columns null for those rows
    rather than with rows quietly dropped.
    """
    derived_vehicle = derive(vehicle_rows, "vehicle")
    derived_pit = derive(pit_rows, "pit")
    pit_times = [_num(row.get("t_unix")) or 0.0 for row in derived_pit]
    merged: list[dict[str, Any]] = []
    for row in derived_vehicle:
        moment = _num(row.get("t_unix")) or 0.0
        out: dict[str, Any] = {"t_unix": row.get("t_unix"), "t_iso": row.get("t_iso")}
        out.update({f"vehicle_{key}": value for key, value in row.items()})
        index = _nearest(pit_times, moment, tolerance_s)
        pit = derived_pit[index] if index is not None else {}
        out.update({f"pit_{key}": pit.get(key) for key in (derived_pit[0] if derived_pit else {})})
        out["pit_matched"] = 0 if index is None else 1
        out["pit_offset_s"] = None if index is None else round(pit_times[index] - moment, 3)
        out["sourcing_backlog"] = _backlog(row, pit)
        merged.append(out)
    return merged


def _nearest(times: Sequence[float], target: float, tolerance_s: float) -> int | None:
    best: int | None = None
    best_distance = tolerance_s
    for index, moment in enumerate(times):
        distance = abs(moment - target)
        if distance <= best_distance:
            best, best_distance = index, distance
    return best


def _backlog(vehicle: dict[str, Any], pit: dict[str, Any]) -> int | None:
    """Vehicle ``TELE.last_seq`` minus pit ``TELE_VEHICLE.last_seq``.

    The single most informative column in a dropout run: it is the number of
    messages the pit has not sourced yet, and it is the series whose failure
    to return to zero defines P4.5's cliff.
    """
    ahead = _num(vehicle.get("stream_last_seq"))
    behind = _num(pit.get("stream_last_seq"))
    if ahead is None or behind is None:
        return None
    return int(ahead - behind)


# --- summary -----------------------------------------------------------------


def percentile(values: Sequence[float], fraction: float) -> float:
    """Linear-interpolated percentile; ``values`` need not be sorted."""
    if not values:
        raise ValueError("percentile of an empty series")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


# Columns `--summary` skips: numeric, but a wall-clock instant rather than a
# series. Suffix-matched so the `vehicle_`/`pit_` prefixes a merged frame adds
# are covered too.
SUMMARY_EXCLUDE_SUFFIXES = ("t_unix",)


@dataclass(frozen=True, slots=True)
class SeriesStats:
    name: str
    count: int
    nulls: int
    mean: float
    p50: float
    p95: float
    maximum: float


def summarise(rows: Sequence[dict[str, Any]]) -> list[SeriesStats]:
    """Per-column mean / p50 / p95 / max over whatever is numeric and present.

    ``nulls`` is reported beside ``count`` because an endpoint that was
    unreachable for part of a run has to be visible in the summary --- a mean
    over the half of the run the probe could still see is a different
    measurement from a mean over all of it.
    """
    if not rows:
        return []
    stats: list[SeriesStats] = []
    for column in rows[0]:
        if column.endswith(SUMMARY_EXCLUDE_SUFFIXES):
            # Numeric, but not a series: a timestamp's p95 is noise that
            # pushes the columns that matter off the screen.
            continue
        values = []
        nulls = 0
        for row in rows:
            value = _num(row.get(column))
            if value is None:
                nulls += 1
            else:
                values.append(value)
        if not values:
            continue
        stats.append(
            SeriesStats(
                name=column,
                count=len(values),
                nulls=nulls,
                mean=sum(values) / len(values),
                p50=percentile(values, 0.5),
                p95=percentile(values, 0.95),
                maximum=max(values),
            )
        )
    return stats


def forward_series(rows: Sequence[dict[str, Any]]) -> tuple[str, list[float]] | None:
    """The forward-rate column in either a single-role or a merged frame."""
    for column in ("fwd_nats_kbit_s", "vehicle_fwd_nats_kbit_s"):
        values = [v for v in (_num(row.get(column)) for row in rows) if v is not None]
        if values:
            return column, values
    return None


def model_comparison(rows: Sequence[dict[str, Any]], tick_ms: int | None) -> list[str]:
    """The ``LINK_BUDGET.md`` §3 reconciliation line(s).

    Measured beside modelled, never replacing it: the ground rules require
    the delta to stay visible so ``tools/size_batch.py`` remains useful for
    the next signal-mix change.
    """
    found = forward_series(rows)
    if found is None:
        return ["forward rate: no samples (nothing to compare against LINK_BUDGET.md §3)"]
    column, values = found
    mean = sum(values) / len(values)
    ticks = [tick_ms] if tick_ms in MODEL_KBIT_S else sorted(MODEL_KBIT_S)
    lines = [f"forward rate ({column}): measured mean {mean:.1f} kbit/s over {len(values)} samples"]
    for tick in ticks:
        modelled = MODEL_KBIT_S[tick]
        delta = (mean - modelled) / modelled * 100.0
        lines.append(
            f"  vs LINK_BUDGET.md §3 modelled {modelled:.1f} kbit/s "
            f"at OPENLAPS_TICK_MS={tick}: {delta:+.1f}%"
        )
    return lines


def format_summary(rows: Sequence[dict[str, Any]], tick_ms: int | None) -> str:
    stats = summarise(rows)
    width = max((len(stat.name) for stat in stats), default=10)
    lines = [
        f"{'series'.ljust(width)}  {'n':>6} {'null':>6} {'mean':>12} "
        f"{'p50':>12} {'p95':>12} {'max':>12}",
        "-" * (width + 68),
    ]
    for stat in stats:
        lines.append(
            f"{stat.name.ljust(width)}  {stat.count:>6} {stat.nulls:>6} "
            f"{stat.mean:>12.3f} {stat.p50:>12.3f} {stat.p95:>12.3f} {stat.maximum:>12.3f}"
        )
    lines.append("")
    lines.extend(model_comparison(rows, tick_ms))
    return "\n".join(lines)


# --- csv i/o -----------------------------------------------------------------


def write_csv(path: Path, rows: Iterable[dict[str, Any]], columns: Sequence[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: "" if row.get(key) is None else row[key] for key in columns})
            written += 1
    return written


def read_csv(path: Path) -> list[dict[str, Any]]:
    """Read a probe CSV back, restoring empty cells to ``None``.

    The round trip has to preserve null-versus-zero or every downstream
    guarantee about unreachable endpoints evaporates at the file boundary.
    """
    with path.open(newline="", encoding="utf-8") as handle:
        return [
            {key: (None if value == "" else value) for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


# --- manifest ----------------------------------------------------------------


def git_sha(repo: Path = REPO_ROOT) -> tuple[str | None, bool | None]:
    """``(sha, dirty)`` for the checkout, or ``(None, None)`` outside git."""
    try:
        sha = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        status = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        return None, None
    if sha.returncode != 0:
        return None, None
    return sha.stdout.strip(), bool(status.stdout.strip())


def profile_provenance(profile: str | None) -> dict[str, Any]:
    """Catalog content hash and persisted registry generation for a profile.

    Reads only: ``build_runtime_catalog`` *bumps* the persisted generation as
    a side effect, and a measurement tool must never change the thing it is
    recording. ``catalog_sha256`` is byte-identical to
    ``ProfileConfig.catalog_hash`` (``src/core/config.py``); the registry
    generation is read from the state file the agent wrote beside the
    profile, which is the generation the run actually used.
    """
    if not profile:
        return {}
    root = Path(profile)
    provenance: dict[str, Any] = {"path": str(root)}
    catalog = root / "catalog.yaml"
    try:
        provenance["catalog_sha256"] = hashlib.sha256(catalog.read_bytes()).hexdigest()
    except OSError as exc:
        provenance["catalog_sha256"] = None
        provenance["catalog_error"] = _short_reason(exc)
    state = root / ".registry-state.json"
    try:
        persisted = json.loads(state.read_text(encoding="utf-8"))
        provenance["registry_seq"] = persisted.get("registry_seq")
        provenance["registry_hash"] = persisted.get("catalog_hash")
    except (OSError, ValueError):
        provenance["registry_seq"] = None
    return provenance


def build_manifest(args: argparse.Namespace, csv_path: Path) -> dict[str, Any]:
    """The provenance the Phase 4 ground rules require. Carries no credentials."""
    sha, dirty = git_sha()
    return {
        "tool": "tools/link_probe.py",
        "role": args.role,
        "host": socket.gethostname(),
        "git_sha": sha,
        "git_dirty": dirty,
        "profile": profile_provenance(args.profile),
        "tick_ms": args.tick_ms,
        "signal_source": args.signal_source,
        "interval_s": args.interval,
        "nats_monitor": args.monitor,
        "stream": args.stream,
        "consumer": args.consumer,
        "wire": {
            "nft_command": args.nft_command,
            "counter_out": args.nft_counter_out,
            "counter_in": args.nft_counter_in,
            "interface": args.interface,
        },
        "radio": {
            "adapter": args.radio_adapter,
            # A hostname, resolved through ~/.ssh/config. Never a credential:
            # authentication is ssh-agent's job and this tool has no password
            # option to record.
            "host": args.radio_host,
            "iface": args.radio_iface,
            "peer": args.radio_peer,
            "config": args.radio_config,
        },
        "clock": {
            "method": args.clock_method,
            "offset_ms": args.clock_offset_ms,
            "source": args.clock_source,
        },
        "notes": args.note,
        "csv_path": str(csv_path.resolve()),
        "started": None,
        "ended": None,
        "samples": 0,
    }


# --- run loop ----------------------------------------------------------------


def build_sampler(args: argparse.Namespace) -> Sampler:
    wire = WireCounters(
        nft_command=args.nft_command.split() if args.nft_command else (),
        counter_out=args.nft_counter_out,
        counter_in=args.nft_counter_in,
        interface=args.interface,
    )
    radio = None
    if args.radio_adapter != "none":
        if not args.radio_host or not args.radio_iface:
            raise ProbeError("--radio-adapter needs both --radio-host and --radio-iface")
        radio = RadioAdapter(
            adapter=args.radio_adapter,
            host=args.radio_host,
            iface=args.radio_iface,
            peer=args.radio_peer,
            ssh_binary=args.ssh_binary,
            timeout_s=args.radio_timeout,
        )
    return Sampler(
        role=args.role,
        monitor_url=args.monitor,
        stream=args.stream,
        consumer=args.consumer,
        wire=wire,
        radio=radio,
        health_base=args.health_base,
        health_ports=dict(HEALTH_PORTS),
        http_timeout_s=args.http_timeout,
    )


def sample_loop(
    sampler: Sampler,
    *,
    interval_s: float,
    duration_s: float | None,
    stop: StopFlag,
    emit,
) -> int:
    """Sample on a fixed grid, skipping missed slots rather than drifting.

    A slot skipped because a source blocked shows up as a longer ``dt_s`` in
    the derived frame, which is honest; a sampler that drifted would instead
    report the same rate over a silently different window.
    """
    started = time.monotonic()
    next_at = started
    count = 0
    while not stop.is_set():
        row = sampler.sample()
        emit(row)
        count += 1
        if duration_s is not None and time.monotonic() - started >= duration_s:
            break
        next_at += interval_s
        now = time.monotonic()
        if next_at < now:
            # Fell behind: resync to the grid instead of firing a burst of
            # back-to-back samples that would all measure the same instant.
            missed = math.ceil((now - next_at) / interval_s)
            next_at += missed * interval_s
        stop.wait(max(0.0, next_at - time.monotonic()))
    return count


class StopFlag:
    """SIGINT/SIGTERM-driven stop that a sleeping loop wakes from promptly."""

    def __init__(self) -> None:
        import threading

        self._event = threading.Event()

    def set(self, *_: object) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> None:
        self._event.wait(timeout)


def run_sampler(args: argparse.Namespace) -> int:
    sampler = build_sampler(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = Path(args.manifest) if args.manifest else out.with_suffix(".manifest.json")
    manifest = build_manifest(args, out)
    manifest["started"] = datetime.now(UTC).isoformat()

    stop = StopFlag()
    # Restored on the way out so a caller that runs the sampler in-process
    # (the tests do) does not inherit the handlers for the rest of its life.
    previous_handlers = {}
    for received in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[received] = signal.signal(received, stop.set)

    count = 0
    # Row-by-row with an explicit flush: a run interrupted by a power cut, a
    # crashed router or an operator's Ctrl-C keeps everything sampled up to
    # that point, which for a 30-minute bench run is the difference between
    # a partial result and none.
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(SAMPLE_COLUMNS), extrasaction="ignore")
        writer.writeheader()

        def emit(row: dict[str, Any]) -> None:
            writer.writerow(
                {key: "" if row.get(key) is None else row[key] for key in SAMPLE_COLUMNS}
            )
            handle.flush()
            os.fsync(handle.fileno())

        print(f"link_probe: role={args.role} interval={args.interval}s -> {out}", file=sys.stderr)
        count = sample_loop(
            sampler,
            interval_s=args.interval,
            duration_s=args.duration,
            stop=stop,
            emit=emit,
        )

    for received, handler in previous_handlers.items():
        signal.signal(received, handler)

    manifest["ended"] = datetime.now(UTC).isoformat()
    manifest["samples"] = count
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"link_probe: {count} samples -> {out}; manifest -> {manifest_path}", file=sys.stderr)
    return 0


def run_merge(args: argparse.Namespace) -> int:
    first, second = (Path(path) for path in args.merge)
    rows = {}
    for path in (first, second):
        loaded = read_csv(path)
        if not loaded:
            raise ProbeError(f"{path}: no rows")
        role = str(loaded[0].get("role") or "")
        if role not in ("vehicle", "pit"):
            raise ProbeError(f"{path}: unrecognised role {role!r}")
        if role in rows:
            raise ProbeError(f"both inputs have role {role!r}; --merge takes one of each")
        rows[role] = loaded
    if set(rows) != {"vehicle", "pit"}:
        raise ProbeError("--merge needs one vehicle CSV and one pit CSV")
    merged = merge(rows["vehicle"], rows["pit"], tolerance_s=args.tolerance)
    if not args.out:
        raise ProbeError("--merge needs --out")
    columns = list(merged[0]) if merged else []
    written = write_csv(Path(args.out), merged, columns)
    unmatched = sum(1 for row in merged if not row.get("pit_matched"))
    print(
        f"link_probe: merged {written} rows -> {args.out} "
        f"({unmatched} vehicle samples with no pit sample within {args.tolerance}s)",
        file=sys.stderr,
    )
    return 0


def run_summary(args: argparse.Namespace) -> int:
    rows = read_csv(Path(args.summary))
    if not rows:
        raise ProbeError(f"{args.summary}: no rows")
    if "role" in rows[0] and not any(key.startswith("vehicle_") for key in rows[0]):
        rows = derive(rows)
    print(format_summary(rows, args.tick_ms))
    return 0


# --- cli ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="link_probe.py",
        description="Sample, merge and summarise the vehicle -> pit link (P4.0).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--role", choices=("vehicle", "pit"), help="sample this host's endpoints")
    mode.add_argument("--merge", nargs=2, metavar=("CSV", "CSV"), help="join a vehicle and pit run")
    mode.add_argument("--summary", metavar="CSV", help="summarise a run or a merged frame")

    parser.add_argument("--out", help="output CSV (required for --role and --merge)")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S, help="seconds")
    parser.add_argument("--duration", type=float, help="stop after this many seconds")
    parser.add_argument("--monitor", default=DEFAULT_MONITOR, help="local nats-server monitoring")
    parser.add_argument("--stream", help="telemetry stream on this host")
    parser.add_argument("--consumer", default=DEFAULT_CONSUMER, help="durable consumer to report")
    parser.add_argument("--http-timeout", type=float, default=DEFAULT_HTTP_TIMEOUT_S)
    parser.add_argument("--health-base", default=DEFAULT_HEALTH_HOST, help="pit /health host")

    parser.add_argument("--nft-command", default=DEFAULT_NFT_COMMAND, help="'' disables nft")
    parser.add_argument("--nft-counter-out", default=DEFAULT_NFT_COUNTER_OUT)
    parser.add_argument("--nft-counter-in", default=DEFAULT_NFT_COUNTER_IN)
    parser.add_argument("--interface", help="/proc/net/dev fallback interface, e.g. enp2s0")

    parser.add_argument(
        "--radio-adapter",
        choices=("none", "auto", "iw", "ubus"),
        default="none",
        help="'auto' tries iw then ubus and records which answered",
    )
    parser.add_argument("--radio-host", help="local router ssh host (~/.ssh/config name)")
    parser.add_argument("--radio-iface", help="wireless interface on the router, e.g. wlan0")
    parser.add_argument("--radio-peer", help="MAC of the far end, when several are associated")
    parser.add_argument("--radio-timeout", type=float, default=DEFAULT_RADIO_TIMEOUT_S)
    parser.add_argument("--ssh-binary", default="ssh")
    parser.add_argument("--radio-config", help="channel/MCS as configured, for the manifest")

    parser.add_argument("--tick-ms", type=int, help="OPENLAPS_TICK_MS this run used")
    parser.add_argument("--profile", help="profile directory, hashed into the manifest")
    parser.add_argument("--signal-source", help="e.g. canplayer+bench_gps, live-car")
    parser.add_argument("--clock-method", help="how the two hosts' clocks were disciplined")
    parser.add_argument("--clock-offset-ms", type=float, help="measured offset at run start")
    parser.add_argument("--clock-source", help="sys.host.clock_source at run start")
    parser.add_argument("--note", action="append", default=[], help="operator note (repeatable)")
    parser.add_argument("--manifest", help="manifest path (default: <out>.manifest.json)")
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_INTERVAL_S / 2.0,
        help="--merge: how far apart two samples may be and still align",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.merge:
            return run_merge(args)
        if args.summary:
            return run_summary(args)
        if not args.out:
            raise ProbeError("--role needs --out")
        if args.stream is None:
            args.stream = DEFAULT_STREAM[args.role]
        return run_sampler(args)
    except ProbeError as exc:
        print(f"link_probe: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
