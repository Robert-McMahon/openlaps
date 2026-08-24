"""P5.4 contract for the provisioned car-and-engine dashboard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "deploy/pit-config/grafana/dashboards/car.json"

LIVE_CHANNELS = {
    "car.rpm",
    "car.throttle_pos",
    "car.brake_pedal_switch",
    "car.gear",
    "car.vehicle_speed",
    "car.coolant_temp",
    "car.oil_temp",
    "car.oil_pressure",
    "car.map",
    "car.lambda1",
    "car.battery_v",
    "car.fuel_pressure",
}
CONTEXT_CHANNELS = {
    "lap.number",
    "timing.lap_elapsed",
    "lap.last_time",
    "lap.best_time",
    "timing.delta_best",
    "sys.agent.publish_lag_ms",
    "sys.agent.publish_drops",
    "sys.agent.rbe_suppressed",
}
TRACE_CHANNELS = LIVE_CHANNELS | {"car.knock_level1", "car.fuel_level"}


def _dashboard() -> dict[str, Any]:
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))


def _data_panels(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    return [panel for panel in dashboard["panels"] if panel["type"] != "row"]


def test_car_dashboard_has_three_rows_and_operator_link() -> None:
    dashboard = _dashboard()

    assert dashboard["uid"] == "car"
    assert [panel["title"] for panel in dashboard["panels"] if panel["type"] == "row"] == [
        "Live",
        "Context",
        "Traces",
    ]
    assert any(
        link["title"] == "Session control" and ":8080" in link["url"] for link in dashboard["links"]
    )


def test_live_and_context_panels_cover_the_required_mqtt_channels() -> None:
    panels = _data_panels(_dashboard())
    mqtt_panels = [panel for panel in panels if panel["datasource"]["uid"] == "mqtt-live"]

    assert all(panel["title"].strip() for panel in mqtt_panels)
    topics = {target["topic"] for panel in mqtt_panels for target in panel["targets"]}
    expected_topics = {
        f"openlaps/$vehicle/{channel}" for channel in LIVE_CHANNELS | CONTEXT_CHANNELS
    }
    assert topics == expected_topics


def test_trace_panels_use_both_named_views_and_honour_the_time_picker() -> None:
    panels = _data_panels(_dashboard())
    sql_targets = [
        target
        for panel in panels
        if panel["datasource"]["uid"] == "timescale"
        for target in panel["targets"]
    ]
    sql = "\n".join(target["rawSql"] for target in sql_targets)

    assert TRACE_CHANNELS <= {channel for channel in TRACE_CHANNELS if channel in sql}
    assert "v_samples_1s_named" in sql
    assert "v_samples_named" in sql
    assert "FROM samples" not in sql
    assert all("$__timeFilter" in target["rawSql"] for target in sql_targets)

    aggregate_targets = [
        target for target in sql_targets if "v_samples_1s_named" in target["rawSql"]
    ]
    assert aggregate_targets
    assert all(
        all(stat in target["rawSql"] for stat in ("avg", "min", "max"))
        for target in aggregate_targets
    )


def test_dashboard_variables_come_from_the_stable_relational_read_surface() -> None:
    variables = {item["name"]: item for item in _dashboard()["templating"]["list"]}

    assert set(variables) == {"vehicle", "session", "driver", "stint", "lap", "trace_source"}
    assert "v_samples_named" in variables["vehicle"]["query"]
    assert "v_laps" in variables["session"]["query"]
    assert "v_laps" in variables["driver"]["query"]
    assert "driver AS __value" in variables["driver"]["query"]
    assert "FROM v_laps" in variables["stint"]["query"]
    assert "$session" in variables["stint"]["query"]
    assert "lap_id" in variables["lap"]["query"]
    assert "lap_number" in variables["lap"]["query"]
    assert "COALESCE(driver" in variables["lap"]["query"]
    assert "$session" in variables["lap"]["query"]
    assert variables["trace_source"]["type"] == "custom"
    assert set(variables["trace_source"]["options"][index]["value"] for index in (0, 1)) == {
        "v_samples_1s_named",
        "v_samples_named",
    }


def test_temperature_panels_declare_kelvin_and_dashboard_has_no_credentials() -> None:
    dashboard = _dashboard()
    panels = _data_panels(dashboard)
    temperature_panels = [
        panel for panel in panels if panel["title"] in {"Coolant temperature", "Oil temperature"}
    ]

    assert len(temperature_panels) == 4  # live and trace versions of each
    assert all(panel["fieldConfig"]["defaults"]["unit"] == "kelvin" for panel in temperature_panels)

    serialized = json.dumps(dashboard).lower()
    assert not any(term in serialized for term in ("password", "api_key", "bearer "))
    assert {panel["datasource"]["uid"] for panel in panels} == {"mqtt-live", "timescale"}
