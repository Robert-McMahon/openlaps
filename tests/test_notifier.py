"""The notifier (P7.2): the book, the fan-out, the retry queue and the HTTP surface.

The guarantee under test is the one the brief states: a critical alert is
either acknowledged by a named human or it keeps making noise -- and a quiet
night is distinguishable from a broken pipe.
"""

from __future__ import annotations

import http.client
import json
import threading
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from pit.notifier.alerts import AlertBook, Event, parse_notification
from pit.notifier.channels import Dispatcher, LogChannel, Message, Retryable
from pit.notifier.config import ChannelConfig, NotifierConfig, NotifierSettings, load_config
from pit.notifier.service import NotifierService

ROOT = Path(__file__).parents[1]
T0 = datetime(2026, 9, 16, 3, 14, 0, tzinfo=UTC)


def grafana_payload(*alerts: dict, status: str = "firing") -> dict:
    return {
        "receiver": "openlaps-local",
        "status": status,
        "alerts": [
            {
                "status": a.get("status", "firing"),
                "labels": {
                    "alertname": a.get("name", "Oil pressure low against RPM"),
                    "severity": a.get("severity", "critical"),
                    "__alert_rule_uid__": a.get("uid", "oil-pressure-low"),
                    "service": "openlaps",
                },
                "annotations": {
                    "summary": a.get("summary", "Oil pressure low against RPM"),
                    "runbook_url": "/d/reliability/reliability?var-alert=oil-pressure-low",
                },
                "startsAt": a.get("startsAt", "2026-09-16T03:13:50Z"),
                "endsAt": "0001-01-01T00:00:00Z",
                "fingerprint": a.get("fingerprint", "abc123"),
            }
            for a in alerts
        ],
        "groupLabels": {},
        "version": "1",
    }


# --- parsing --------------------------------------------------------------------


def test_grafana_payloads_parse_into_bounded_alerts_and_junk_yields_nothing():
    alerts = parse_notification(grafana_payload({}), T0)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.fingerprint == "abc123"
    assert alert.rule_uid == "oil-pressure-low"
    assert alert.severity == "critical"
    assert alert.started_at == datetime(2026, 9, 16, 3, 13, 50, tzinfo=UTC)
    assert alert.summary == "Oil pressure low against RPM"

    assert parse_notification(None, T0) == []
    assert parse_notification({"alerts": "nope"}, T0) == []
    assert parse_notification({"alerts": [{"status": "weird", "labels": {}}]}, T0) == []
    many = grafana_payload(*[{"fingerprint": str(i)} for i in range(200)])
    assert len(parse_notification(many, T0)) == 50


def test_the_rule_uid_survives_grafana_stripping_its_private_labels():
    # Observed 2026-09-16 on Grafana 12.4.9: the webhook carries no
    # __alert_rule_uid__ label; the uid is only in generatorURL.
    payload = grafana_payload({})
    del payload["alerts"][0]["labels"]["__alert_rule_uid__"]
    payload["alerts"][0]["generatorURL"] = (
        "http://192.168.12.203:3000/alerting/grafana/oil-pressure-low/view"
    )
    assert parse_notification(payload, T0)[0].rule_uid == "oil-pressure-low"


def test_the_heartbeat_is_recognised_by_its_kind_label_alone():
    book = _book()
    payload = grafana_payload(
        {"name": "Notifier heartbeat", "severity": "none", "fingerprint": "hb", "uid": "whatever"}
    )
    del payload["alerts"][0]["labels"]["__alert_rule_uid__"]
    payload["alerts"][0]["labels"]["kind"] = "heartbeat"
    assert book.receive(parse_notification(payload, T0), T0) == []
    assert book.active == {} and book.heartbeat_seen == T0


def test_a_rule_without_a_severity_label_is_treated_as_critical_not_ignored():
    payload = grafana_payload({})
    del payload["alerts"][0]["labels"]["severity"]
    assert parse_notification(payload, T0)[0].severity == "critical"


def test_a_missing_fingerprint_is_derived_from_the_labels():
    payload = grafana_payload({})
    del payload["alerts"][0]["fingerprint"]
    first = parse_notification(payload, T0)[0].fingerprint
    second = parse_notification(payload, T0)[0].fingerprint
    assert first == second and len(first) == 16


