#!/usr/bin/env python3
"""Render Grafana alert rules from a profile's `alarms.yaml`.

Alarm limits are car configuration and live in the profile beside the
catalog that gives each channel its units (`docs/plan/PHASE7.md`, P7.1;
ADR 0011). This tool turns them into the provisioning YAML Grafana loads
from `deploy/pit-config/grafana/provisioning/alerting/`, and the test suite
asserts the committed file is a fresh render, the same way dashboards are
held to the repository rather than the browser.

The conversion of units is the point. A limit is declared in the unit a
person thinks in (`110 °C`) and rendered in the unit the catalog says the
channel carries (`383.15 K`), with both values written into the SQL as a
comment so a reviewer sees them side by side. A declared unit the
generator cannot reconcile with the catalog's is an error.

Usage:
    uv run python tools/gen_alert_rules.py                 # example profile
    uv run python tools/gen_alert_rules.py --profile DIR   # another profile
    uv run python tools/gen_alert_rules.py --check         # exit 1 if stale
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.config import ConfigError, load_profile  # noqa: E402

DEFAULT_PROFILE = ROOT / "profiles" / "example-club-racer"
ALERTING_DIR = ROOT / "deploy" / "pit-config" / "grafana" / "provisioning" / "alerting"
TIMESCALE_UID = "timescale"
EXPRESSION_UID = "-100"
# Evaluation cadence. 2 s is below Grafana's default floor of 10 s; the floor
# is lowered in deploy/pit-compose.yaml (GF_UNIFIED_ALERTING_MIN_INTERVAL and
# _SCHEDULER_TICK_INTERVAL) and Grafana silently rounds a group interval up
# to the floor if the two disagree -- which is why the bench drill measures
# the result instead of trusting this constant.
GROUP_INTERVAL = "2s"

# Units the generator knows how to convert between, as families. Each entry
# maps a unit to (scale, offset) into the family's base unit. Anything
# outside these families converts only to itself.
_TEMPERATURE: dict[str, tuple[float, float]] = {
    "K": (1.0, 0.0),
    "°C": (1.0, 273.15),
    "°F": (5.0 / 9.0, 255.3722222222222),
}
_PRESSURE: dict[str, tuple[float, float]] = {
    "kPa": (1.0, 0.0),
    "bar": (100.0, 0.0),
    "psi": (6.894757293168, 0.0),
}
_FAMILIES = (_TEMPERATURE, _PRESSURE)

# The catalog spells some units more than one way ("V" and "Volts", "degC"
# and "C"). Both sides are normalised through this table before anything is
# compared or converted, so an alarm may use the obvious spelling.
_ALIASES: dict[str, str] = {
    "Volts": "V",
    "volts": "V",
    "degC": "°C",
    "C": "°C",
    "degF": "°F",
    "F": "°F",
    "deg": "°",
    "lambda": "λ",
}

# Namespaces with no catalog entry: the agent and the timing engine publish
# them without a `from:`. A limit on one must declare its own units and is
# never converted.
_UNCATALOGUED_PREFIXES = ("sys.", "lap.", "timing.")


class AlarmError(ValueError):
    """An alarms.yaml entry the generator refuses to render."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class WhenClause(_Strict):
    channel: str
    at_least: float | None = None
    at_most: float | None = None

    @model_validator(mode="after")
    def exactly_one_bound(self) -> WhenClause:
        if (self.at_least is None) == (self.at_most is None):
            raise ValueError("`when` needs exactly one of at_least / at_most")
        return self


