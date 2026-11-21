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
    "handling.json": "handling",
    "video.json": "video",
    "system.json": "system-status",
    "throttle.json": "throttle",
}


def test_phase_6_adds_exactly_five_endurance_dashboards() -> None:
    actual = {
        path.name: json.loads(path.read_text(encoding="utf-8"))["uid"]
        for path in DASHBOARDS.glob("*.json")
    }
    assert actual == {**NON_ENDURANCE, **EXPECTED}


def test_pitwall_is_sparse_live_timing_with_pit_extrapolation() -> None:
    dashboard = _dashboard("pitwall.json")
    panels = _panels(dashboard)
    topics = _all_topics(dashboard)

    # 1 s until the BEERS 36 Hour: thirteen SQL queries, the map and both
    # tables redrawn every second made the wall sluggish. The lap, sector and
    # delta tiles are MQTT streams and never waited for a refresh.
    assert dashboard["refresh"] == "5s"
    non_row_panels = [panel for panel in panels if panel["type"] != "row"]
    alert_strips = [panel for panel in non_row_panels if panel["type"] == "alertlist"]
    assert len(alert_strips) == 1
    # Thirteen until the BEERS 36 Hour (2026-11-14), which added a tile per
    # sector, best lap, the race standings row and the refuel countdown.
    assert len(non_row_panels) - len(alert_strips) <= 18
    assert {
        "openlaps/$vehicle/timing.lap_elapsed_pit",
        "openlaps/$vehicle/timing.sector_1_pit",
        "openlaps/$vehicle/timing.sector_2_pit",
        "openlaps/$vehicle/timing.sector_3_pit",
        "openlaps/$vehicle/timing.delta_best",
        "openlaps/$vehicle/timing.predicted_lap",
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
        "openlaps/$vehicle/timing.sector_1_pit",
        "openlaps/$vehicle/timing.sector_2_pit",
        "openlaps/$vehicle/timing.sector_3_pit",
        "openlaps/$vehicle/lap.last_time",
        "openlaps/$vehicle/timing.delta_best",
    }
    for panel in panels:
        if {target.get("topic") for target in panel.get("targets", [])} & timing_topics:
            include = panel["transformations"][0]["options"]["include"]["names"]
            assert include == ["display"], f"panel {panel['id']} shows {include}"

    # The stint clock is a browser-local count-up: it ticks in the browser
    # from a queried timestamp, with no live stream or repainting value
    # behind it. The lap and sector browser clocks were removed 2026-11-14;
    # the pit-extrapolated Lap and Sector panels are the ones the wall reads.
    # The refuel release is the same idea counting down: to the earliest
    # legal release, from the refuel line crossing.
    clocks = {
        panel["title"]: panel["options"]
        for panel in panels
        if panel["type"] == "grafana-clock-panel"
    }
    assert set(clocks) == {"Stint clock", "Refuel release in"}
    assert clocks["Stint clock"]["mode"] == "countup"
    assert clocks["Refuel release in"]["mode"] == "countdown"
    for options in clocks.values():
        settings = options[f"{options['mode']}Settings"]
        assert settings["source"] == "query"
        assert settings["queryField"]


def test_fuel_dashboard_is_a_consumer_of_the_strategy_views() -> None:
    """P7.9: the arithmetic lives in the strategy service, not in a panel."""
    dashboard = _dashboard("fuel.json")
    sql = _all_sql(dashboard)
    text = json.dumps(dashboard).lower()
    panels = _panels(dashboard)

    # The measurements are still drawn from the fuel views; every projection
    # comes from the strategy service's table through its views.
    assert "v_lap_fuel" in sql
    # Every fuel level on this dashboard is the counter-based one; the tank
    # level sender does not read the bottom of the tank and is not drawn.
    assert "v_stint_fuel_level" not in sql
    assert "car.fuel_level" not in text
    assert "time_to_dry_s_lo" in sql
    assert "v_strategy_latest" in sql and "v_strategy_history" in sql
    assert "v_watch_findings" in sql and "strategy.%" in sql
    for column in (
        "laps_to_dry_lo",
        "laps_to_dry_hi",
        "window_open_lap",
        "window_close_lap",
        "target_lap_s",
        "driver_time_remaining_s",
        "stop_plan",
        "rebase_confidence",
    ):
        assert column in sql, column
    # No panel computes a projection any more. The refuel release counts
    # down in the browser from the refuel line crossing plus the race plan's
    # minimum (v_race_plan), so it starts at the line and not a lap later.
    assert "v_race_plan" in sql and "refuel_min_s" in sql
    assert "level_end_l / fuel_used_l" not in sql
    assert "480.0" not in sql and "interval '8 minutes'" not in sql
    assert "reset" in text
    assert "lower bound" in text
    # Only the pickers a panel reads: a picker nothing reads looks broken.
    assert [item["name"] for item in dashboard["templating"]["list"]] == ["vehicle", "session"]
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