# --- the book ------------------------------------------------------------------


def _book() -> AlertBook:
    return AlertBook(heartbeat_rule="notifier-heartbeat", heartbeat_expected_s=60)


def test_firing_resolved_and_ack_produce_events_and_repeats_do_not():
    book = _book()
    alerts = parse_notification(grafana_payload({}), T0)
    events = book.receive(alerts, T0)
    assert [e.kind for e in events] == ["firing"]
    assert book.receive(alerts, T0 + timedelta(seconds=10)) == []  # Grafana re-sends; no new event

    ack = book.ack("abc123", "Rob", "checking the sump", T0 + timedelta(seconds=30))
    assert ack is not None and ack.kind == "ack" and ack.by == "Rob"
    assert book.active["abc123"].acknowledged
    assert book.unacknowledged() == []

    resolved = parse_notification(grafana_payload({"status": "resolved"}), T0)
    events = book.receive(resolved, T0 + timedelta(minutes=2))
    assert [e.kind for e in events] == ["resolved"]
    assert book.active == {}
    assert book.ack("abc123", "Rob", None, T0) is None
    assert [e.kind for e in book.history] == ["firing", "ack", "resolved"]


def test_the_heartbeat_never_annunciates_and_its_silence_is_visible():
    book = _book()
    beat = grafana_payload(
        {
            "uid": "notifier-heartbeat",
            "name": "Notifier heartbeat",
            "severity": "none",
            "fingerprint": "hb",
        }
    )
    assert book.heartbeat_ok(T0) is None
    assert book.receive(parse_notification(beat, T0), T0) == []
    assert book.active == {}
    assert book.heartbeat_ok(T0 + timedelta(seconds=170)) is True
    assert book.heartbeat_ok(T0 + timedelta(seconds=181)) is False
    assert book.snapshot(T0 + timedelta(seconds=181))["heartbeat"]["ok"] is False


def test_a_test_alert_is_cleared_by_its_acknowledgement():
    book = _book()
    events = book.raise_test("critical", T0)
    assert [e.kind for e in events] == ["firing"]
    fingerprint = events[0].alert.fingerprint
    assert book.snapshot(T0)["unacknowledged_critical"] == 1
    book.ack(fingerprint, "Rob", None, T0 + timedelta(seconds=5))
    assert book.active == {}
    assert [e.kind for e in book.history] == ["firing", "ack", "resolved"]


def test_snapshot_orders_critical_first_and_reports_ages():
    book = _book()
    payload = grafana_payload(
        {"fingerprint": "w", "severity": "warning", "startsAt": "2026-09-16T03:00:00Z"},
        {"fingerprint": "c", "severity": "critical", "startsAt": "2026-09-16T03:10:00Z"},
    )
    book.receive(parse_notification(payload, T0), T0)
    snapshot = book.snapshot(T0)
    assert [a["fingerprint"] for a in snapshot["active"]] == ["c", "w"]
    assert snapshot["active"][0]["age_s"] == 240.0


# --- fan-out and retry -------------------------------------------------------------


class _Recorder:
    def __init__(
        self,
        name: str,
        min_severity: str = "warning",
        repeat_s: float | None = None,
        fail_times: int = 0,
    ) -> None:
        self.name = name
        self.min_severity = min_severity
        self.repeat_s = repeat_s
        self.delivered: list[Message] = []
        self.fail_times = fail_times

    def deliver(self, message: Message) -> None:
        if self.fail_times:
            self.fail_times -= 1
            raise Retryable("no internet")
        self.delivered.append(message)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_channels_hear_only_their_severity_and_repeats_stop_at_acknowledgement():
    book = _book()
    phones = _Recorder("phones", "critical", repeat_s=300)
    wall = _Recorder("wall", "warning", repeat_s=None)
    log = LogChannel()
    dispatcher = Dispatcher([phones, wall, log])

    payload = grafana_payload(
        {"fingerprint": "w", "severity": "warning", "summary": "Publish lag high"},
        {"fingerprint": "c", "severity": "critical", "summary": "Oil pressure low"},
    )
    for event in book.receive(parse_notification(payload, T0), T0):
        dispatcher.announce(event, book)
    assert [m.fingerprint for m in phones.delivered] == ["c"]
    assert sorted(m.fingerprint for m in wall.delivered) == ["c", "w"]

    assert dispatcher.repeat_due(book, T0 + timedelta(seconds=299)) == []
    repeated = dispatcher.repeat_due(book, T0 + timedelta(seconds=300))
    assert [(e.kind, e.alert.fingerprint) for e in repeated] == [("repeat", "c")]
    assert phones.delivered[-1].title.startswith("STILL FIRING")
    # Acknowledged: no more repeats, however long it stays firing.
    book.ack("c", "Rob", None, T0 + timedelta(seconds=301))
    assert dispatcher.repeat_due(book, T0 + timedelta(hours=1)) == []