class Alarm(_Strict):
    kind: Literal["threshold", "stale", "watch", "heartbeat"] = "threshold"
    channel: str | None = None
    units: str | None = None
    above: float | None = None
    below: float | None = None
    clear_below: float | None = None
    clear_above: float | None = None
    older_than_s: float | None = None
    severity_at_least: Literal["critical", "warning"] | None = None
    for_: str = Field(alias="for")
    severity: Literal["critical", "warning", "none"] = "critical"
    gate: Literal["on_track", "always", "engine_running"] = "on_track"
    resting: float | None = None
    when: WhenClause | None = None
    summary: str
    panel: int | None = None

    @model_validator(mode="after")
    def shape_matches_kind(self) -> Alarm:
        if self.kind == "threshold":
            if (self.above is None) == (self.below is None):
                raise ValueError("a threshold alarm needs exactly one of above / below")
            if self.above is not None and self.clear_above is not None:
                raise ValueError("an `above` alarm clears with clear_below, not clear_above")
            if self.below is not None and self.clear_below is not None:
                raise ValueError("a `below` alarm clears with clear_above, not clear_below")
            if self.channel is None or self.units is None or self.resting is None:
                raise ValueError("a threshold alarm needs channel, units and resting")
            if self.above is not None and self.resting > self.above:
                raise ValueError("resting must sit on the non-firing side of `above`")
            if self.below is not None and self.resting < self.below:
                raise ValueError("resting must sit on the non-firing side of `below`")
            if self.clear_below is not None and self.clear_below > self.above:
                raise ValueError("clear_below must not exceed `above`")
            if self.clear_above is not None and self.clear_above < self.below:
                raise ValueError("clear_above must not be under `below`")
        elif self.kind == "stale":
            if self.channel is None or self.older_than_s is None:
                raise ValueError("a stale alarm needs channel and older_than_s")
            if self.units not in (None, "s"):
                raise ValueError("a stale alarm's units are seconds")
        elif self.kind == "watch":
            if self.severity_at_least is None:
                raise ValueError("a watch alarm needs severity_at_least")
        return self


class AlarmsFile(_Strict):
    group: str
    alarms: dict[str, Alarm] = Field(min_length=1)


def canonical(units: str) -> str:
    """One spelling per unit, so "Volts" and "V" compare equal."""
    return _ALIASES.get(units, units)


def convert(value: float, declared: str, catalog_units: str) -> float:
    """Convert `value` from the alarm's declared unit into the catalog's."""
    declared, catalog_units = canonical(declared), canonical(catalog_units)
    if declared == catalog_units:
        return value
    for family in _FAMILIES:
        if declared in family and catalog_units in family:
            scale_in, offset_in = family[declared]
            scale_out, offset_out = family[catalog_units]
            base = value * scale_in + offset_in
            return (base - offset_out) / scale_out
    raise AlarmError(f"cannot convert {declared!r} to the catalog's {catalog_units!r}")


def _catalog_units(channel: str, channels: dict[str, Any]) -> str | None:
    """The catalog's unit for `channel`, or None for an uncatalogued namespace."""
    if channel in channels:
        return channels[channel].units or "none"
    if channel.startswith(_UNCATALOGUED_PREFIXES):
        return None
    raise AlarmError(f"channel {channel!r} is not in the catalog")


def _resolve(uid: str, alarm: Alarm, channels: dict[str, Any]) -> tuple[float, float, str]:
    """Threshold and resting value in catalog units, plus a note for the SQL."""
    assert alarm.channel is not None and alarm.units is not None and alarm.resting is not None
    limit = alarm.above if alarm.above is not None else alarm.below
    assert limit is not None
    target = _catalog_units(alarm.channel, channels)
    if target is None:
        return limit, alarm.resting, f"{uid}: {_bound(alarm)} {_fmt(limit)} {alarm.units}"
    try:
        rendered = convert(limit, alarm.units, target)
        resting = convert(alarm.resting, alarm.units, target)
    except AlarmError as exc:
        raise AlarmError(f"{uid}: {exc}") from exc
    if canonical(alarm.units) == canonical(target):
        note = f"{uid}: {_bound(alarm)} {_fmt(limit)} {target}"
    else:
        note = (
            f"{uid}: {_bound(alarm)} {_fmt(limit)} {alarm.units} = {_fmt(rendered)} {target}"
            " (catalog units)"
        )
    return rendered, resting, note


def _bound(alarm: Alarm) -> str:
    return "above" if alarm.above is not None else "below"


def _fmt(value: float) -> str:
    return f"{value:g}" if float(value).is_integer() else f"{value:.6g}"


def _latest(vehicle: str, channel: str) -> str:
    return (
        "(SELECT value FROM v_samples_named WHERE vehicle_id = '"
        f"{vehicle}' AND channel = '{channel}' AND value IS NOT NULL "
        "ORDER BY time DESC LIMIT 1)"
    )


def _gate_sql(vehicle: str, gate: str) -> str | None:
    if gate == "always":
        return None
    if gate == "on_track":
        return (
            "COALESCE((SELECT value_text::jsonb ->> 'pit_status' FROM v_samples_named "
            f"WHERE vehicle_id = '{vehicle}' AND channel = 'lap.event' AND value_text "
            "IS NOT NULL ORDER BY time DESC LIMIT 1), 'track') = 'track'"
        )
    if gate == "engine_running":
        return f"COALESCE({_latest(vehicle, 'car.rpm')}, 0) >= 500"
    raise AlarmError(f"unknown gate {gate!r}")


