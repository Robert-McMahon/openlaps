"""Phase 6 contracts for the endurance dashboard set."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = ROOT / "deploy/pit-config/grafana/dashboards"

EXPECTED = {
    "pitwall.json": "pitwall",
    "fuel.json": "fuel",
    "reliability.json": "reliability",
    "laps.json": "laps",
    "stints.json": "stints",
}


def _dashboard(name: str) -> dict[str, Any]:
    return json.loads((DASHBOARDS / name).read_text(encoding="utf-8"))


def _panels(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    pending = list(dashboard["panels"])
    result: list[dict[str, Any]] = []
    while pending:
        panel = pending.pop(0)
        result.append(panel)
        pending[0:0] = panel.get("panels", [])
    return result


def _all_sql(dashboard: dict[str, Any]) -> str:
    return "\n".join(
        target["rawSql"]
        for panel in _panels(dashboard)
        for target in panel.get("targets", [])
        if "rawSql" in target
    )


def _all_topics(dashboard: dict[str, Any]) -> set[str]:
    return {
        target["topic"]
        for panel in _panels(dashboard)
        for target in panel.get("targets", [])
        if "topic" in target
    }


def test_phase_6_adds_exactly_five_endurance_dashboards() -> None:
    actual = {
        path.name: json.loads(path.read_text(encoding="utf-8"))["uid"]
        for path in DASHBOARDS.glob("*.json")
    }
    assert actual == {"car.json": "car", **EXPECTED}


def test_pitwall_is_sparse_live_timing_with_pit_extrapolation_and_fix_quality() -> None:
    dashboard = _dashboard("pitwall.json")
    panels = _panels(dashboard)
    topics = _all_topics(dashboard)

    assert dashboard["refresh"] == "1s"
    assert len([panel for panel in panels if panel["type"] != "row"]) <= 12
    assert {
        "openlaps/$vehicle/lap.number",
        "openlaps/$vehicle/timing.lap_elapsed_pit",
        "openlaps/$vehicle/timing.sector_elapsed_pit",
        "openlaps/$vehicle/lap.last_time",
        "openlaps/$vehicle/lap.best_time",
        "openlaps/$vehicle/timing.delta_best",
        "openlaps/$vehicle/timing.predicted_lap",
        "openlaps/$vehicle/position.lat",
        "openlaps/$vehicle/position.lon",
        "openlaps/$vehicle/position.speed",
        "openlaps/$vehicle/position.fix_quality",
    } <= topics
    assert any("extrapolat" in panel.get("description", "").lower() for panel in panels)

    # Lap and sector times are race-formatted: the timing panels show the
    # publishers' pre-formatted `display` field, not raw seconds.
    timing_topics = {
        "openlaps/$vehicle/timing.lap_elapsed_pit",
        "openlaps/$vehicle/timing.sector_elapsed_pit",
        "openlaps/$vehicle/lap.last_time",
        "openlaps/$vehicle/timing.delta_best",
    }
    for panel in panels:
        if {target.get("topic") for target in panel.get("targets", [])} & timing_topics:
            include = panel["transformations"][0]["options"]["include"]["names"]
            assert include == ["display"], f"panel {panel['id']} shows {include}"

    # Browser-local count-up clocks (lap, sector, stint): they tick in the
    # browser from a queried crossing timestamp, with no live stream or
    # repainting value behind them.
    clocks = [panel for panel in panels if panel["type"] == "grafana-clock-panel"]
    assert len(clocks) == 3
    for clock in clocks:
        assert clock["options"]["mode"] == "countup"
        assert clock["options"]["countupSettings"]["source"] == "query"
        assert clock["options"]["countupSettings"]["queryField"]


def test_fuel_dashboard_uses_fuel_views_and_has_refuel_legal_clock() -> None:
    dashboard = _dashboard("fuel.json")
    sql = _all_sql(dashboard)
    text = json.dumps(dashboard).lower()

    assert "v_lap_fuel" in sql
    assert "v_stint_fuel_level" in sql
    assert "v_pit_stops" in sql
    assert "8 minute" in text or "8-minute" in text
    assert "reset" in text
    assert "cross-check" in text
    assert all(
        name in {item["name"] for item in dashboard["templating"]["list"]}
        for name in ("vehicle", "session", "driver", "stint", "lap")
    )


def test_reliability_dashboard_uses_long_range_aggregate_and_kelvin_units() -> None:
    dashboard = _dashboard("reliability.json")
    sql = _all_sql(dashboard)
    panels = _panels(dashboard)

    assert "v_samples_1m_named" in sql
    assert "car.oil_pressure" in sql and "car.rpm" in sql
    assert "car.knock_level1" in sql and "car.engine_protection_severity" in sql
    assert "car.pd16_total_current" in sql and "car.thermo_fan_1_current" in sql
    temperature_panels = [
        panel for panel in panels if "temperature" in panel.get("title", "").lower()
    ]
    assert temperature_panels
    assert all(panel["fieldConfig"]["defaults"]["unit"] == "kelvin" for panel in temperature_panels)


def test_lap_analysis_uses_sector_view_and_identity_based_lap_picker() -> None:
    dashboard = _dashboard("laps.json")
    sql = _all_sql(dashboard)
    variables = {item["name"]: item for item in dashboard["templating"]["list"]}

    assert "v_lap_sectors" in sql
    assert "lap_time_s" in sql
    assert "stint_number" in sql
    assert "valid" in sql
    assert "lap_id" in variables["lap"]["query"]


def test_stint_report_covers_stops_driver_consistency_fuel_and_open_stops() -> None:
    dashboard = _dashboard("stints.json")
    sql = _all_sql(dashboard)
    text = json.dumps(dashboard).lower()

    assert "v_pit_stops" in sql
    assert "v_lap_fuel" in sql
    assert "median" in text
    assert "driving time" in text
    assert "open" in text