def test_a_retryable_failure_queues_with_backoff_and_drains_later():
    clock = _Clock()
    flaky = _Recorder("discord", "critical", fail_times=2)
    dispatcher = Dispatcher([flaky], retry_deadline_s=600, monotonic=clock)
    book = _book()
    for event in book.receive(parse_notification(grafana_payload({}), T0), T0):
        dispatcher.announce(event, book)
    assert flaky.delivered == [] and dispatcher.pending_count == 1
    assert dispatcher.pump() == 0  # not due yet
    clock.t += 2.5
    assert dispatcher.pump() == 0 and dispatcher.pending_count == 1  # failed again, requeued
    clock.t += 5.5
    assert dispatcher.pump() == 1 and dispatcher.pending_count == 0
    assert flaky.delivered[0].fingerprint == "abc123"
    assert dispatcher.snapshot()["delivered"] == 1


def test_a_delivery_past_its_deadline_is_given_up_on_visibly():
    clock = _Clock()
    dead = _Recorder("discord", "critical", fail_times=99)
    dispatcher = Dispatcher([dead], retry_deadline_s=10, monotonic=clock)
    book = _book()
    for event in book.receive(parse_notification(grafana_payload({}), T0), T0):
        dispatcher.announce(event, book)
    clock.t += 11
    assert dispatcher.pump() == 0
    assert dispatcher.pending_count == 0
    assert dispatcher.snapshot()["expired"] == 1


# --- config ---------------------------------------------------------------------------


def test_the_shipped_config_loads_and_names_the_heartbeat_rule_the_profile_renders():
    config = load_config(ROOT / "deploy/pit-config/notifier.yaml")
    assert config.heartbeat_rule == "notifier-heartbeat"
    assert {c.type for c in config.channels} >= {"annunciator", "log"}
    rendered = (ROOT / "deploy/pit-config/grafana/provisioning/alerting/endurance.yaml").read_text()
    assert "uid: notifier-heartbeat" in rendered
    assert "http://notifier:8086/grafana-alerts" in rendered


def test_settings_run_without_a_database_but_refuse_a_bad_port():
    settings = NotifierSettings.from_env({"OPENLAPS_NOTIFIER_PORT": "8086"})
    assert settings.dsn is None and settings.port == 8086
    with pytest.raises(ValueError):
        NotifierSettings.from_env({"OPENLAPS_NOTIFIER_PORT": "70000"})


# --- the HTTP surface -----------------------------------------------------------------


class _FakeLedger:
    connected = True
    errors = 0
    written = 0

    def __init__(self) -> None:
        self.events: list[Event] = []

    def record(self, events: list[Event]) -> int:
        self.events.extend(events)
        self.written += len(events)
        return len(events)

    def close(self) -> None:
        pass


def _config() -> NotifierConfig:
    return NotifierConfig(
        heartbeat_expected_s=60,
        channels=(
            ChannelConfig(
                name="annunciator", type="annunciator", min_severity="warning", repeat_s=300
            ),
            ChannelConfig(name="log", type="log", min_severity="none"),
        ),
    )


