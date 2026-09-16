"""P6 contracts for the pit-monitor: the pit's own health into `pit_metrics`."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psycopg
import pytest

from pit.db.migrate import apply_migrations
from pit.pit_monitor.health import HealthState
from pit.pit_monitor.probes import (
    HostProbe,
    NatsProbe,
    NtripProbe,
    parse_duration_s,
    sanitize,
    split_host_reading,
)
from pit.pit_monitor.service import PitMonitorService, PitMonitorSettings
from pit.pit_monitor.store import PitMetricStore, to_rows

STAMP = datetime(2026, 8, 1, 4, 30, tzinfo=UTC)


class _FakeHostReader:
    """Stands in for HostMetricsReader, returning fixed source refs."""

    def __init__(self, readings, probe_failures=0):
        self._readings = readings
        self.stats = SimpleNamespace(probe_failures=probe_failures)

    def read(self):
        return list(self._readings)


class _FakeStore:
    """Records what the service asked to write, or refuses to write it."""

    def __init__(self, error: Exception | None = None):
        self.written = []
        self.closed = False
        self._error = error

    def write(self, rows):
        if self._error is not None:
            raise self._error
        self.written.append(list(rows))
        return len(rows)

    def close(self):
        self.closed = True


class _FixedProbe:
    def __init__(self, readings):
        self._readings = readings

    def read(self):
        yield from self._readings


class _BrokenProbe:
    def read(self):
        yield "nats", "connections", 3.0
        raise OSError("connection refused")


# --------------------------------------------------------------------- probes


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.53ms", 0.00153),
        ("250us", 0.00025),
        ("250µs", 0.00025),
        ("2s", 2.0),
        ("1.5m", 90.0),
        ("900ns", 9e-7),
        ("not a duration", None),
        ("", None),
    ],
)
def test_leafnode_rtt_is_parsed_from_the_go_duration_nats_reports(text, expected):
    if expected is None:
        assert parse_duration_s(text) is None
    else:
        assert parse_duration_s(text) == pytest.approx(expected)


def test_sanitize_makes_a_metric_name_fragment_of_any_server_name():
    assert sanitize("veh-nats.local") == "veh_nats_local"
    assert sanitize("  ") == "unknown"


def test_clock_readings_answer_under_chrony_and_the_rest_under_host():
    """The one reader covers two sources; the dashboard asks about them apart."""
    assert split_host_reading("host:clock_offset_s") == ("chrony", "offset_s")
    assert split_host_reading("host:clock_source") == ("chrony", "source")
    assert split_host_reading("host:cpu.percent") == ("host", "cpu.percent")
    assert split_host_reading("host:temp.coretemp.package_id_0") == (
        "host",
        "temp.coretemp.package_id_0",
    )


def test_host_probe_passes_collector_metric_names_through_unchanged():
    probe = HostProbe(
        reader=_FakeHostReader(
            [
                ("host:cpu.percent", 12.5),
                ("host:clock_offset_s", -0.000031),
                ("host:clock_source", "GPS"),
                ("host:net.err_in", 0),
            ]
        )
    )
    assert list(probe.read()) == [
        ("host", "cpu.percent", 12.5),
        ("chrony", "offset_s", -0.000031),
        ("chrony", "source", "GPS"),
        ("host", "net.err_in", 0),
        ("host", "probe_failures", 0.0),
    ]


def test_a_swallowed_metric_group_is_still_reported_as_a_number():
    """A dead chronyd must not look identical to a dead collector."""
    probe = HostProbe(reader=_FakeHostReader([("host:cpu.percent", 12.5)], probe_failures=1))
    assert ("host", "probe_failures", 1.0) in list(probe.read())


def test_ntrip_probe_reports_every_scalar_the_health_endpoint_carries(monkeypatch):
    """Not a fixed key list: /health is the ntrip-client's contract, not this one."""
    snapshot = {
        "connected": True,
        "bytes_per_s": 812.0,
        "reconnects": 2,
        "caster_host": "caster.example:2101",
        "mountpoint": "MOUNT0",
        "last_byte_age_s": None,
        "something_new": 7,
    }
    monkeypatch.setattr("pit.pit_monitor.probes.fetch_json", lambda url, timeout: snapshot)
    readings = dict(((metric, value) for _, metric, value in NtripProbe("http://x/health").read()))

    assert readings["connected"] == 1.0
    assert readings["bytes_per_s"] == 812.0
    assert readings["reconnects"] == 2.0
    assert readings["caster_host"] == "caster.example:2101"
    assert readings["mountpoint"] == "MOUNT0"
    assert readings["something_new"] == 7.0
    # Null is an absent reading, not a zero: "no bytes yet" and "a byte this
    # instant" must not draw the same point.
    assert "last_byte_age_s" not in readings


