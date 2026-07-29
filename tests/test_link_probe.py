"""tools/link_probe.py: parsers against captured fixtures, and the counter
arithmetic where a silent measurement bug would live.

The parsers are driven from `tests/fixtures/probe/` (see its README for
per-fixture provenance) rather than from hand-built dicts, so they are
pinned to the shapes a real bench emits. Everything else here is about the
three ways a delta can lie -- a wrapped counter, a restarted server, an
irregular sample interval -- plus the guarantee the whole instrument rests
on: an unreachable endpoint yields nulls and a reason, never an exception
and never a zero.

No live hardware, no docker: every test here runs in CI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import link_probe  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "probe"


def argv(**options: object) -> list[str]:
    """``--flag value`` pairs from keyword names, so long CLI cases stay readable."""
    flags: list[str] = []
    for name, value in options.items():
        flags += [f"--{name.replace('_', '-')}", str(value)]
    return flags


def load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- /varz -------------------------------------------------------------------


def test_varz_takes_the_totals_and_the_restart_marker():
    row = link_probe.parse_varz(load_json("varz-vehicle.json"))
    assert row["varz_ok"] == 1
    assert row["varz_in_msgs"] == 303
    assert row["varz_out_msgs"] == 606
    assert row["varz_in_bytes"] == 211674
    assert row["varz_slow_consumers"] == 0
    # `start` is what restart detection compares; without it a restart-to-zero
    # would read as a counter wrap.
    assert row["varz_start"].startswith("2026-")


def test_varz_missing_keys_are_null_not_zero():
    row = link_probe.parse_varz({})
    assert row["varz_ok"] == 1
    assert row["varz_in_bytes"] is None


# --- /leafz ------------------------------------------------------------------


def test_leafz_reads_the_link_counters_and_rtt():
    row = link_probe.parse_leafz(load_json("leafz-pit.json"))
    assert row["leafz_ok"] == 1
    assert row["leaf_count"] == 1
    assert row["leaf_in_msgs"] == 304
    assert row["leaf_in_bytes"] == 211526
    assert row["leaf_out_bytes"] == 303
    assert row["leaf_rtt_ms"] == pytest.approx(0.327, abs=1e-4)


def test_leafz_direction_is_mirrored_across_the_two_ends():
    """Forward traffic is `out` on the vehicle and `in` on the pit.

    The two fixtures are the same link sampled from both ends, so this is the
    cross-check that `link_direction()` is not inverted -- which would flip
    LINK_BUDGET.md §8's reverse-channel result on its head.
    """
    vehicle = link_probe.parse_leafz(load_json("leafz-vehicle.json"))
    pit = link_probe.parse_leafz(load_json("leafz-pit.json"))
    forward_vehicle, reverse_vehicle = link_probe.link_direction("vehicle")
    forward_pit, reverse_pit = link_probe.link_direction("pit")
    assert vehicle[forward_vehicle] > vehicle[reverse_vehicle]
    assert pit[forward_pit] > pit[reverse_pit]
    # The two fixtures were captured seconds apart from the same link, and
    # the forward byte count agrees exactly across them.
    assert vehicle[forward_vehicle] == pit[forward_pit] == 211526
    assert vehicle[reverse_vehicle] == pit[reverse_pit] == 303


def test_leafz_with_no_connections_reports_zero_leaves_and_null_counters():
    """A severed link: zero connections is a measurement, its byte counters
    are not. Reporting them as 0 would show a healthy idle link."""
    row = link_probe.parse_leafz({"leafnodes": 0, "leafs": []})
    assert row["leaf_count"] == 0
    assert row["leaf_in_bytes"] is None
    assert row["leaf_rtt_ms"] is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("787µs", 0.787),
        ("702us", 0.702),
        ("1.5ms", 1.5),
        ("2s", 2000.0),
        (None, None),
        ("", None),
        (3.25, 3.25),
    ],
)
def test_go_duration_parsing(value, expected):
    result = link_probe.parse_duration_ms(value)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected, rel=1e-6)


# --- /jsz --------------------------------------------------------------------


def test_jsz_reads_stream_state_and_the_durable_consumer():
    row = link_probe.parse_jsz(load_json("jsz-pit.json"), "TELE_VEHICLE", "ingest-writer")
    assert row["jsz_ok"] == 1
    assert row["stream_messages"] == 300
    assert row["stream_first_seq"] == 1
    assert row["stream_last_seq"] == 300
    assert row["consumer_name"] == "ingest-writer"
    assert row["consumer_num_pending"] == 250
    assert row["consumer_num_ack_pending"] == 20
    assert row["consumer_num_redelivered"] == 5
    assert row["consumer_delivered_stream_seq"] == 50
    assert row["consumer_ack_floor_stream_seq"] == 30
    # The sourced stream carries its source's lag, which is the pit's own
    # view of how far behind it is.
    assert row["source_lag"] == 0


def test_jsz_reads_the_vehicle_side_of_the_same_link():
    row = link_probe.parse_jsz(load_json("jsz-vehicle.json"), "TELE", "ingest-writer")
    assert row["stream_last_seq"] == 300
    assert row["stream_consumer_count"] == 0
    # No consumer on the vehicle's TELE: null, and no exception.
    assert row["consumer_name"] is None
    assert row["consumer_num_pending"] is None


def test_jsz_absent_stream_is_a_fact_not_a_failure():
    """The server answered; it simply has no such stream. jsz_ok stays 1 --
    provisioning that has not run yet is a different problem from a pit that
    cannot be reached, and the CSV must be able to say which."""
    row = link_probe.parse_jsz(load_json("jsz-vehicle.json"), "TELE_VEHICLE", "ingest-writer")
    assert row["jsz_ok"] == 1
    assert row["stream_messages"] is None
    assert row["stream_last_seq"] is None


def test_backlog_is_the_vehicle_minus_the_pit():
    vehicle = link_probe.parse_jsz(load_json("jsz-vehicle.json"), "TELE", "ingest-writer")
    pit = link_probe.parse_jsz(load_json("jsz-pit.json"), "TELE_VEHICLE", "ingest-writer")
    assert link_probe._backlog(vehicle, pit) == 0
    assert link_probe._backlog({"stream_last_seq": 900}, pit) == 600
    assert link_probe._backlog(vehicle, {}) is None


# --- wire counters -----------------------------------------------------------


def test_nft_named_counters():
    row = link_probe.parse_nft_counters(
        load_json("nft-counters.json"), "openlaps_leaf_out", "openlaps_leaf_in"
    )
    assert row["wire_source"] == "nft"
    assert row["wire_out_bytes"] == 4186480
    assert row["wire_out_packets"] == 3586
    assert row["wire_in_bytes"] == 186272


def test_nft_without_the_named_counters_returns_nothing_so_the_fallback_runs():
    assert link_probe.parse_nft_counters(load_json("nft-counters.json"), "nope", "also-nope") == {}


def test_proc_net_dev_finds_one_interface_among_many():
    text = load_text("proc-net-dev.txt")
    row = link_probe.parse_proc_net_dev(text, "enp2s0")
    assert row["wire_source"] == "procnetdev"
    assert row["wire_out_bytes"] > 0
    assert row["wire_in_bytes"] > 0
    assert row["wire_out_packets"] > 0


def test_proc_net_dev_unknown_interface_is_empty_not_zero():
    assert link_probe.parse_proc_net_dev(load_text("proc-net-dev.txt"), "wlan9") == {}


def test_wire_counters_fall_back_to_the_interface_and_say_so():
    """The recorded source is the point: a run must never be silently less
    precise than it looks."""
    wire = link_probe.WireCounters(
        nft_command=(),
        counter_out="openlaps_leaf_out",
        counter_in="openlaps_leaf_in",
        interface="enp2s0",
        proc_path=FIXTURES / "proc-net-dev.txt",
    )
    row, reason = wire.sample()
    assert row["wire_source"] == "procnetdev"
    assert reason is None


def test_wire_counters_with_no_source_report_a_reason_and_no_columns():
    wire = link_probe.WireCounters(nft_command=(), counter_out="a", counter_in="b", interface=None)
    row, reason = wire.sample()
    assert row == {}
    assert "no wire counter source" in reason


def test_wire_counters_survive_an_nft_that_is_not_installed():
    """The probe runs where nft may be absent or unprivileged; that has to
    degrade to the interface counter, not end the run."""
    wire = link_probe.WireCounters(
        nft_command=("definitely-not-a-real-binary", "-j", "list", "counters"),
        counter_out="openlaps_leaf_out",
        counter_in="openlaps_leaf_in",
        interface="enp2s0",
        proc_path=FIXTURES / "proc-net-dev.txt",
    )
    row, reason = wire.sample()
    assert row["wire_source"] == "procnetdev"
    assert "nft:" in reason


# --- radio -------------------------------------------------------------------


def test_iw_station_dump():
    stations = link_probe.parse_iw_station_dump(load_text("iw-station-dump.txt"))
    assert len(stations) == 1
    station = stations[0]
    assert station["mac"] == "8c:1f:64:a2:71:0e"
    assert station["signal_dbm"] == -67.0
    assert station["tx_bitrate_mbps"] == 3.9
    assert station["rx_bitrate_mbps"] == 3.9
    assert station["tx_retries"] == 9421
    assert station["tx_failed"] == 37
    assert station["expected_throughput_mbps"] == 1.95


def test_iw_sparse_driver_output_leaves_unreported_fields_null():
    """The HaLow vendor drivers populate a subset. Absent is not zero: a
    `tx_failed` of 0 would read as a clean link during a range walk."""
    stations = link_probe.parse_iw_station_dump(load_text("iw-station-dump-sparse.txt"))
    station = stations[0]
    assert station["signal_dbm"] == -67.0
    assert "tx_bitrate_mbps" not in station
    assert "tx_failed" not in station
    row = link_probe.radio_row(stations, "iw", None)
    assert row["radio_signal_dbm"] == -67.0
    assert row["radio_tx_failed"] is None


def test_radio_row_selects_the_named_peer():
    stations = link_probe.parse_iw_station_dump(load_text("iw-station-dump-two.txt"))
    assert len(stations) == 2
    first = link_probe.radio_row(stations, "iw", None)
    assert first["radio_signal_dbm"] == -67.0
    named = link_probe.radio_row(stations, "iw", "04:F0:21:1C:98:B3")
    assert named["radio_signal_dbm"] == -42.0
    assert named["radio_tx_bitrate_mbps"] == 6.5


def test_ubus_assoclist_converts_kbit_to_mbit():
    stations = link_probe.parse_ubus_assoclist(load_json("ubus-assoclist.json"))
    assert stations[0]["mac"] == "8c:1f:64:a2:71:0e"
    assert stations[0]["tx_bitrate_mbps"] == 3.9
    assert stations[0]["rx_bitrate_mbps"] == 3.9
    assert stations[0]["tx_retries"] == 9421
    assert stations[0]["tx_failed"] == 37
    assert stations[0]["signal_dbm"] == -67.0
    assert stations[1]["tx_bitrate_mbps"] == 6.5


def test_iw_and_ubus_agree_on_the_same_station():
    """The two adapters read the same radio; a run that switched between them
    mid-walk must not show a step change that is really a parser difference."""
    from_iw = link_probe.radio_row(
        link_probe.parse_iw_station_dump(load_text("iw-station-dump-two.txt")), "iw", None
    )
    from_ubus = link_probe.radio_row(
        link_probe.parse_ubus_assoclist(load_json("ubus-assoclist.json")), "ubus", None
    )
    for column in ("radio_signal_dbm", "radio_tx_bitrate_mbps", "radio_tx_retries"):
        assert from_iw[column] == from_ubus[column]


def test_radio_adapter_records_a_reason_when_ssh_fails():
    adapter = link_probe.RadioAdapter(
        adapter="auto",
        host="203.0.113.1",
        iface="wlan0",
        ssh_binary="definitely-not-a-real-binary",
        timeout_s=1.0,
    )
    row, reason = adapter.sample()
    assert row == {}
    assert "iw:" in reason and "ubus:" in reason


def test_radio_row_with_no_stations_is_reachable_but_unassociated():
    """The router answered and nothing is associated -- the far end of a
    severed link. That is a real reading, so radio_ok is 1."""
    row = link_probe.radio_row([], "iw", None)
    assert row == {"radio_ok": 1, "radio_source": "iw"}


# --- pit health --------------------------------------------------------------


def test_pit_health_parsers_take_the_keys_that_matter():
    ingest = link_probe.parse_ingest_health(load_json("health-ingest.json"))
    assert ingest["ingest_rows_per_s"] == 4100.0
    assert ingest["ingest_flushes_per_s"] == 50.0
    assert ingest["ingest_last_stream_seq"] == 91700
    assert ingest["ingest_unknown_seq_batches"] == 0
    assert ingest["ingest_dropped_flushes"] == 0

    live = link_probe.parse_live_health(load_json("health-live.json"))
    assert live["live_publish_rate"] == 37.0
    assert live["live_aggregate_sheds"] == 0
    assert live["live_mqtt_drops"] == 0

    ntrip = link_probe.parse_ntrip_health(load_json("health-ntrip.json"))
    assert ntrip["ntrip_bytes_per_s"] == 1240.0
    assert ntrip["ntrip_last_byte_age_s"] == 0.0

    session = link_probe.parse_session_health(load_json("health-session.json"))
    assert session["session_db_connected"] == 1
    assert session["session_db_errors"] == 0


def test_health_parsers_null_missing_keys():
    """A service on an older build serves fewer keys; that must null the
    column rather than fail the sample or invent a zero."""
    assert link_probe.parse_ingest_health({})["ingest_lag_ms"] is None
    assert link_probe.parse_ntrip_health({})["ntrip_last_byte_age_s"] is None
    assert link_probe.parse_session_health({})["session_db_connected"] is None


# --- counter arithmetic ------------------------------------------------------


def test_counter_delta_normal_case():
    assert link_probe.counter_delta(100, 250) == 150


def test_counter_delta_missing_reading_is_null():
    assert link_probe.counter_delta(None, 250) is None
    assert link_probe.counter_delta(100, None) is None
    assert link_probe.counter_delta(None, None) is None


def test_counter_delta_wrap_is_carried_not_dropped():
    """A 32-bit interface counter really does wrap on a 32-bit SBC."""
    width = 32
    previous = 2**32 - 1000
    assert link_probe.counter_delta(previous, 500, width_bits=width) == 1500


def test_counter_delta_restart_to_zero_is_null_not_a_negative_rate():
    """A restarted nats-server zeroes its counters. Reporting the new value
    as the delta would invent traffic; reporting the difference would emit a
    negative rate. Neither is a measurement."""
    assert link_probe.counter_delta(500_000, 12, reset=True) is None
    # Even without the restart flag, a decrease from the bottom of the range
    # is not a plausible wrap.
    assert link_probe.counter_delta(500_000, 12) is None


def test_counter_delta_ignores_non_numeric_readings():
    assert link_probe.counter_delta("", 12) is None
    assert link_probe.counter_delta("abc", "def") is None


def _row(t_unix, **columns):
    row = dict.fromkeys(link_probe.SAMPLE_COLUMNS)
    row.update({"t_unix": t_unix, "role": "vehicle"}, **columns)
    return row


def test_derive_divides_by_real_elapsed_time_not_the_nominal_interval():
    """A probe that was delayed must report the correct average over the real
    gap, not an inflated rate over the interval it meant to sample."""
    rows = [
        _row(1000.0, varz_start="s", leaf_out_bytes=0, leaf_in_bytes=0),
        # Two seconds late: 136,100 bytes over 3 s, not over 1 s.
        _row(1003.0, varz_start="s", leaf_out_bytes=204_150, leaf_in_bytes=1_200),
    ]
    derived = link_probe.derive(rows)
    assert derived[0]["fwd_nats_kbit_s"] is None  # nothing to difference against
    assert derived[1]["dt_s"] == 3.0
    assert derived[1]["fwd_nats_kbit_s"] == pytest.approx(544.4, rel=1e-3)


def test_derive_reports_the_reverse_ratio_as_a_first_class_number():
    rows = [
        _row(1000.0, varz_start="s", leaf_out_bytes=0, leaf_in_bytes=0),
        _row(1001.0, varz_start="s", leaf_out_bytes=68_050, leaf_in_bytes=1_361),
    ]
    derived = link_probe.derive(rows)
    assert derived[1]["rev_nats_kbit_s"] == pytest.approx(10.888, rel=1e-3)
    assert derived[1]["reverse_ratio"] == pytest.approx(0.02, rel=1e-6)


def test_derive_nulls_the_interval_that_spans_a_restart():
    rows = [
        _row(1000.0, varz_start="A", leaf_out_bytes=900_000, leaf_in_bytes=9_000),
        _row(1001.0, varz_start="B", leaf_out_bytes=1_200, leaf_in_bytes=40),
        _row(1002.0, varz_start="B", leaf_out_bytes=69_250, leaf_in_bytes=1_401),
    ]
    derived = link_probe.derive(rows)
    assert derived[1]["nats_restarted"] == 1
    assert derived[1]["fwd_nats_kbit_s"] is None
    # The interval after the restart is measurable again.
    assert derived[2]["nats_restarted"] == 0
    assert derived[2]["fwd_nats_kbit_s"] == pytest.approx(544.4, rel=1e-3)


def test_derive_computes_framing_overhead_from_wire_over_nats():
    """LINK_BUDGET.md §2 estimates TCP/IP + 802.11 framing at 6-13% and does
    not model it; this ratio is that estimate meeting a measurement."""
    rows = [
        _row(
            1000.0,
            varz_start="s",
            leaf_out_bytes=0,
            leaf_in_bytes=0,
            wire_source="nft",
            wire_out_bytes=0,
            wire_in_bytes=0,
        ),
        _row(
            1001.0,
            varz_start="s",
            leaf_out_bytes=68_050,
            leaf_in_bytes=1_361,
            wire_source="nft",
            wire_out_bytes=72_500,
            wire_in_bytes=2_000,
        ),
    ]
    derived = link_probe.derive(rows)
    assert derived[1]["framing_overhead"] == pytest.approx(72_500 / 68_050, rel=1e-6)
    assert derived[1]["fwd_wire_kbit_s"] == pytest.approx(580.0, rel=1e-3)


def test_derive_refuses_to_difference_two_different_wire_sources():
    """nft counts the leafnode; /proc/net/dev counts the interface. A delta
    across the switch is a jump between two different quantities."""
    rows = [
        _row(1000.0, varz_start="s", wire_source="nft", wire_out_bytes=5_000),
        _row(1001.0, varz_start="s", wire_source="procnetdev", wire_out_bytes=990_000),
    ]
    derived = link_probe.derive(rows)
    assert derived[1]["fwd_wire_kbit_s"] is None
    assert derived[1]["framing_overhead"] is None


def test_derive_measures_airtime_efficiency_against_the_reported_phy_rate():
    """LINK_BUDGET.md §5 assumes 0.5 and §8 calls it the softest input."""
    rows = [
        _row(
            1000.0,
            varz_start="s",
            wire_source="nft",
            wire_out_bytes=0,
            wire_in_bytes=0,
            radio_tx_bitrate_mbps=3.9,
        ),
        _row(
            1001.0,
            varz_start="s",
            wire_source="nft",
            wire_out_bytes=243_750,
            wire_in_bytes=0,
            radio_tx_bitrate_mbps=3.9,
        ),
    ]
    derived = link_probe.derive(rows)
    # 243,750 B/s = 1.95 Mbit/s over a 3.9 Mbit/s PHY.
    assert derived[1]["airtime_efficiency"] == pytest.approx(0.5, rel=1e-6)


def test_derive_on_a_pit_run_uses_the_mirrored_direction():
    rows = [
        {**_row(1000.0, varz_start="s", leaf_in_bytes=0, leaf_out_bytes=0), "role": "pit"},
        {
            **_row(1001.0, varz_start="s", leaf_in_bytes=68_050, leaf_out_bytes=1_361),
            "role": "pit",
        },
    ]
    derived = link_probe.derive(rows)
    assert derived[1]["fwd_nats_kbit_s"] == pytest.approx(544.4, rel=1e-3)
    assert derived[1]["rev_nats_kbit_s"] == pytest.approx(10.888, rel=1e-3)


def test_derive_of_an_empty_run_is_empty():
    assert link_probe.derive([]) == []


# --- merge -------------------------------------------------------------------


def _pit_row(t_unix, **columns):
    row = dict.fromkeys(link_probe.SAMPLE_COLUMNS)
    row.update({"t_unix": t_unix, "role": "pit"}, **columns)
    return row


def test_merge_aligns_two_probes_started_on_different_phases():
    """Two probes started by hand never share a phase; a 0.3 s offset must
    still align."""
    vehicle = [_row(1000.0, stream_last_seq=500), _row(1001.0, stream_last_seq=550)]
    pit = [_pit_row(1000.3, stream_last_seq=498), _pit_row(1001.3, stream_last_seq=545)]
    merged = link_probe.merge(vehicle, pit, tolerance_s=0.5)
    assert [row["pit_matched"] for row in merged] == [1, 1]
    assert merged[0]["pit_offset_s"] == 0.3
    assert merged[0]["sourcing_backlog"] == 2
    assert merged[1]["sourcing_backlog"] == 5


def test_merge_leaves_pit_columns_null_across_a_pit_side_gap():
    """The pit probe stopped answering -- which is what a sever looks like
    from one side. Those rows must survive with null pit columns, not vanish:
    they are the samples that matter."""
    vehicle = [_row(1000.0 + n, stream_last_seq=500 + n * 50) for n in range(5)]
    pit = [_pit_row(1000.1, stream_last_seq=498), _pit_row(1004.1, stream_last_seq=680)]
    merged = link_probe.merge(vehicle, pit, tolerance_s=0.5)
    assert len(merged) == 5
    assert [row["pit_matched"] for row in merged] == [1, 0, 0, 0, 1]
    assert merged[2]["pit_stream_last_seq"] is None
    assert merged[2]["sourcing_backlog"] is None
    assert merged[4]["sourcing_backlog"] == 20


def test_merge_keeps_every_vehicle_sample():
    vehicle = [_row(1000.0 + n) for n in range(10)]
    merged = link_probe.merge(vehicle, [], tolerance_s=0.5)
    assert len(merged) == 10
    assert all(row["pit_matched"] == 0 for row in merged)


def test_merge_prefixes_both_roles_so_nothing_collides():
    vehicle = [_row(1000.0, leaf_out_bytes=10), _row(1001.0, leaf_out_bytes=20)]
    pit = [_pit_row(1000.0, leaf_in_bytes=10), _pit_row(1001.0, leaf_in_bytes=20)]
    merged = link_probe.merge(vehicle, pit)
    assert merged[1]["vehicle_leaf_out_bytes"] == 20
    assert merged[1]["pit_leaf_in_bytes"] == 20
    assert merged[1]["vehicle_role"] == "vehicle"
    assert merged[1]["pit_role"] == "pit"


# --- summary -----------------------------------------------------------------


def test_percentiles():
    values = list(range(1, 101))
    assert link_probe.percentile(values, 0.5) == pytest.approx(50.5)
    assert link_probe.percentile(values, 0.95) == pytest.approx(95.05)
    assert link_probe.percentile([7.0], 0.95) == 7.0
    with pytest.raises(ValueError):
        link_probe.percentile([], 0.5)


def test_summarise_counts_nulls_beside_values():
    """An endpoint unreachable for half a run has to be visible in the
    summary, or the mean silently describes a different run."""
    rows = [{"a": 1.0, "b": None}, {"a": 3.0, "b": 2.0}, {"a": None, "b": 4.0}]
    stats = {stat.name: stat for stat in link_probe.summarise(rows)}
    assert stats["a"].count == 2
    assert stats["a"].nulls == 1
    assert stats["a"].mean == 2.0
    assert stats["b"].count == 2
    assert stats["b"].maximum == 4.0


def test_summarise_skips_columns_with_nothing_numeric():
    stats = {stat.name for stat in link_probe.summarise([{"role": "vehicle", "n": 1}])}
    assert stats == {"n"}


def test_summarise_skips_timestamps_at_every_prefix():
    """A wall-clock instant is numeric but is not a series; its p95 is noise
    that pushes the columns that matter off the screen."""
    row = {"t_unix": 1785324891.7, "vehicle_t_unix": 1785324891.7, "pit_t_unix": 1785324891.8}
    assert link_probe.summarise([row, row]) == []


def test_model_comparison_reports_measured_beside_modelled():
    rows = [{"fwd_nats_kbit_s": 544.4}, {"fwd_nats_kbit_s": 544.4}]
    lines = link_probe.model_comparison(rows, 20)
    assert "measured mean 544.4 kbit/s" in lines[0]
    assert "modelled 544.4 kbit/s at OPENLAPS_TICK_MS=20" in lines[1]
    assert "+0.0%" in lines[1]


def test_model_comparison_covers_both_ticks_when_none_is_named():
    lines = link_probe.model_comparison([{"fwd_nats_kbit_s": 500.0}], None)
    assert len(lines) == 3
    assert "OPENLAPS_TICK_MS=10" in lines[1]
    assert "OPENLAPS_TICK_MS=20" in lines[2]


def test_model_comparison_finds_the_merged_frame_column_too():
    lines = link_probe.model_comparison([{"vehicle_fwd_nats_kbit_s": 600.0}], 20)
    assert "vehicle_fwd_nats_kbit_s" in lines[0]


def test_model_comparison_says_so_when_there_is_nothing_to_compare():
    assert "no samples" in link_probe.model_comparison([{"fwd_nats_kbit_s": None}], 20)[0]


# --- csv round trip ----------------------------------------------------------


def test_csv_round_trip_preserves_null_versus_zero(tmp_path):
    """The distinction the whole instrument rests on has to survive the file
    boundary: empty means 'could not ask', 0 means 'asked, answer was zero'."""
    path = tmp_path / "run.csv"
    rows = [_row(1000.0, leaf_out_bytes=0, leaf_in_bytes=None, varz_slow_consumers=0)]
    link_probe.write_csv(path, rows, link_probe.SAMPLE_COLUMNS)
    read = link_probe.read_csv(path)
    assert read[0]["leaf_out_bytes"] == "0"
    assert read[0]["leaf_in_bytes"] is None
    assert read[0]["varz_slow_consumers"] == "0"
    assert link_probe.counter_delta(read[0]["leaf_out_bytes"], 500) == 500
    assert link_probe.counter_delta(read[0]["leaf_in_bytes"], 500) is None


def test_derive_works_on_a_csv_read_back_from_disk(tmp_path):
    """Everything downstream reads a file, not the sampler's dicts, so the
    arithmetic must hold over strings."""
    path = tmp_path / "run.csv"
    rows = [
        _row(1000.0, varz_start="s", leaf_out_bytes=0, leaf_in_bytes=0),
        _row(1001.0, varz_start="s", leaf_out_bytes=68_050, leaf_in_bytes=1_361),
    ]
    link_probe.write_csv(path, rows, link_probe.SAMPLE_COLUMNS)
    derived = link_probe.derive(link_probe.read_csv(path))
    assert derived[1]["fwd_nats_kbit_s"] == pytest.approx(544.4, rel=1e-3)
    assert derived[1]["reverse_ratio"] == pytest.approx(0.02, rel=1e-6)


# --- the sampler under failure ----------------------------------------------


def _sampler(monitor_url="http://127.0.0.1:1", **kwargs):
    defaults = dict(
        role="vehicle",
        monitor_url=monitor_url,
        stream="TELE",
        consumer="ingest-writer",
        wire=None,
        radio=None,
        health_base="http://127.0.0.1",
        health_ports={},
        http_timeout_s=0.25,
        host="bench",
    )
    defaults.update(kwargs)
    return link_probe.Sampler(**defaults)


def test_an_unreachable_server_yields_a_null_row_with_a_reason_and_no_exception():
    """The ground rule the instrument exists under: a probe that dies when
    the link degrades measures nothing about the interesting part of a run."""
    row = _sampler().sample(now=1000.0)
    assert set(row) == set(link_probe.SAMPLE_COLUMNS)
    assert row["varz_ok"] == 0
    assert row["leafz_ok"] == 0
    assert row["jsz_ok"] == 0
    assert row["leaf_out_bytes"] is None
    assert row["stream_last_seq"] is None
    assert "varz=" in row["reasons"]
    assert row["t_unix"] == 1000.0
    assert row["role"] == "vehicle"


def test_one_dead_endpoint_does_not_null_the_others(monkeypatch):
    """A pit whose live-decoder is restarting still measures its link."""
    payloads = {
        "/varz": load_json("varz-pit.json"),
        "/leafz": load_json("leafz-pit.json"),
        "/jsz?streams=1&consumers=1": load_json("jsz-pit.json"),
    }

    def fake_http_json(url, timeout_s):
        for suffix, payload in payloads.items():
            if url.endswith(suffix):
                return payload
        if url.endswith(":8081/health"):
            return load_json("health-ingest.json")
        raise OSError("connection refused")

    monkeypatch.setattr(link_probe, "http_json", fake_http_json)
    sampler = _sampler(
        role="pit",
        stream="TELE_VEHICLE",
        monitor_url="http://pit:8222",
        health_ports=dict(link_probe.HEALTH_PORTS),
    )
    row = sampler.sample(now=1000.0)
    assert row["leafz_ok"] == 1
    assert row["leaf_in_bytes"] == 211526
    assert row["ingest_ok"] == 1
    assert row["ingest_rows_per_s"] == 4100.0
    assert row["live_ok"] == 0
    assert row["live_publish_rate"] is None
    assert row["ntrip_ok"] == 0
    assert "live=" in row["reasons"] and "ntrip=" in row["reasons"]


def test_a_failing_radio_adapter_never_stops_a_sample():
    sampler = _sampler(
        radio=link_probe.RadioAdapter(
            adapter="iw",
            host="203.0.113.1",
            iface="wlan0",
            ssh_binary="definitely-not-a-real-binary",
            timeout_s=1.0,
        )
    )
    row = sampler.sample(now=1000.0)
    assert row["radio_ok"] == 0
    assert row["radio_signal_dbm"] is None
    assert "radio=" in row["reasons"]


def test_reasons_stay_on_one_csv_line():
    """A multi-line traceback in a CSV cell corrupts every downstream read."""
    row = _sampler().sample(now=1000.0)
    assert "\n" not in row["reasons"]


# --- manifest ----------------------------------------------------------------


def test_manifest_carries_provenance_and_no_credentials(tmp_path):
    parser = link_probe.build_parser()
    args = parser.parse_args(
        argv(
            role="vehicle",
            out=tmp_path / "run.csv",
            tick_ms=20,
            signal_source="canplayer+bench_gps",
            radio_adapter="auto",
            radio_host="halow-vehicle",
            radio_iface="wlan0",
            radio_config="2 MHz MCS4",
            clock_method="chrony against the SBC",
            clock_offset_ms=0.8,
            clock_source="gps",
            note="antenna at the bench edge",
            profile="profiles/example-club-racer",
        )
    )
    args.stream = link_probe.DEFAULT_STREAM[args.role]
    manifest = link_probe.build_manifest(args, tmp_path / "run.csv")
    assert manifest["tick_ms"] == 20
    assert manifest["signal_source"] == "canplayer+bench_gps"
    assert manifest["radio"]["config"] == "2 MHz MCS4"
    assert manifest["clock"] == {
        "method": "chrony against the SBC",
        "offset_ms": 0.8,
        "source": "gps",
    }
    assert manifest["notes"] == ["antenna at the bench edge"]
    assert manifest["git_sha"] is None or len(manifest["git_sha"]) == 40
    serialised = json.dumps(manifest).lower()
    for secret in ("password", "passwd", "secret", "token", "api_key"):
        assert secret not in serialised


def test_manifest_hashes_the_profile_without_bumping_its_registry_generation():
    """`build_runtime_catalog` bumps the persisted generation as a side
    effect. A measurement tool must never change the thing it records, so the
    manifest hashes the file directly."""
    profile = Path(__file__).parents[1] / "profiles" / "example-club-racer"
    state = profile / ".registry-state.json"
    before = state.read_bytes() if state.exists() else None
    provenance = link_probe.profile_provenance(str(profile))
    assert len(provenance["catalog_sha256"]) == 64
    after = state.read_bytes() if state.exists() else None
    assert before == after


def test_profile_provenance_of_a_missing_profile_reports_instead_of_raising():
    provenance = link_probe.profile_provenance("/nonexistent/profile")
    assert provenance["catalog_sha256"] is None
    assert "catalog_error" in provenance


def test_no_profile_is_an_empty_block():
    assert link_probe.profile_provenance(None) == {}


# --- cli ---------------------------------------------------------------------


def test_cli_requires_a_mode():
    with pytest.raises(SystemExit):
        link_probe.build_parser().parse_args([])


def test_cli_role_without_out_is_an_error(capsys):
    assert link_probe.main(["--role", "vehicle"]) == 2
    assert "--out" in capsys.readouterr().err


def test_cli_radio_adapter_without_a_host_is_an_error(capsys, tmp_path):
    code = link_probe.main(
        ["--role", "vehicle", "--out", str(tmp_path / "x.csv"), "--radio-adapter", "iw"]
    )
    assert code == 2
    assert "--radio-host" in capsys.readouterr().err


def test_cli_merge_rejects_two_runs_of_the_same_role(tmp_path, capsys):
    first, second = tmp_path / "a.csv", tmp_path / "b.csv"
    for path in (first, second):
        link_probe.write_csv(path, [_row(1000.0)], link_probe.SAMPLE_COLUMNS)
    code = link_probe.main(["--merge", str(first), str(second), "--out", str(tmp_path / "m.csv")])
    assert code == 2
    assert "one of each" in capsys.readouterr().err


def test_cli_merge_and_summary_round_trip(tmp_path, capsys):
    vehicle_path, pit_path = tmp_path / "veh.csv", tmp_path / "pit.csv"
    link_probe.write_csv(
        vehicle_path,
        [
            _row(1000.0, varz_start="s", leaf_out_bytes=0, leaf_in_bytes=0, stream_last_seq=100),
            _row(
                1001.0,
                varz_start="s",
                leaf_out_bytes=68_050,
                leaf_in_bytes=1_361,
                stream_last_seq=150,
            ),
        ],
        link_probe.SAMPLE_COLUMNS,
    )
    link_probe.write_csv(
        pit_path,
        [
            _pit_row(1000.1, varz_start="p", leaf_in_bytes=0, stream_last_seq=98),
            _pit_row(1001.1, varz_start="p", leaf_in_bytes=68_050, stream_last_seq=145),
        ],
        link_probe.SAMPLE_COLUMNS,
    )
    merged_path = tmp_path / "merged.csv"
    assert (
        link_probe.main(["--merge", str(vehicle_path), str(pit_path), "--out", str(merged_path)])
        == 0
    )
    merged = link_probe.read_csv(merged_path)
    assert merged[1]["sourcing_backlog"] == "5"

    assert link_probe.main(["--summary", str(merged_path), "--tick-ms", "20"]) == 0
    output = capsys.readouterr().out
    assert "vehicle_fwd_nats_kbit_s" in output
    assert "LINK_BUDGET.md §3 modelled 544.4 kbit/s" in output


def test_cli_summary_of_a_single_role_run_derives_first(tmp_path, capsys):
    path = tmp_path / "veh.csv"
    link_probe.write_csv(
        path,
        [
            _row(1000.0, varz_start="s", leaf_out_bytes=0, leaf_in_bytes=0),
            _row(1001.0, varz_start="s", leaf_out_bytes=68_050, leaf_in_bytes=1_361),
        ],
        link_probe.SAMPLE_COLUMNS,
    )
    assert link_probe.main(["--summary", str(path), "--tick-ms", "20"]) == 0
    output = capsys.readouterr().out
    assert "fwd_nats_kbit_s" in output
    assert "544.4" in output


def test_sampler_writes_a_csv_and_a_manifest_end_to_end(tmp_path, monkeypatch):
    """The whole --role path against an unreachable stack: it still produces
    a well-formed run, which is what a probe started before the services are
    up has to do."""
    out = tmp_path / "run.csv"
    monkeypatch.setattr(link_probe.time, "sleep", lambda _: None)
    code = link_probe.main(
        argv(
            role="vehicle",
            out=out,
            interval=0.01,
            duration=0.02,
            monitor="http://127.0.0.1:1",
            http_timeout=0.05,
            nft_command="",
        )
    )
    assert code == 0
    rows = link_probe.read_csv(out)
    assert rows
    assert rows[0]["role"] == "vehicle"
    assert rows[0]["varz_ok"] == "0"
    manifest = json.loads((tmp_path / "run.manifest.json").read_text())
    assert manifest["role"] == "vehicle"
    assert manifest["stream"] == "TELE"
    assert manifest["samples"] == len(rows)
    assert manifest["started"] and manifest["ended"]


def test_sample_loop_resyncs_after_a_slow_sample(monkeypatch):
    """A sampler that fell behind must not fire a burst of back-to-back
    samples that all measure the same instant. It skips the missed slots and
    the gap shows up honestly as a longer dt_s in the derived frame."""
    clock = {"now": 0.0}
    monkeypatch.setattr(link_probe.time, "monotonic", lambda: clock["now"])

    class _Stop:
        def __init__(self):
            self.taken = 0

        def is_set(self):
            return self.taken >= 5

        def wait(self, timeout):
            clock["now"] += max(0.0, timeout)

    stop = _Stop()
    taken_at: list[float] = []

    class _Sampler:
        def sample(self):
            # The third sample blocks for 4.5 s on a 1 s grid.
            if len(taken_at) == 2:
                clock["now"] += 4.5
            taken_at.append(clock["now"])
            stop.taken += 1
            return {}

    link_probe.sample_loop(
        _Sampler(), interval_s=1.0, duration_s=None, stop=stop, emit=lambda row: None
    )
    gaps = [round(b - a, 3) for a, b in zip(taken_at, taken_at[1:], strict=False)]
    # No zero-length gap: the missed slots were skipped, not replayed.
    assert all(gap > 0 for gap in gaps)
    # And the loop is back on the original grid, not offset by the overrun.
    assert taken_at[-1] % 1.0 == pytest.approx(0.0, abs=1e-9)