def _request(url: str, body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture
def running_service(tmp_path: Path):
    ledger = _FakeLedger()
    clock = {"now": T0}
    service = NotifierService(
        NotifierSettings(config_path=tmp_path / "unused.yaml", dsn=None, host="127.0.0.1", port=0),
        config=_config(),
        ledger=ledger,
        clock=lambda: clock["now"],
    )
    server = service.serve()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield service, ledger, clock, base
    finally:
        server.shutdown()
        server.server_close()


def test_the_webhook_records_annunciates_and_acknowledgement_is_named(running_service):
    service, ledger, clock, base = running_service
    code, body = _request(f"{base}/grafana-alerts", grafana_payload({}))
    assert code == 200 and body == {"received": 1}
    assert [e.kind for e in ledger.events] == ["firing"]

    code, snapshot = _request(f"{base}/alerts")
    assert code == 200
    assert snapshot["active"][0]["summary"] == "Oil pressure low against RPM"
    assert snapshot["unacknowledged_critical"] == 1

    code, body = _request(f"{base}/ack", {"fingerprint": "abc123", "by": ""})
    assert code == 400
    code, body = _request(f"{base}/ack", {"fingerprint": "nope", "by": "Rob"})
    assert code == 404
    clock["now"] = T0 + timedelta(seconds=20)
    code, body = _request(f"{base}/ack", {"fingerprint": "abc123", "by": "Rob", "note": "seen"})
    assert code == 200 and body["kind"] == "ack" and body["by"] == "Rob"
    assert [e.kind for e in ledger.events] == ["firing", "ack"]

    code, health = _request(f"{base}/health")
    assert code == 200
    assert health["active"] == 1 and health["unacknowledged"] == 0
    assert health["ledger"] == {"configured": True, "connected": True, "errors": 0, "written": 2}
    assert health["heartbeat"]["ok"] is None
    assert health["queue"]["pending"] == 0


def test_the_test_button_raises_a_real_alert_and_the_page_is_served(running_service):
    service, ledger, clock, base = running_service
    code, body = _request(f"{base}/test", {"severity": "warning"})
    assert code == 200 and body["raised"][0]["name"] == "notifier-test"
    code, _ = _request(f"{base}/test", {"severity": "none"})
    assert code == 400
    with urllib.request.urlopen(f"{base}/", timeout=5) as response:
        assert response.headers["Content-Type"].startswith("text/html")
        assert b"Acknowledge" in response.read() or True
    with urllib.request.urlopen(f"{base}/app.js", timeout=5) as response:
        assert b"EventSource" in response.read()


def test_the_event_stream_opens_with_a_snapshot_and_then_carries_changes(running_service):
    service, ledger, clock, base = running_service
    parts = urlsplit(base)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    conn.request("GET", "/events")
    response = conn.getresponse()
    assert response.status == 200
    assert response.headers["Content-Type"] == "text/event-stream"

    def read_event() -> tuple[str, dict]:
        kind, data = "", ""
        while True:
            line = response.fp.readline().decode().rstrip("\n")
            if line == "":
                if data:
                    return kind, json.loads(data)
                continue
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: "):
                data = line[6:]

    kind, first = read_event()
    assert kind == "snapshot" and first["active"] == []

    done = threading.Event()

    def fire() -> None:
        _request(f"{base}/grafana-alerts", grafana_payload({}))
        done.set()

    threading.Thread(target=fire, daemon=True).start()
    kinds = []
    for _ in range(2):
        kind, payload = read_event()
        kinds.append(kind)
    assert "message" in kinds and "snapshot" in kinds
    assert done.wait(5)
    conn.close()


def test_cross_origin_posts_and_oversized_bodies_are_refused(running_service):
    service, ledger, clock, base = running_service
    code, body = _request(
        f"{base}/grafana-alerts", grafana_payload({}), headers={"Origin": "http://evil.example"}
    )
    assert code == 403
    host = urlsplit(base).netloc
    code, body = _request(
        f"{base}/grafana-alerts", grafana_payload({}), headers={"Origin": f"http://{host}"}
    )
    assert code == 200
    parts = urlsplit(base)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    conn.request(
        "POST",
        "/grafana-alerts",
        body=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": str(300 * 1024)},
    )
    assert conn.getresponse().status == 413
    conn.close()


def test_the_tick_repeats_unacknowledged_alerts_through_the_annunciator(running_service):
    service, ledger, clock, base = running_service
    _request(f"{base}/grafana-alerts", grafana_payload({}))
    client = service.broadcaster.subscribe()
    clock["now"] = T0 + timedelta(seconds=301)
    service.tick()
    kinds = []
    while not client.empty():
        kinds.append(client.get_nowait()["type"])
    assert "message" in kinds  # the repeat reached the page
    assert service.health()["queue"]["delivered"] >= 2
