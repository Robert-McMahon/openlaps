"""tools/gen_alert_rules.py: alarm limits are car configuration, rendered.

The committed provisioning file is output, not source (docs/plan/PHASE7.md
P7.1). What matters is that it is a fresh render of the profile's
alarms.yaml, that units are converted from the ones a person declares into
the ones the catalog says the channel carries, and that a limit the
generator cannot reconcile is refused rather than defaulted.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import gen_alert_rules  # noqa: E402

ROOT = Path(__file__).parents[1]
EXAMPLE_PROFILE = ROOT / "profiles" / "example-club-racer"
ALERTING = ROOT / "deploy/pit-config/grafana/provisioning/alerting"

# The shipped limits: channel, comparison, threshold in catalog units, `for`.
# The thresholds are the Phase 6 ones unchanged -- the first render was a
# refactor, and this table pinned it. The holds were then retuned for the
# under-10-second latency budget (docs/plan/PHASE7.md, locked decision 1)
# in a separate commit, which is the only reason they differ from Phase 6.
SHIPPED_LIMITS: dict[str, tuple[str | None, str, float, str]] = {
    "oil-pressure-low": ("car.oil_pressure", "lt", 200.0, "3s"),
    "coolant-temperature-high": ("car.coolant_temp", "gt", 383.15, "15s"),
    "oil-temperature-high": ("car.oil_temp", "gt", 398.15, "15s"),
    "battery-voltage-low": ("car.battery_v", "lt", 11.5, "10s"),
    "knock-high": ("car.knock_level1", "gt", 80.0, "5s"),
    "engine-protection-active": ("car.engine_protection_severity", "gt", 0.0, "2s"),
    "publish-lag-high": ("sys.agent.publish_lag_ms", "gt", 500.0, "30s"),
    "live-feed-stale": ("car.rpm", "gt", 5.0, "5s"),
    "notifier-heartbeat": (None, "gt", 0.0, "0s"),
    # P7.9: the strategy service's findings, by severity.
    "strategy-warning": (None, "gt", 0.0, "0s"),
    "strategy-critical": (None, "gt", 0.0, "0s"),
    # P7.10: the timing feed's findings, by severity.
    "field-warning": (None, "gt", 0.0, "0s"),
    "field-critical": (None, "gt", 0.0, "0s"),
}


def _rendered() -> dict[str, Any]:
    _, text = gen_alert_rules.render(EXAMPLE_PROFILE)
    return yaml.safe_load(text)


def _rules(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {rule["uid"]: rule for group in document["groups"] for rule in group["rules"]}


def _sql(rule: dict[str, Any]) -> str:
    return rule["data"][0]["model"]["rawSql"]


def _condition(rule: dict[str, Any]) -> dict[str, Any]:
    return rule["data"][2]["model"]["conditions"][0]


def _profile_with_alarms(tmp_path: Path, alarms: dict[str, Any]) -> Path:
    profile = tmp_path / "profile"
    shutil.copytree(EXAMPLE_PROFILE, profile)
    (profile / "alarms.yaml").write_text(
        yaml.safe_dump({"group": "test", "alarms": alarms}, allow_unicode=True),
        encoding="utf-8",
    )
    return profile


def test_the_committed_file_is_a_fresh_render_of_the_profile() -> None:
    name, text = gen_alert_rules.render(EXAMPLE_PROFILE)
    committed = (ALERTING / name).read_text(encoding="utf-8")
    assert committed == text, "run `uv run python tools/gen_alert_rules.py`"


def test_the_rendered_limits_are_the_phase_6_thresholds_with_retuned_holds() -> None:
    rules = _rules(_rendered())
    assert rules.keys() == SHIPPED_LIMITS.keys() | {"watch-critical", "watch-warning"}
    for uid, (channel, comparison, threshold, hold) in SHIPPED_LIMITS.items():
        rule = rules[uid]
        evaluator = _condition(rule)["evaluator"]
        if channel is not None:
            assert f"channel = '{channel}'" in _sql(rule)
        assert evaluator["type"] == comparison
        assert evaluator["params"] == [pytest.approx(threshold)]
        assert rule["for"] == hold
        assert rule["noDataState"] == "OK"


def test_a_limit_declared_in_celsius_renders_in_kelvin_with_both_values_visible() -> None:
    rule = _rules(_rendered())["coolant-temperature-high"]
    sql = _sql(rule)
    assert "110 °C" in sql and "383.15 K" in sql
    assert _condition(rule)["evaluator"]["params"] == [pytest.approx(383.15)]


def test_unit_spelling_aliases_reconcile_volts_with_v() -> None:
    # The catalog says "Volts" for car.battery_v and "V" for every other
    # voltage; an alarm may say V for both.
    assert gen_alert_rules.convert(11.5, "V", "Volts") == 11.5
    assert gen_alert_rules.convert(1.0, "bar", "kPa") == pytest.approx(100.0)
    assert gen_alert_rules.convert(212.0, "°F", "K") == pytest.approx(373.15)


def test_the_vehicle_id_comes_from_the_profile_not_the_generator(tmp_path: Path) -> None:
    profile = _profile_with_alarms(
        tmp_path,
        {
            "rpm-high": {
                "channel": "car.rpm",
                "units": "RPM",
                "above": 8000,
                "for": "5s",
                "resting": 0,
                "summary": "RPM high",
            }
        },
    )
    vehicle = profile / "vehicle.yaml"
    vehicle.write_text(
        vehicle.read_text(encoding="utf-8").replace("id: example-club-racer", "id: other-car"),
        encoding="utf-8",
    )
    name, text = gen_alert_rules.render(profile)
    assert name == "test.yaml"
    rule = _rules(yaml.safe_load(text))["rpm-high"]
    assert "vehicle_id = 'other-car'" in _sql(rule)
    assert "example-club-racer" not in text


@pytest.mark.parametrize(
    ("alarm", "message"),
    [
        (
            {
                "channel": "car.coolant_temp",
                "units": "kPa",
                "above": 110,
                "for": "1s",
                "resting": 0,
            },
            "cannot convert",
        ),
        (
            {"channel": "car.coolant_temp", "above": 110, "for": "1s", "resting": 0},
            "units",
        ),
        (
            {"channel": "car.no_such_thing", "units": "K", "above": 1, "for": "1s", "resting": 0},
            "not in the catalog",
        ),
        (
            {"channel": "car.rpm", "units": "RPM", "above": 8000, "for": "1s", "resting": 9000},
            "non-firing side",
        ),
        (
            {"channel": "car.rpm", "units": "RPM", "above": 8000, "below": 100, "for": "1s"},
            "exactly one",
        ),
        (
            {
                "channel": "car.rpm",
                "units": "RPM",
                "above": 8000,
                "clear_below": 8500,
                "for": "1s",
                "resting": 0,
            },
            "clear_below",
        ),
    ],
)
def test_limits_the_generator_cannot_reconcile_are_refused(
    tmp_path: Path, alarm: dict[str, Any], message: str
) -> None:
    alarm = {"summary": "x", **alarm}
    profile = _profile_with_alarms(tmp_path, {"bad": alarm})
    with pytest.raises(gen_alert_rules.AlarmError, match=message):
        gen_alert_rules.render(profile)


def test_a_clear_threshold_renders_as_a_recovery_evaluator_in_catalog_units(
    tmp_path: Path,
) -> None:
    profile = _profile_with_alarms(
        tmp_path,
        {
            "coolant-high": {
                "channel": "car.coolant_temp",
                "units": "°C",
                "above": 110,
                "clear_below": 105,
                "for": "5s",
                "resting": 0,
                "summary": "Coolant high",
            }
        },
    )
    _, text = gen_alert_rules.render(profile)
    condition = _condition(_rules(yaml.safe_load(text))["coolant-high"])
    assert condition["evaluator"] == {"type": "gt", "params": [pytest.approx(383.15)]}
    assert condition["unloadEvaluator"] == {"type": "lt", "params": [pytest.approx(378.15)]}


def test_gates_and_the_second_channel_condition_render_into_the_query(tmp_path: Path) -> None:
    profile = _profile_with_alarms(
        tmp_path,
        {
            "gated": {
                "channel": "car.oil_pressure",
                "units": "kPa",
                "below": 200,
                "for": "1s",
                "resting": 10000,
                "gate": "on_track",
                "when": {"channel": "car.rpm", "at_least": 2000},
                "summary": "g",
            },
            "engine": {
                "channel": "car.oil_pressure",
                "units": "kPa",
                "below": 200,
                "for": "1s",
                "resting": 10000,
                "gate": "engine_running",
                "summary": "e",
            },
            "ungated": {
                "channel": "sys.agent.publish_lag_ms",
                "units": "ms",
                "above": 500,
                "for": "1s",
                "resting": 0,
                "gate": "always",
                "summary": "u",
            },
        },
    )
    _, text = gen_alert_rules.render(profile)
    rules = _rules(yaml.safe_load(text))
    assert "pit_status" in _sql(rules["gated"]) and ">= 2000" in _sql(rules["gated"])
    # A parked car's last lap.event says "track" forever; only an open
    # session makes a car-channel rule judge at all (migration 008).
    assert "v_session_active" in _sql(rules["gated"])
    assert "v_session_active" not in _sql(rules["ungated"])
    assert rules["gated"]["labels"]["scope"] == "on track"
    assert "car.rpm" in _sql(rules["engine"]) and "pit_status" not in _sql(rules["engine"])
    assert "CASE" not in _sql(rules["ungated"])
    assert rules["ungated"]["labels"]["scope"] == "always"


def test_the_stale_watch_and_heartbeat_kinds_render(tmp_path: Path) -> None:
    profile = _profile_with_alarms(
        tmp_path,
        {
            "stale": {
                "kind": "stale",
                "channel": "car.rpm",
                "older_than_s": 5,
                "for": "10s",
                "summary": "s",
            },
            "findings": {
                "kind": "watch",
                "severity_at_least": "warning",
                "for": "0s",
                "summary": "w",
            },
            "notifier-heartbeat": {
                "kind": "heartbeat",
                "for": "0s",
                "severity": "none",
                "gate": "always",
                "summary": "h",
            },
        },
    )
    _, text = gen_alert_rules.render(profile)
    rules = _rules(yaml.safe_load(text))
    assert "extract(epoch" in _sql(rules["stale"]) and "pit_status" in _sql(rules["stale"])
    assert _condition(rules["stale"])["evaluator"]["params"] == [5.0]
    assert "v_watch_findings" in _sql(rules["findings"])
    assert "'critical', 'warning'" in _sql(rules["findings"])
    assert "monitor LIKE" not in _sql(rules["findings"])


_HEARTBEAT = {
    "kind": "heartbeat",
    "for": "0s",
    "severity": "none",
    "gate": "always",
    "summary": "h",
}


def test_a_watch_alarm_can_be_scoped_to_a_monitor_prefix_and_a_severity_band(
    tmp_path: Path,
) -> None:
    # The strategy service's findings (P7.9) alert through the same kind as
    # the anomaly monitors', narrowed to their monitor prefix, with a warning
    # rule and a critical rule that do not both fire on one critical finding.
    profile = _profile_with_alarms(
        tmp_path,
        {
            "strategy-warning": {
                "kind": "watch",
                "monitor_prefix": "strategy.",
                "severity_at_least": "warning",
                "severity_at_most": "warning",
                "severity": "warning",
                "gate": "always",
                "dashboard": "fuel",
                "for": "0s",
                "summary": "w",
                "panel": 3,
            },
            "strategy-critical": {
                "kind": "watch",
                "monitor_prefix": "strategy.",
                "severity_at_least": "critical",
                "gate": "always",
                "dashboard": "fuel",
                "for": "0s",
                "summary": "c",
            },
            "notifier-heartbeat": _HEARTBEAT,
        },
    )
    _, text = gen_alert_rules.render(profile)
    rules = _rules(yaml.safe_load(text))
    warning, critical = _sql(rules["strategy-warning"]), _sql(rules["strategy-critical"])
    assert "monitor LIKE 'strategy.%'" in warning and "monitor LIKE 'strategy.%'" in critical
    assert "severity IN ('warning')" in warning
    assert "severity IN ('critical')" in critical
    assert rules["strategy-warning"]["annotations"]["dashboardUID"] == "fuel"
    assert rules["strategy-warning"]["annotations"]["panelID"] == "3"
    assert rules["strategy-warning"]["labels"]["severity"] == "warning"
    with pytest.raises(ValueError, match="severity_at_most"):
        gen_alert_rules.render(
            _profile_with_alarms(
                tmp_path / "bad",
                {
                    "x": {
                        "kind": "watch",
                        "severity_at_least": "critical",
                        "severity_at_most": "warning",
                        "for": "0s",
                        "summary": "x",
                    },
                    "notifier-heartbeat": _HEARTBEAT,
                },
            )
        )
    assert _sql(rules["notifier-heartbeat"]).endswith('1.0 AS "value"')
    assert rules["notifier-heartbeat"]["labels"]["severity"] == "none"


def test_a_watch_alarm_can_exclude_the_prefixes_other_rules_own(tmp_path: Path) -> None:
    # The envelope rules (P7.5) count every open finding except the ones the
    # strategy-* and field-* rules already page on, so one finding pages once.
    profile = _profile_with_alarms(
        tmp_path,
        {
            "envelopes": {
                "kind": "watch",
                "exclude_monitor_prefixes": ["strategy.", "field."],
                "severity_at_least": "warning",
                "for": "0s",
                "severity": "warning",
                "gate": "always",
                "summary": "Envelope finding",
            }
        },
    )
    rules = _rules(yaml.safe_load(gen_alert_rules.render(profile)[1]))
    sql = _sql(rules["envelopes"])
    assert "monitor NOT LIKE 'strategy.%'" in sql and "monitor NOT LIKE 'field.%'" in sql
    assert "monitor LIKE" not in sql.replace("NOT LIKE", "")
    with pytest.raises(gen_alert_rules.AlarmError, match="watch alarms only"):
        gen_alert_rules.render(
            _profile_with_alarms(
                tmp_path / "threshold",
                {
                    "oil": {
                        "channel": "car.oil_pressure",
                        "units": "kPa",
                        "below": 200,
                        "resting": 400,
                        "for": "3s",
                        "summary": "x",
                        "exclude_monitor_prefixes": ["strategy."],
                    }
                },
            )
        )


def test_the_rendered_cadence_divides_grafana_s_fixed_base_interval() -> None:
    # Grafana 12.4.9 refuses to start on a group interval that does not
    # divide its 10 s base interval, and refuses to lower that base
    # (deploy/pit-compose.yaml). The rest of the latency budget lives in
    # the policy's zero group wait and in each alarm's `for`.
    document = _rendered()
    assert all(group["interval"] == "10s" for group in document["groups"])
    policy = document["policies"][0]
    assert policy["group_wait"] == "0s"
    compose = yaml.safe_load((ROOT / "deploy/pit-compose.yaml").read_text(encoding="utf-8"))
    environment = compose["services"]["grafana"]["environment"]
    assert "GF_UNIFIED_ALERTING_MIN_INTERVAL" not in environment


def test_every_continuous_shipped_alarm_has_hysteresis() -> None:
    # The discrete ones (a severity level, a staleness age) fire and clear on
    # their own threshold; everything measured on a continuous scale clears
    # past a second value so a reading that hovers at the limit fires once.
    discrete = {
        "engine-protection-active",
        "live-feed-stale",
        "notifier-heartbeat",
        "strategy-warning",
        "strategy-critical",
        "field-warning",
        "field-critical",
        "watch-critical",
        "watch-warning",
    }
    for uid, rule in _rules(_rendered()).items():
        condition = _condition(rule)
        if uid in discrete:
            assert "unloadEvaluator" not in condition, uid
        else:
            assert "unloadEvaluator" in condition, uid


def test_every_rendered_rule_carries_a_severity_label_for_the_notifier() -> None:
    for uid, rule in _rules(_rendered()).items():
        assert rule["labels"]["severity"] in {"critical", "warning", "none"}, uid


def test_check_mode_reports_a_stale_file(tmp_path: Path, capsys) -> None:
    out = tmp_path / "alerting"
    out.mkdir()
    assert gen_alert_rules.main(["--out-dir", str(out), "--check"]) == 1
    assert gen_alert_rules.main(["--out-dir", str(out)]) == 0
    assert gen_alert_rules.main(["--out-dir", str(out), "--check"]) == 0
    assert re.search(r"is current", capsys.readouterr().out)
