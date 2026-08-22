"""P5.5 contracts for provisioned Grafana dashboards and their queries."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, LiteralString, cast
from urllib.parse import urlsplit

import psycopg
import yaml
from psycopg.conninfo import make_conninfo

from agent.agent import agent_derived_channels
from pit.db.migrate import apply_migrations

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = ROOT / "deploy/pit-config/grafana/dashboards"
DATASOURCES = ROOT / "deploy/pit-config/grafana/provisioning/datasources"
CATALOG = ROOT / "profiles/example-club-racer/catalog.yaml"

CORE_PANEL_TYPES = {
    "alertlist",
    "annolist",
    "barchart",
    "bargauge",
    "candlestick",
    "canvas",
    "dashlist",
    "flamegraph",
    "gauge",
    "geomap",
    "heatmap",
    "histogram",
    "logs",
    "news",
    "nodeGraph",
    "piechart",
    "row",
    "stat",
    "state-timeline",
    "status-history",
    "table",
    "text",
    "timeseries",
    "trend",
    "xychart",
}
CORE_DATASOURCE_TYPES = {"grafana-postgresql-datasource"}
ALLOWED_PLUGIN_DATASOURCE_TYPES = {"grafana-mqtt-datasource"}
SECRET_KEY = re.compile(r"(?:password|passwd|token|api[_-]?key|authorization)", re.IGNORECASE)
BEARER = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
GRAFANA_MACRO = re.compile(r"\$__[A-Za-z][A-Za-z0-9_]*(?:\([^)]*\))?")
GRAFANA_VARIABLE = re.compile(r"\$(?!__)(?:[A-Za-z][A-Za-z0-9_]*|\{[^}]+\})")
MQTT_TOPIC = re.compile(r"openlaps/\$vehicle/([a-z][a-z0-9_.]*)")
SQL_ALIAS = re.compile(r'\bAS\s+"([^"]+)"', re.IGNORECASE)
VEHICLE = "example-club-racer"


def _dashboards() -> list[tuple[Path, dict[str, Any]]]:
    return [
        (path, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(DASHBOARDS.glob("*.json"))
    ]


def _panels(dashboard: dict[str, Any]) -> Iterator[dict[str, Any]]:
    pending = list(dashboard.get("panels", []))
    while pending:
        panel = pending.pop(0)
        yield panel
        pending[0:0] = panel.get("panels", [])


def _provisioned_datasources() -> dict[str, str]:
    provisioned: dict[str, str] = {}
    for path in sorted(DATASOURCES.glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for datasource in document.get("datasources", []):
            uid = datasource["uid"]
            assert uid not in provisioned, f"duplicate datasource uid {uid!r}"
            provisioned[uid] = datasource["type"]
    return provisioned


def _datasource_references(
    dashboard: dict[str, Any],
) -> Iterator[tuple[str, Any]]:
    for panel in _panels(dashboard):
        if panel.get("type") == "row":
            if "datasource" in panel:
                yield f"row panel {panel.get('id')}", panel["datasource"]
            continue
        yield f"panel {panel.get('id')}", panel.get("datasource")
        for target in panel.get("targets", []):
            yield f"panel {panel.get('id')} target {target.get('refId')}", target.get("datasource")


def _credential_findings(value: Any, path: str = "dashboard") -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if SECRET_KEY.search(str(key)):
                yield child_path
            yield from _credential_findings(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _credential_findings(child, f"{path}[{index}]")
    elif isinstance(value, str):
        if SECRET_KEY.search(value) or BEARER.search(value):
            yield path
        parsed = urlsplit(value)
        if parsed.scheme and (parsed.username is not None or parsed.password is not None):
            yield path


def _known_channels() -> set[str]:
    catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    channels = set(catalog["channels"])
    channels.update(channel.name for channel in agent_derived_channels([]))
    return channels


def _raw_sql_targets() -> Iterator[tuple[Path, dict[str, Any], dict[str, Any]]]:
    for path, dashboard in _dashboards():
        for panel in _panels(dashboard):
            for target in panel.get("targets", []):
                if "rawSql" in target:
                    yield path, panel, target


def _query_channel(raw_sql: str) -> str:
    match = re.search(r"channel\s*=\s*'([^']+)'", raw_sql)
    assert match is not None, "dashboard query does not select one literal channel"
    return match.group(1)


def _render_sql(raw_sql: str, *, start: datetime, end: datetime) -> str:
    source = "v_samples_1s_named" if "FROM v_samples_1s_named" in raw_sql else "v_samples_named"
    rendered = re.sub(
        r"\$__timeGroup\(\s*([^,]+?)\s*,\s*\$__interval\s*\)",
        r"time_bucket(INTERVAL '1 second', \1)",
        raw_sql,
    )
    rendered = re.sub(
        r"\$__timeFilter\(\s*([^)]+?)\s*\)",
        lambda match: (
            f"{match.group(1)} BETWEEN TIMESTAMPTZ '{start.isoformat()}' "
            f"AND TIMESTAMPTZ '{end.isoformat()}'"
        ),
        rendered,
    )
    rendered = rendered.replace("$__interval", "1 second")
    rendered = rendered.replace("$vehicle", VEHICLE)
    rendered = rendered.replace("$session", "all")
    rendered = rendered.replace("$trace_source", source)
    unknown = GRAFANA_MACRO.findall(rendered)
    assert not unknown, f"unsupported Grafana macros: {unknown}"
    variables = GRAFANA_VARIABLE.findall(rendered)
    assert not variables, f"unsupported Grafana template variables: {variables}"
    return rendered


def test_dashboard_json_files_have_unique_non_empty_uids() -> None:
    dashboards = _dashboards()

    assert dashboards, "no provisioned dashboard JSON files found"
    uids = [dashboard.get("uid") for _, dashboard in dashboards]
    assert all(isinstance(uid, str) and uid.strip() for uid in uids)
    assert len(uids) == len(set(uids))


def test_panels_and_targets_reference_explicit_provisioned_datasource_uids() -> None:
    provisioned = _provisioned_datasources()
    assert provisioned

    for path, dashboard in _dashboards():
        for location, datasource in _datasource_references(dashboard):
            assert isinstance(datasource, dict), f"{path.name} {location} defaults its datasource"
            assert datasource.get("uid") in provisioned, (
                f"{path.name} {location} references unprovisioned datasource uid "
                f"{datasource.get('uid')!r}"
            )
            assert datasource.get("type") == provisioned[datasource["uid"]]


def test_dashboards_contain_no_credential_shaped_content() -> None:
    for path, dashboard in _dashboards():
        findings = list(_credential_findings(dashboard))
        assert not findings, f"{path.name} contains credential-shaped content at {findings}"


def test_dashboards_use_only_core_panels_and_the_single_allowed_plugin() -> None:
    provisioned = _provisioned_datasources()
    datasource_types = set(provisioned.values())
    assert datasource_types <= CORE_DATASOURCE_TYPES | ALLOWED_PLUGIN_DATASOURCE_TYPES
    assert datasource_types - CORE_DATASOURCE_TYPES == ALLOWED_PLUGIN_DATASOURCE_TYPES

    for path, dashboard in _dashboards():
        for panel in _panels(dashboard):
            assert panel.get("type") in CORE_PANEL_TYPES, (
                f"{path.name} panel {panel.get('id')} uses non-core type {panel.get('type')!r}"
            )


def test_mqtt_topics_follow_the_per_channel_contract() -> None:
    known_channels = _known_channels()

    for path, dashboard in _dashboards():
        for panel in _panels(dashboard):
            for target in panel.get("targets", []):
                datasource = target.get("datasource", {})
                if datasource.get("type") != "grafana-mqtt-datasource":
                    continue
                topic = target.get("topic", "")
                match = MQTT_TOPIC.fullmatch(topic)
                assert match, (
                    f"{path.name} panel {panel.get('id')} has invalid MQTT topic {topic!r}"
                )
                assert match.group(1) in known_channels, (
                    f"{path.name} panel {panel.get('id')} names unknown channel {match.group(1)!r}"
                )


def test_every_dashboard_query_executes_and_returns_configured_fields(timescale_dsn) -> None:
    targets = list(_raw_sql_targets())
    assert targets, "no dashboard rawSql targets found"
    stamp = datetime(2026, 8, 1, 4, 30, tzinfo=UTC)
    channels = sorted({_query_channel(target["rawSql"]) for _, _, target in targets})

    with psycopg.connect(timescale_dsn) as conn:
        apply_migrations(conn)
        for index, channel in enumerate(channels, start=1):
            channel_key = conn.execute(
                "INSERT INTO channels (vehicle_id, name, units, value_type) "
                "VALUES (%s, %s, '', 1) RETURNING channel_key",
                (VEHICLE, channel),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO samples (time, channel_key, value) VALUES (%s, %s, %s)",
                (stamp, channel_key, float(index)),
            )
        conn.commit()
        conn.autocommit = True
        conn.execute(
            "CALL refresh_continuous_aggregate('samples_1s', %s, %s)",
            (stamp, stamp + timedelta(seconds=1)),
        )
        conn.autocommit = False

    reader_dsn = make_conninfo(
        timescale_dsn,
        user="grafana_ro",
        password="openlaps-grafana-test",
    )
    panel_columns: dict[tuple[str, int], set[str]] = {}
    panel_expected: dict[tuple[str, int], set[str]] = {}
    with psycopg.connect(reader_dsn) as reader:
        for path, panel, target in targets:
            sql = _render_sql(
                target["rawSql"],
                start=stamp - timedelta(seconds=1),
                end=stamp + timedelta(seconds=1),
            )
            result = reader.execute(cast(LiteralString, sql))
            columns = {column.name for column in result.description or []}
            aliases = set(SQL_ALIAS.findall(target["rawSql"]))
            assert columns == {"time", *aliases}, (
                f"{path.name} panel {panel.get('id')} target {target.get('refId')} "
                f"returned {columns}, expected time plus {aliases}"
            )
            assert result.fetchall(), (
                f"{path.name} panel {panel.get('id')} target {target.get('refId')} returned no rows"
            )

            key = (path.name, panel["id"])
            panel_columns.setdefault(key, set()).update(columns)
            expected = panel_expected.setdefault(key, set())
            for override in panel.get("fieldConfig", {}).get("overrides", []):
                matcher = override.get("matcher", {})
                if matcher.get("id") == "byName":
                    expected.add(matcher["options"])
                expected.update(
                    prop["value"]
                    for prop in override.get("properties", [])
                    if prop.get("id") == "custom.fillBelowTo"
                )

    for key, expected in panel_expected.items():
        assert expected <= panel_columns[key], (
            f"{key[0]} panel {key[1]} config expects missing query fields "
            f"{expected - panel_columns[key]}"
        )