def test_reliability_dashboard_uses_long_range_aggregate_and_celsius_units() -> None:
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
    # The Haltech temperature channels are stored in Kelvin (docs/CATALOG.md).
    # These panels convert in SQL so the axis reads in degrees Celsius.
    for panel in temperature_panels:
        assert panel["fieldConfig"]["defaults"]["unit"] == "celsius"
        for target in panel["targets"]:
            assert "- 273.15" in target["rawSql"]


def _session_scoped(dashboard: dict[str, Any]) -> None:
    """A report dashboard reads its session picker, never the time range."""
    variables = {item["name"]: item for item in dashboard["templating"]["list"]}
    session = variables["session"]
    assert session["includeAll"] is False and session["current"] == {}
    assert "$__timeFilter" not in session["query"]
    assert "ORDER BY min(crossed_at) DESC" in session["query"], "newest session first"
    assert dashboard["timepicker"] == {"hidden": True}
    for panel in _panels(dashboard):
        for target in panel.get("targets", []):
            sql = target["rawSql"]
            assert "$__time" not in sql, f"panel {panel['id']} still reads the time range"
            assert "'$session'" in sql, f"panel {panel['id']} ignores the session picker"
    for variable in variables.values():
        # Grafana's alphabetical sort puts stint 10 ahead of stint 2.
        assert variable["sort"] == 0, variable["name"]


def test_lap_analysis_is_session_scoped_and_counts_laps_in_crossing_order() -> None:
    dashboard = _dashboard("laps.json")
    sql = _all_sql(dashboard)
    variables = {item["name"]: item for item in dashboard["templating"]["list"]}

    _session_scoped(dashboard)
    assert list(variables) == ["vehicle", "session", "driver", "stint"]
    assert "v_lap_sectors" in sql
    assert "v_session_laps" in sql
    # lap_number restarts with the agent; nothing here may show it.
    assert "lap_number" not in sql and "session_lap" in sql
    scatter = next(panel for panel in _panels(dashboard) if panel["type"] == "xychart")
    assert "clean" in scatter["targets"][0]["rawSql"]
    # The lap-time clock unit belongs to the lap-time field alone: set as the
    # panel default it also formatted the lap axis and the hover time.
    assert "unit" not in scatter["fieldConfig"]["defaults"]
    # Auto mapping still walks the series list: an empty list maps nothing
    # and the panel draws "Err". One empty entry means every frame.
    assert scatter["options"]["mapping"] == "auto"
    assert scatter["options"]["series"] == [{}]


def test_stint_report_covers_stops_driver_consistency_fuel_and_open_stops() -> None:
    dashboard = _dashboard("stints.json")
    sql = _all_sql(dashboard)
    text = json.dumps(dashboard).lower()

    _session_scoped(dashboard)
    assert "v_pit_stops" in sql
    assert "v_lap_fuel" in sql
    assert "median" in text
    assert "driving time" in text
    assert "open" in text
    for panel in _panels(dashboard):
        panel_sql = panel["targets"][0]["rawSql"]
        # Every panel names who or which stop a row is about.
        assert '"Driver' in panel_sql or '"Stop"' in panel_sql, panel["title"]
        # Seconds as a Grafana unit render as decimal minutes past 60.
        assert panel["fieldConfig"]["defaults"].get("unit") != "s", panel["title"]
        if panel["type"] == "barchart":
            # A panel-wide unit also formats the text labels along the bars'
            # axis: a clock unit turned every stop's label into NaN.
            assert "unit" not in panel["fieldConfig"]["defaults"], panel["title"]
