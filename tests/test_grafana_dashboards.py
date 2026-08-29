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
from pit.timing_extrapolator.config import load_config as load_timing_config

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = ROOT / "deploy/pit-config/grafana/dashboards"
DATASOURCES = ROOT / "deploy/pit-config/grafana/provisioning/datasources"
ALERTING = ROOT / "deploy/pit-config/grafana/provisioning/alerting"
CATALOG = ROOT / "profiles/example-club-racer/catalog.yaml"
VEHICLE_YAML = ROOT / "profiles/example-club-racer/vehicle.yaml"
PIT_COMPOSE = ROOT / "deploy/pit-compose.yaml"
TIMING_EXTRAPOLATOR = ROOT / "deploy/pit-config/timing-extrapolator.yaml"

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


def _pinned_preinstall_plugins() -> set[str]:
    compose = yaml.safe_load(PIT_COMPOSE.read_text(encoding="utf-8"))
    preinstall = compose["services"]["grafana"]["environment"]["GF_PLUGINS_PREINSTALL_SYNC"]
    plugins: set[str] = set()
    for entry in preinstall.split(","):
        plugin_id, separator, version = entry.strip().partition("@")
        assert separator and plugin_id and version, f"unpinned Grafana plugin {entry!r}"
        default = re.fullmatch(r"\$\{[A-Z0-9_]+:-([^}]+)\}", version)
        pinned_version = default.group(1) if default else version
        assert re.fullmatch(r"\d+\.\d+\.\d+", pinned_version), (
            f"Grafana plugin {plugin_id!r} does not have an exact version pin"
        )
        plugins.add(plugin_id)
    return plugins


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


def _collector_names() -> list[str]:
    """Collector names the agent will derive ``sys.agent.drops.*`` from.

    These are per-collector and therefore profile-dependent, so passing an
    empty list here would silently accept a dashboard naming a drops channel
    that no collector produces -- which is exactly the class of typo this
    test exists to catch.
    """
    vehicle = yaml.safe_load(VEHICLE_YAML.read_text(encoding="utf-8"))
    names = [bus["name"] for bus in vehicle.get("buses") or []]
    names += [port["name"] for port in vehicle.get("serial") or []]
    if (vehicle.get("host") or {}).get("enabled"):
        names.append("host")
    return names


def _known_channels() -> set[str]:
    catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    channels = set(catalog["channels"])
    channels.update(channel.name for channel in agent_derived_channels(_collector_names()))
    channels.update(load_timing_config(TIMING_EXTRAPOLATOR).output_channels)
    return channels


def _raw_sql_targets() -> Iterator[tuple[Path, dict[str, Any], dict[str, Any]]]:
    for path, dashboard in _dashboards():
        for panel in _panels(dashboard):
            for target in panel.get("targets", []):
                if "rawSql" in target:
                    yield path, panel, target


def _query_channels(raw_sql: str) -> set[str]:
    """Return literal telemetry channels used by a SQL target, if any.

    Endurance dashboards also issue relational queries against lap, sector,
    fuel and stop views, so an empty result is valid.  Both equality and IN
    predicates are supported because long-range panels pivot several channels.
    """
    channels = set(re.findall(r"channel\s*=\s*'([^']+)'", raw_sql))
    for values in re.findall(r"channel\s+IN\s*\(([^)]+)\)", raw_sql, re.IGNORECASE):
        channels.update(re.findall(r"'([^']+)'", values))
    return channels


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
    rendered = rendered.replace("$session", "s-1")
    rendered = rendered.replace("$driver", "Driver A")
    rendered = rendered.replace("$stint", "1")
    rendered = rendered.replace("$lap_compare", "2")
    rendered = rendered.replace("$lap", "1")
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