def test_nats_probe_reads_slow_consumers_stream_depth_and_the_leafnode(monkeypatch):
    documents = {
        "/varz": {
            "server_name": "pit-nats",
            "version": "2.12.1",
            "connections": 6,
            "leafnodes": 1,
            "slow_consumers": 0,
            "in_msgs": 41000,
        },
        "/jsz?streams=1&consumers=1": {
            "streams": 1,
            "messages": 90210,
            "account_details": [
                {
                    "stream_detail": [
                        {
                            "name": "TELE_VEHICLE",
                            "state": {"messages": 90210, "bytes": 4096, "consumer_count": 1},
                            "consumer_detail": [
                                {"name": "ingest-writer", "num_pending": 12, "num_ack_pending": 3}
                            ],
                        }
                    ]
                }
            ],
        },
        "/leafz": {
            "leafs": [
                {
                    "name": "veh-nats",
                    "rtt": "18.4ms",
                    "in_msgs": 41000,
                    "out_msgs": 12,
                    "in_bytes": 1200000,
                }
            ]
        },
    }
    monkeypatch.setattr(
        "pit.pit_monitor.probes.fetch_json",
        lambda url, timeout: documents[url.removeprefix("http://pit:8222")],
    )
    readings = dict(((metric, value) for _, metric, value in NatsProbe("http://pit:8222").read()))

    assert readings["slow_consumers"] == 0.0
    assert readings["connections"] == 6.0
    assert readings["server_name"] == "pit-nats"
    assert readings["js.messages"] == 90210.0
    assert readings["stream.tele_vehicle.messages"] == 90210.0
    assert readings["consumer.tele_vehicle.ingest_writer.num_pending"] == 12.0
    # The leafnode is how the pit sees both ends of the radio link without
    # dialling the vehicle for monitoring.
    assert readings["leafz.count"] == 1.0
    assert readings["leaf.veh_nats.rtt_s"] == pytest.approx(0.0184)
    assert readings["leaf.veh_nats.in_bytes"] == 1200000.0
    assert readings["leaf.veh_nats.name"] == "veh-nats"


def test_an_unnamed_leafnode_still_gets_a_stable_metric_name(monkeypatch):
    """A leaf mid-handshake has no name; two of them must not collide."""
    documents = {
        "/varz": {},
        "/jsz?streams=1&consumers=1": {},
        "/leafz": {
            "leafs": [
                {"ip": "10.0.0.2", "port": 7422, "in_msgs": 1},
                {"ip": "10.0.0.3", "port": 7422, "in_msgs": 2},
            ]
        },
    }
    monkeypatch.setattr(
        "pit.pit_monitor.probes.fetch_json",
        lambda url, timeout: documents[url.removeprefix("http://pit:8222")],
    )
    metrics = {metric for _, metric, _ in NatsProbe("http://pit:8222").read()}
    assert "leaf.10_0_0_2_7422.in_msgs" in metrics
    assert "leaf.10_0_0_3_7422.in_msgs" in metrics


# ---------------------------------------------------------------------- rows


def test_rows_split_numeric_and_text_values_into_their_own_columns():
    rows = to_rows([("host", "cpu.percent", 12.5), ("chrony", "source", "GPS")], STAMP)
    assert rows == [
        ("host", "cpu.percent", STAMP, 12.5, None),
        ("chrony", "source", STAMP, None, "GPS"),
    ]


# ------------------------------------------------------------------- service


def _settings(**overrides) -> PitMonitorSettings:
    return PitMonitorSettings(dsn="postgresql://unused", **overrides)


def test_a_failing_probe_costs_only_its_own_readings():
    """The service that reports on everything else must not be all-or-nothing."""
    store = _FakeStore()
    service = PitMonitorService(
        _settings(),
        store=store,
        probes={
            "host": _FixedProbe([("host", "cpu.percent", 3.0)]),
            "ntrip": _BrokenProbe(),
        },
    )

    written = service.poll()

    metrics = {(row[0], row[1]) for row in store.written[0]}
    assert ("host", "cpu.percent") in metrics
    # Partial output from the generator that raised is kept: half of a probe's
    # numbers beats none of them.
    assert ("nats", "connections") in metrics
    assert written == 2
    assert service.health.probe_failures == {"ntrip": 1}
    assert service.health.polls == 1


def test_a_database_failure_is_counted_and_the_loop_keeps_polling():
    store = _FakeStore(error=psycopg.OperationalError("server closed the connection"))
    service = PitMonitorService(
        _settings(), store=store, probes={"host": _FixedProbe([("host", "cpu.percent", 3.0)])}
    )

    assert service.poll() == 0
    assert service.health.db_errors == 1
    assert service.health.polls == 0
    assert "database" in (service.health.last_error or "")


