"""Phase 6 contracts for provisioned endurance alert rules."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
ALERTING = ROOT / "deploy/pit-config/grafana/provisioning/alerting"
SECRET = re.compile(r"password|passwd|token|api[_-]?key|authorization|bearer\s", re.IGNORECASE)
REQUIRED_RULES = {
    "oil-pressure-low",
    "coolant-temperature-high",
    "oil-temperature-high",
    "battery-voltage-low",
    "knock-high",
    "engine-protection-active",
    "publish-lag-high",
    "live-feed-stale",
    "notifier-heartbeat",
    "strategy-warning",
    "strategy-critical",
    "field-warning",
    "field-critical",
    "watch-critical",
    "watch-warning",
}


def _documents() -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for path in sorted(ALERTING.glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(document, dict):
            documents.append(document)
    return documents


def _rules() -> list[dict[str, Any]]:
    return [
        rule
        for document in _documents()
        for group in document.get("groups", [])
        for rule in group.get("rules", [])
    ]


def test_alert_rules_are_provisioned_with_stable_uids_and_local_contact_point() -> None:
    documents = _documents()
    rules = _rules()

    assert documents
    assert {rule["uid"] for rule in rules} == REQUIRED_RULES
    assert any(document.get("contactPoints") for document in documents)
    assert all(rule.get("for") for rule in rules)


def test_alert_files_contain_no_secrets_and_every_rule_links_to_its_dashboard() -> None:
    serialized = "\n".join(path.read_text(encoding="utf-8") for path in ALERTING.glob("*.yaml"))
    assert not SECRET.search(serialized)
    for rule in _rules():
        annotations = rule.get("annotations", {})
        # Car and pipeline rules explain themselves on the reliability
        # dashboard; the strategy rules (P7.9) on the fuel dashboard.
        expected = "fuel" if rule["uid"].startswith("strategy-") else "reliability"
        assert annotations.get("dashboardUID") == expected, rule["uid"]
        assert f"/d/{expected}/" in annotations.get("runbook_url", ""), rule["uid"]


def test_car_staleness_alerts_are_gated_off_in_the_pits() -> None:
    rules = {rule["uid"]: rule for rule in _rules()}
    for uid in (
        "oil-pressure-low",
        "coolant-temperature-high",
        "oil-temperature-high",
        "battery-voltage-low",
        "knock-high",
        "engine-protection-active",
        "live-feed-stale",
    ):
        serialized = yaml.safe_dump(rules[uid]).lower()
        assert "pit_status" in serialized or "on track" in serialized


def test_each_rule_has_a_secret_free_demonstrated_firing_procedure() -> None:
    runbook = (ROOT / "docs/BENCH_RUNBOOK.md").read_text(encoding="utf-8").lower()
    for uid in REQUIRED_RULES:
        assert uid in runbook


def test_every_rule_annotates_a_panel_that_exists_on_its_dashboard() -> None:
    # Grafana draws alert state changes on the panel a rule names; a stale
    # panel id annotates nothing and says nothing (P7.4).
    dashboards = ROOT / "deploy/pit-config/grafana/dashboards"
    for rule in _rules():
        annotations = rule.get("annotations", {})
        panel_id = annotations.get("panelID")
        if panel_id is None:
            continue
        dashboard = yaml.safe_load(
            (dashboards / f"{annotations['dashboardUID']}.json").read_text(encoding="utf-8")
        )
        panels = list(dashboard["panels"])
        for panel in list(panels):
            panels.extend(panel.get("panels", []))
        matching = [p for p in panels if str(p.get("id")) == str(panel_id)]
        uid = annotations["dashboardUID"]
        assert matching, f"{rule['uid']} names panel {panel_id}, absent from {uid}"
        assert matching[0]["type"] in {"timeseries", "xychart", "state-timeline", "trend"}, (
            f"{rule['uid']} annotates a {matching[0]['type']} panel, which draws no annotations"
        )


def test_the_four_watched_dashboards_carry_a_firing_alert_strip_at_the_top() -> None:
    dashboards = ROOT / "deploy/pit-config/grafana/dashboards"
    for name in ("pitwall", "car", "reliability", "fuel"):
        dashboard = yaml.safe_load((dashboards / f"{name}.json").read_text(encoding="utf-8"))
        strips = [p for p in dashboard["panels"] if p.get("type") == "alertlist"]
        assert len(strips) == 1, f"{name} needs exactly one alert strip"
        strip = strips[0]
        assert strip["gridPos"]["y"] == 0 and strip["gridPos"]["w"] == 24
        assert strip["options"]["stateFilter"]["firing"] is True
        assert strip["options"]["stateFilter"]["normal"] is False