def _threshold_sql(vehicle: str, alarm: Alarm, limit_note: str, resting: float) -> str:
    assert alarm.channel is not None
    conditions = []
    gate = _gate_sql(vehicle, alarm.gate)
    if gate is not None:
        conditions.append(gate)
    if alarm.when is not None:
        latest = f"COALESCE({_latest(vehicle, alarm.when.channel)}, 0)"
        if alarm.when.at_least is not None:
            conditions.append(f"{latest} >= {alarm.when.at_least:g}")
        else:
            conditions.append(f"{latest} <= {alarm.when.at_most:g}")
    value = f"COALESCE({_latest(vehicle, alarm.channel)}, {resting})"
    if conditions:
        value = f"CASE WHEN {' AND '.join(conditions)} THEN {value} ELSE {resting} END"
    return f'/* {limit_note} */ SELECT now() AS "time", {value} AS "value"'


def _stale_sql(vehicle: str, alarm: Alarm) -> str:
    assert alarm.channel is not None
    age = (
        "COALESCE(extract(epoch FROM (now() - (SELECT max(time) FROM v_samples_named "
        f"WHERE vehicle_id = '{vehicle}' AND channel = '{alarm.channel}'))), 1000000.0)"
    )
    gate = _gate_sql(vehicle, alarm.gate)
    value = f"CASE WHEN {gate} THEN {age} ELSE 0.0 END" if gate else age
    return f'SELECT now() AS "time", {value} AS "value"'


def _watch_sql(vehicle: str, alarm: Alarm) -> str:
    severities = {"critical": "('critical')", "warning": "('critical', 'warning')"}
    assert alarm.severity_at_least is not None
    return (
        'SELECT now() AS "time", (SELECT count(*) FROM v_watch_findings WHERE vehicle_id = '
        f"'{vehicle}' AND closed_at IS NULL AND severity IN "
        f'{severities[alarm.severity_at_least]})::double precision AS "value"'
    )


def _rule(uid: str, alarm: Alarm, vehicle: str, channels: dict[str, Any]) -> dict[str, Any]:
    if alarm.kind == "threshold":
        limit, resting, note = _resolve(uid, alarm, channels)
        sql = _threshold_sql(vehicle, alarm, note, resting)
        evaluator = {"type": "gt" if alarm.above is not None else "lt", "params": [limit]}
        clear = alarm.clear_below if alarm.above is not None else alarm.clear_above
        unload = None
        if clear is not None:
            target = _catalog_units(alarm.channel or "", channels)
            clear_value = clear if target is None else convert(clear, alarm.units or "", target)
            unload = {"type": "lt" if alarm.above is not None else "gt", "params": [clear_value]}
    elif alarm.kind == "stale":
        sql = _stale_sql(vehicle, alarm)
        evaluator = {"type": "gt", "params": [float(alarm.older_than_s or 0)]}
        unload = None
    elif alarm.kind == "watch":
        sql = _watch_sql(vehicle, alarm)
        evaluator = {"type": "gt", "params": [0.0]}
        unload = None
    else:  # heartbeat
        sql = 'SELECT now() AS "time", 1.0 AS "value"'
        evaluator = {"type": "gt", "params": [0.0]}
        unload = None

    datasource = {"type": "grafana-postgresql-datasource", "uid": TIMESCALE_UID}
    expression = {"type": "__expr__", "uid": EXPRESSION_UID}
    condition: dict[str, Any] = {
        "evaluator": evaluator,
        "operator": {"type": "and"},
        "query": {"params": ["C"]},
        "reducer": {"params": [], "type": "last"},
        "type": "query",
    }
    if unload is not None:
        condition["unloadEvaluator"] = unload
    annotations = {
        "dashboardUID": "reliability",
        "runbook_url": f"/d/reliability/reliability?var-alert={uid}",
        "summary": alarm.summary,
    }
    if alarm.panel is not None:
        annotations["panelID"] = str(alarm.panel)
    return {
        "uid": uid,
        "title": alarm.summary,
        "condition": "C",
        "for": alarm.for_,
        "noDataState": "OK",
        "execErrState": "Error",
        "annotations": annotations,
        "labels": {
            "service": "openlaps",
            "severity": alarm.severity,
            "scope": "on track" if alarm.gate == "on_track" else "always",
        },
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 600, "to": 0},
                "datasourceUid": TIMESCALE_UID,
                "model": {
                    "datasource": datasource,
                    "editorMode": "code",
                    "format": "time_series",
                    "instant": True,
                    "rawQuery": True,
                    "rawSql": sql,
                    "refId": "A",
                },
            },
            {
                "refId": "B",
                "relativeTimeRange": {"from": 0, "to": 0},
                "datasourceUid": EXPRESSION_UID,
                "model": {
                    "conditions": [],
                    "datasource": expression,
                    "expression": "A",
                    "reducer": "last",
                    "refId": "B",
                    "type": "reduce",
                },
            },
            {
                "refId": "C",
                "relativeTimeRange": {"from": 0, "to": 0},
                "datasourceUid": EXPRESSION_UID,
                "model": {
                    "conditions": [condition],
                    "datasource": expression,
                    "expression": "B",
                    "refId": "C",
                    "type": "threshold",
                },
            },
        ],
    }


