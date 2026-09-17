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


# Dashboards that are not part of the phase 6 endurance set but are
# provisioned from the same directory: the live gauge panel (P5.3), the car
# camera (video feed port) and the bring-up/health surface added alongside
# the GNSS work.
NON_ENDURANCE = {
    "car.json": "car",
    "video.json": "video",
    "system.json": "system-status",
}


def test_phase_6_adds_exactly_five_endurance_dashboards() -> None:
    actual = {
        path.name: json.loads(path.read_text(encoding="utf-8"))["uid"]
        for path in DASHBOARDS.glob("*.json")
    }
    assert actual == {**NON_ENDURANCE, **EXPECTED}


def test_pitwall_is_sparse_live_timing_with_pit_extrapolation_and_fix_quality() -> None:
    dashboard = _dashboard("pitwall.json")
    panels = _panels(dashboard)
    topics = _all_topics(dashboard)

    assert dashboard["refresh"] == "1s"
    non_row_panels = [panel for panel in panels if panel["type"] != "row"]
    alert_strips = [panel for panel in non_row_panels if panel["type"] == "alertlist"]
    assert len(alert_strips) == 1
    assert len(non_row_panels) - len(alert_strips) <= 12
    assert {
        "openlaps/$vehicle/lap.number",
        "openlaps/$vehicle/timing.lap_elapsed_pit",
        "openlaps/$vehicle/timing.sector_elapsed_pit",
        "openlaps/$vehicle/lap.last_time",
        "openlaps/$vehicle/lap.best_time",
        "openlaps/$vehicle/timing.delta_best",
        "openlaps/$vehicle/timing.predicted_lap",
        "openlaps/$vehicle/position.fix_quality",
    } <= topics
    assert any("extrapolat" in panel.get("description", "").lower() for panel in panels)

    # The track map is the one live panel that cannot be MQTT-fed. The live
    # datasource returns one frame per topic, each carrying only `time` and
    # `value`, so lat and lon never share a frame and the geomap has no
    # location field to place a point from -- it drew nothing for as long as
    # it was wired that way. It reads the same three channels out of
    # Timescale instead, pivoted into one frame with real latitude/longitude
    # columns; ingest lag is ~200 ms, which a track map cannot show.
    geomaps = [panel for panel in panels if panel["type"] == "geomap"]
    assert geomaps, "pitwall must still carry a track map"
    for panel in geomaps:
        assert panel["datasource"]["uid"] == "timescale"
        assert panel["options"]["layers"], "a geomap without layers cannot draw"
        sql = " ".join(target.get("rawSql", "") for target in panel["targets"])
        for channel in ("position.lat", "position.lon", "position.speed"):
            assert channel in sql, f"track map no longer reads {channel}"
        assert "latitude" in sql and "longitude" in sql, (
            "the pivot must expose latitude/longitude by name -- the geomap "
            "locates by field name, not by column order"
        )

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


def test_fuel_dashboard_is_a_consumer_of_the_strategy_views() -> None:
    """P7.9: the arithmetic lives in the strategy service, not in a panel."""
    dashboard = _dashboard("fuel.json")
    sql = _all_sql(dashboard)
    text = json.dumps(dashboard).lower()
    panels = _panels(dashboard)

    # The measurements are still drawn from the fuel views; every projection
    # comes from the strategy service's table through its views.
    assert "v_lap_fuel" in sql
    assert "v_stint_fuel_level" in sql
    assert "v_strategy_latest" in sql and "v_strategy_history" in sql
    assert "v_watch_findings" in sql and "strategy.%" in sql
    for column in (
        "laps_to_dry_lo",
        "laps_to_dry_hi",
        "window_open_lap",
        "window_close_lap",
        "target_lap_s",
        "driver_time_remaining_s",
        "refuel_release_at",
        "refuel_remaining_s",
        "stop_plan",
        "rebase_confidence",
    ):
        assert column in sql, column
    # No panel computes a projection any more, and the legal minimum comes
    # from the race plan rather than a number typed into a panel.
    assert "level_end_l / fuel_used_l" not in sql
    assert "480.0" not in sql and "interval '8 minutes'" not in sql
    assert "reset" in text
    assert "cross-check" in text
    assert "lower bound" in text
    assert all(
        name in {item["name"] for item in dashboard["templating"]["list"]}
        for name in ("vehicle", "session", "driver", "stint", "lap")
    )
    # A projection from three laps and one from a full stint must not look
    # identical: the band panel fills between its lower and upper bound.
    band = next(panel for panel in panels if panel["id"] == 3)
    assert band["type"] == "timeseries"
    fills = [
        prop["value"]
        for override in band["fieldConfig"]["overrides"]
        for prop in override["properties"]
        if prop["id"] == "custom.fillBelowTo"
    ]
    assert fills == ["Laps to dry (lower)"]


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
