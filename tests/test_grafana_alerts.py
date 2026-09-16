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


def test_alert_files_contain_no_secrets_and_every_rule_links_to_reliability() -> None:
    serialized = "\n".join(path.read_text(encoding="utf-8") for path in ALERTING.glob("*.yaml"))
    assert not SECRET.search(serialized)
    for rule in _rules():
        annotations = rule.get("annotations", {})
        assert (
            "reliability"
            in (annotations.get("dashboardUID", "") + annotations.get("runbook_url", "")).lower()
        )


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