def _delivery() -> dict[str, Any]:
    """The contact point and policy: deployment wiring, not car configuration.

    One contact point, posting to a pit service by its compose name. The
    receiver is unauthenticated by design -- see POST /grafana-alerts in
    pit/session_control/service.py for why the operator key deliberately
    does not appear in a committed file. P7.2 moves the URL to `notifier`.
    """
    return {
        "contactPoints": [
            {
                "orgId": 1,
                "name": "openlaps-local",
                "receivers": [
                    {
                        "uid": "openlaps-local-webhook",
                        "type": "webhook",
                        "settings": {
                            "url": "http://session-control:8080/grafana-alerts",
                            "httpMethod": "POST",
                        },
                        "disableResolveMessage": False,
                    }
                ],
            }
        ],
        "policies": [
            {
                "orgId": 1,
                "receiver": "openlaps-local",
                "group_by": ["grafana_folder", "alertname"],
                # Send the first notification the moment a group has an alert,
                # follow-ups for the group every 10 s, and leave repeats to the
                # notifier's acknowledgement logic rather than Grafana's clock.
                "group_wait": "0s",
                "group_interval": "10s",
                "repeat_interval": "4h",
            }
        ],
    }


def render(profile_dir: Path) -> tuple[str, str]:
    """Render `profile_dir/alarms.yaml`; return (output file name, YAML text)."""
    profile = load_profile(profile_dir)
    alarms_path = profile_dir / "alarms.yaml"
    try:
        spec = AlarmsFile.model_validate(yaml.safe_load(alarms_path.read_text(encoding="utf-8")))
    except ValidationError as exc:
        raise AlarmError(f"{alarms_path}: {exc}") from exc
    vehicle = profile.vehicle.vehicle.id
    channels = profile.catalog.channels
    rules = [_rule(uid, alarm, vehicle, channels) for uid, alarm in spec.alarms.items()]
    document: dict[str, Any] = {
        "apiVersion": 1,
        "groups": [
            {
                "orgId": 1,
                "name": f"openlaps-{spec.group}",
                "folder": "openlaps",
                "interval": GROUP_INTERVAL,
                "rules": rules,
            }
        ],
    }
    document.update(_delivery())
    shown = alarms_path.relative_to(ROOT) if alarms_path.is_relative_to(ROOT) else alarms_path
    header = (
        f"# GENERATED by tools/gen_alert_rules.py from {shown}.\n"
        "# Do not edit: change the profile's alarms.yaml and re-run the generator.\n"
        "# tests/test_gen_alert_rules.py fails when this file is stale.\n"
    )
    body = yaml.safe_dump(document, sort_keys=False, width=88, allow_unicode=True)
    return f"{spec.group}.yaml", header + body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gen_alert_rules", description=__doc__)
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE), help="profile directory")
    parser.add_argument("--out-dir", default=str(ALERTING_DIR), help="provisioning/alerting/")
    parser.add_argument("--check", action="store_true", help="exit 1 if the output is stale")
    args = parser.parse_args(argv)
    try:
        name, text = render(Path(args.profile))
    except (AlarmError, ConfigError, OSError) as exc:
        print(f"gen_alert_rules: {exc}", file=sys.stderr)
        return 2
    target = Path(args.out_dir) / name
    if args.check:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current != text:
            print(f"gen_alert_rules: {target} is stale; re-run without --check", file=sys.stderr)
            return 1
        print(f"gen_alert_rules: {target} is current")
        return 0
    target.write_text(text, encoding="utf-8")
    print(f"gen_alert_rules: wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