def test_dashboards_and_alerting_contain_no_credential_shaped_content() -> None:
    for path, dashboard in _dashboards():
        findings = list(_credential_findings(dashboard))
        assert not findings, f"{path.name} contains credential-shaped content at {findings}"
    for path in sorted(ALERTING.glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        findings = list(_credential_findings(document, "alerting"))
        assert not findings, f"{path.name} contains credential-shaped content at {findings}"


def test_dashboard_plugins_are_derived_from_the_exact_pinned_preinstall_list() -> None:
    provisioned = _provisioned_datasources()
    datasource_types = set(provisioned.values())
    pinned_plugins = _pinned_preinstall_plugins()
    assert datasource_types <= CORE_DATASOURCE_TYPES | pinned_plugins
    used_plugins = datasource_types - CORE_DATASOURCE_TYPES

    for path, dashboard in _dashboards():
        for panel in _panels(dashboard):
            panel_type = panel.get("type")
            assert panel_type in CORE_PANEL_TYPES | pinned_plugins, (
                f"{path.name} panel {panel.get('id')} uses unpinned type {panel_type!r}"
            )
            if panel_type not in CORE_PANEL_TYPES:
                used_plugins.add(panel_type)

    assert used_plugins == pinned_plugins, "pinned plugins must be used by a datasource or panel"


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
    channels = sorted(
        {channel for _, _, target in targets for channel in _query_channels(target["rawSql"])}
        | {"car.fuel_total_used", "car.fuel_level", "car.battery_v", "car.rpm", "lap.event"}
    )

    with psycopg.connect(timescale_dsn) as conn:
        apply_migrations(conn)
        driver_id = conn.execute(
            "INSERT INTO drivers (name) VALUES ('Driver A') RETURNING driver_id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO sessions "
            "(session_id, vehicle_id, session_type, track_name, car, started, status) "
            "VALUES ('s-1', %s, 'race', 'Wanneroo', 'test-car', %s, 'active')",
            (VEHICLE, stamp - timedelta(minutes=10)),
        )
        stint_id = conn.execute(
            "INSERT INTO stints (session_id, stint_number, driver_id, started) "
            "VALUES ('s-1', 1, %s, %s) RETURNING stint_id",
            (driver_id, stamp - timedelta(minutes=10)),
        ).fetchone()[0]
        lap_ids: list[int] = []
        for lap_number, crossed_at, lap_time in (
            (1, stamp - timedelta(minutes=2), 110.0),
            (2, stamp, 108.0),
        ):
            lap_id = conn.execute(
                "INSERT INTO laps "
                "(vehicle_id, session_id, stint_id, track_name, lap_number, crossed_at, "
                "lap_time_s, valid, pit_status, direction) "
                "VALUES (%s, 's-1', %s, 'Wanneroo', %s, %s, %s, true, 'track', 'forward') "
                "RETURNING lap_id",
                (VEHICLE, stint_id, lap_number, crossed_at, lap_time),
            ).fetchone()[0]
            lap_ids.append(lap_id)
            for sector in range(1, 4):
                conn.execute(
                    "INSERT INTO lap_sectors (lap_id, sector, split_time_s, crossed_at) "
                    "VALUES (%s, %s, %s, %s)",
                    (lap_id, sector, lap_time / 3.0, crossed_at - timedelta(seconds=3 - sector)),
                )

        channel_keys: dict[str, int] = {}
        for index, channel in enumerate(channels, start=1):
            channel_key = conn.execute(
                "INSERT INTO channels (vehicle_id, name, units, value_type) "
                "VALUES (%s, %s, '', 1) RETURNING channel_key",
                (VEHICLE, channel),
            ).fetchone()[0]
            channel_keys[channel] = channel_key
            if channel != "lap.event":
                conn.execute(
                    "INSERT INTO samples (time, channel_key, value) VALUES (%s, %s, %s)",
                    (stamp, channel_key, float(index)),
                )
        for at, value in (
            (stamp - timedelta(minutes=4), 1000.0),
            (stamp - timedelta(minutes=2), 1400.0),
            (stamp, 1800.0),
        ):
            conn.execute(
                "INSERT INTO samples (time, channel_key, value) VALUES (%s, %s, %s)",
                (at, channel_keys["car.fuel_total_used"], value),
            )
        for at, value in (
            (stamp - timedelta(minutes=4), 45.0),
            (stamp - timedelta(minutes=2), 44.6),
            (stamp, 44.2),
        ):
            conn.execute(
                "INSERT INTO samples (time, channel_key, value) VALUES (%s, %s, %s)",
                (at, channel_keys["car.fuel_level"], value),
            )
        for at, kind, line in (
            (stamp - timedelta(minutes=9), "pit_entry", "PitEntryRefuel"),
            (stamp - timedelta(minutes=1), "pit_exit", "PitExitRefuel"),
        ):
            conn.execute(
                "INSERT INTO samples (time, channel_key, value_text) VALUES (%s, %s, %s)",
                (
                    at,
                    channel_keys["lap.event"],
                    json.dumps(
                        {
                            "type": kind,
                            "line": line,
                            "pit_status": "pit" if kind == "pit_entry" else "track",
                        }
                    ),
                ),
            )
        conn.commit()
        conn.autocommit = True
        for aggregate in ("samples_1s", "samples_1m"):
            if conn.execute("SELECT to_regclass(%s)", (aggregate,)).fetchone()[0] is not None:
                conn.execute(
                    f"CALL refresh_continuous_aggregate('{aggregate}', %s, %s)",
                    (stamp - timedelta(minutes=11), stamp + timedelta(minutes=1)),
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
                start=stamp - timedelta(minutes=20),
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