def test_the_run_loop_stops_on_the_event_and_closes_the_store():
    store = _FakeStore()
    service = PitMonitorService(
        _settings(interval_s=0.01),
        store=store,
        probes={"host": _FixedProbe([("host", "cpu.percent", 3.0)])},
    )
    stop = threading.Event()
    stop.set()

    service.run(stop)

    assert store.closed


def test_settings_from_env_defaults_to_loopback_probes_and_port_8085():
    settings = PitMonitorSettings.from_env({"TIMESCALE_DSN": "postgresql://pit/openlaps"})
    assert settings.dsn == "postgresql://pit/openlaps"
    assert settings.health_port == 8085
    assert settings.interval_s == 5.0
    assert settings.ntrip_health_url == "http://127.0.0.1:8083/health"
    assert settings.nats_monitor_url == "http://127.0.0.1:8222"


def test_an_empty_probe_url_disables_that_probe_rather_than_failing_every_poll():
    """A pit not running RTK stops the ntrip-client on purpose; that is config."""
    settings = PitMonitorSettings.from_env(
        {
            "TIMESCALE_DSN": "postgresql://pit/openlaps",
            "OPENLAPS_PIT_MONITOR_NTRIP_URL": "",
        }
    )
    assert settings.ntrip_health_url is None

    service = PitMonitorService(settings, store=_FakeStore())
    assert "ntrip" not in service._probes
    assert {"host", "nats"} == set(service._probes)


def test_settings_from_env_rejects_a_non_positive_interval():
    with pytest.raises(ValueError, match="INTERVAL_S"):
        PitMonitorSettings.from_env(
            {"TIMESCALE_DSN": "postgresql://pit/openlaps", "OPENLAPS_PIT_MONITOR_INTERVAL_S": "0"}
        )


# --------------------------------------------------------------------- store


def test_a_poll_lands_in_pit_metrics_and_reads_back_through_the_view(timescale_dsn):
    with psycopg.connect(timescale_dsn) as conn:
        apply_migrations(conn)
        conn.commit()

    store = PitMetricStore(timescale_dsn)
    try:
        written = store.write(
            to_rows(
                [
                    ("host", "cpu.percent", 12.5),
                    ("chrony", "source", "GPS"),
                    ("nats", "leaf.veh_nats.rtt_s", 0.0184),
                ],
                STAMP,
            )
        )
        assert written == 3
    finally:
        store.close()

    with psycopg.connect(timescale_dsn) as conn:
        rows = conn.execute(
            "SELECT source, metric, value, value_text FROM v_pit_metrics "
            "WHERE time = %s ORDER BY source, metric",
            (STAMP,),
        ).fetchall()
    assert rows == [
        ("chrony", "source", None, "GPS"),
        ("host", "cpu.percent", 12.5, None),
        ("nats", "leaf.veh_nats.rtt_s", 0.0184, None),
    ]


def test_the_store_redials_rather_than_polling_into_a_dead_connection(timescale_dsn):
    """A collector that writes into a broken connection is its own blind spot.

    Two ways a connection goes bad, and both must leave the next poll working:
    the server hangs up between polls, and a statement fails and leaves the
    session in a state nobody should reuse.
    """
    with psycopg.connect(timescale_dsn) as conn:
        apply_migrations(conn)
        conn.commit()

    store = PitMetricStore(timescale_dsn)
    try:
        store.write(to_rows([("host", "cpu.percent", 1.0)], STAMP))

        store._connection().close()
        assert store.write(to_rows([("host", "cpu.percent", 2.0)], STAMP + timedelta(seconds=5)))

        with pytest.raises(psycopg.Error):
            store.write([("host", "cpu.percent", None, 9.0, None)])
        # The failed write drops the connection rather than reusing a session
        # whose state after the error is anyone's guess.
        assert store._conn is None

        assert store.write(to_rows([("host", "cpu.percent", 3.0)], STAMP + timedelta(seconds=10)))
    finally:
        store.close()

    with psycopg.connect(timescale_dsn) as conn:
        values = conn.execute(
            "SELECT value FROM v_pit_metrics WHERE metric = 'cpu.percent' ORDER BY time"
        ).fetchall()
    assert values == [(1.0,), (2.0,), (3.0,)]


# -------------------------------------------------------------------- health


def test_health_reports_the_collectors_own_pulse():
    state = HealthState()
    assert state.snapshot()["last_write_age_s"] is None

    state.observe_write(42)
    state.observe_probe_failure("nats", OSError("connection refused"))

    snapshot = state.snapshot()
    assert snapshot["polls"] == 1
    assert snapshot["rows_written"] == 42
    assert snapshot["probe_failures"] == {"nats": 1}
    assert snapshot["last_write_age_s"] is not None
    assert "nats" in snapshot["last_error"]
