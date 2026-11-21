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
# provisioned from the same directory: the live gauges (P5.3), their traces,
# and the bring-up/health surface added alongside the GNSS work. The camera
# and throttle dashboards went after the BEERS 36 Hour (the camera is a link
# to go2rtc; the throttle panels are on the traces dashboard), and the stint
# report became the second half of the session report.
NON_ENDURANCE = {
    "car.json": "car",
    "car-traces.json": "car-traces",
    "driver.json": "driver",
    "system.json": "system-status",
}


def test_the_provisioned_dashboards_are_exactly_these_eight() -> None:
    actual = {
        path.name: json.loads(path.read_text(encoding="utf-8"))["uid"]
        for path in DASHBOARDS.glob("*.json")
    }
    assert actual == {**NON_ENDURANCE, **EXPECTED}


def test_every_dashboard_carries_the_same_link_bar() -> None:
    """Finding a dashboard meant the folder list; now each links to the rest."""
    bars = []
    for name in {**NON_ENDURANCE, **EXPECTED}:
        dashboard = _dashboard(name)
        assert "openlaps" in dashboard["tags"], name
        bars.append(json.dumps(dashboard["links"], sort_keys=True))
    assert len(set(bars)) == 1
    links = {link["title"]: link for link in json.loads(bars[0])}
    assert links["openlaps"]["type"] == "dashboards" and links["openlaps"]["tags"] == ["openlaps"]
    # Passing one dashboard's pickers to the next would hand "all" to a
    # session picker that has no such option.
    assert links["openlaps"]["includeVars"] is False
    assert ":8080" in links["Session control"]["url"]
    # The camera plays in go2rtc's own page, not in a Grafana panel.
    assert ":1984" in links["Car camera"]["url"] and "mode=mse" in links["Car camera"]["url"]


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
    # sector, best lap, the race standings row and the refuel countdown; then
    # one strip of fuel and engine vitals, so the wall is the one race screen.
    assert len(non_row_panels) - len(alert_strips) <= 25
    strip = {panel["title"]: panel for panel in panels if panel["gridPos"]["y"] == 12}
    assert list(strip) == [
        "Fuel remaining",
        "Laps to dry",
        "Pit window",
        "Oil pressure",
        "Oil temperature",
        "Coolant temperature",
        "Fuel pressure",
    ]
    for title in ("Fuel remaining", "Laps to dry", "Pit window"):
        assert "v_strategy_latest" in strip[title]["targets"][0]["rawSql"]
    for title in ("Oil pressure", "Oil temperature", "Coolant temperature", "Fuel pressure"):
        assert strip[title]["datasource"]["uid"] == "mqtt-live"
        assert strip[title]["fieldConfig"]["defaults"]["color"]["mode"] == "thresholds"
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
    assert [p["title"] for p in _panels(dashboard) if p["type"] == "row"] == [
        "Laps and pace",
        "Stops, stints and driving time",
    ]
    for panel in _panels(dashboard):
        for target in panel.get("targets", []):
            sql = target["rawSql"]
            assert "$__time" not in sql, f"panel {panel['id']} still reads the time range"
            assert "'$session'" in sql, f"panel {panel['id']} ignores the session picker"
    for variable in variables.values():
        # Grafana's alphabetical sort puts stint 10 ahead of stint 2.
        assert variable["sort"] == 0, variable["name"]


def test_the_oil_pressure_trend_shows_pressure_and_rpm_on_hover() -> None:
    dashboard = _dashboard("reliability.json")
    panel = next(p for p in _panels(dashboard) if p["title"] == "Oil pressure per 1000 rpm")
    assert panel["options"]["tooltip"]["mode"] == "multi"
    for name in ("Oil pressure", "RPM"):
        properties = {
            item["id"]: item["value"]
            for override in panel["fieldConfig"]["overrides"]
            if override["matcher"]["options"] == name
            for item in override["properties"]
        }
        # Hidden from the plot would also drop it from the hover.
        assert properties["custom.hideFrom"] == {"viz": False, "legend": True, "tooltip": False}
        assert properties["custom.lineWidth"] == 0
        assert properties["custom.axisPlacement"] == "hidden"


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
    assert dashboard["title"] == "Session report"
    assert "clean" in scatter["targets"][0]["rawSql"]
    # The lap-time clock unit belongs to the lap-time field alone: set as the
    # panel default it also formatted the lap axis and the hover time.
    assert "unit" not in scatter["fieldConfig"]["defaults"]
    # By regex: with a frame per driver the field reads "Simon Lap time".
    assert {o["matcher"]["id"] for o in scatter["fieldConfig"]["overrides"]} == {"byRegexp"}
    # Without a pluginVersion of 11.1 or later Grafana takes the options for
    # the old XY chart format and rewrites them on load: the chart drew "Err",
    # or the first driver only.
    assert float(scatter["pluginVersion"].rsplit(".", 1)[0]) >= 11.1
    assert scatter["options"]["mapping"] == "auto"
    # One entry with no frame: every frame, so every driver, is a series.
    assert scatter["options"]["series"] == [{}]


def test_driver_dashboard_compares_a_lap_with_a_reference_by_distance() -> None:
    dashboard = _dashboard("driver.json")
    panels = _panels(dashboard)
    variables = {item["name"]: item for item in dashboard["templating"]["list"]}

    assert list(variables) == ["vehicle", "session", "driver", "lap", "lap_ref"]
    assert dashboard["timepicker"] == {"hidden": True}
    # Laps are picked by identity and shown by their number in the session.
    for name in ("lap", "lap_ref"):
        query = variables[name]["query"]
        assert "lap_id::text AS __value" in query or "lap_id::text" in query
        assert "session_lap" in query and "lap_number" not in query
        assert variables[name]["sort"] == 0
    # The reference opens on the session's fastest clean lap.
    assert variables["lap_ref"]["query"].index("'best' AS __value") < variables["lap_ref"][
        "query"
    ].index("UNION ALL")
    for panel in panels:
        for target in panel["targets"]:
            sql = target["rawSql"]
            assert "$__time" not in sql, panel["title"]
            assert "'$lap'" in sql and "'$lap_ref'" in sql, panel["title"]
    # Grafana shares a crosshair only between panels on a time axis, so the
    # distance traces are time series whose "time" is distance: one metre is
    # one millisecond after the epoch, and the dashboard's fixed range is the
    # lap. Hovering one marks the same distance on the others, and dragging
    # across one zooms them all.
    assert dashboard["graphTooltip"] == 1
    assert dashboard["timezone"] == "utc"
    assert dashboard["time"]["from"] == "1970-01-01T00:00:00.000Z"
    traces = [panel for panel in panels if panel["type"] == "timeseries"]
    assert [panel["title"] for panel in traces] == [
        "Speed around the lap",
        "Time lost or gained against the reference",
        "Accelerator pedal",
        "Brake",
        "Gear",
    ]
    for panel in traces:
        sql = panel["targets"][0]["rawSql"]
        assert "timing.distance" in sql
        assert "to_timestamp(bin / 1000.0) AS time" in sql
        # The axis cannot say metres, so the hover does. A series hidden
        # from the plot is dropped from the hover too: this one is drawn,
        # invisibly, on a hidden axis of its own.
        distance = next(
            override["properties"]
            for override in panel["fieldConfig"]["overrides"]
            if override["matcher"]["options"] == "Distance (m)"
        )
        properties = {item["id"]: item["value"] for item in distance}
        assert properties["custom.hideFrom"]["viz"] is False
        assert properties["custom.lineWidth"] == 0
        assert properties["custom.axisPlacement"] == "hidden"
        assert panel["options"]["tooltip"]["mode"] == "multi"
    # The pedal is what the driver did; the throttle body is what the ECU did.
    pedal = next(panel for panel in traces if panel["title"] == "Accelerator pedal")
    assert "car.accel_pedal_pos" in pedal["targets"][0]["rawSql"]
    assert "car.throttle_pos" not in json.dumps(dashboard)
    gg = next(panel for panel in panels if panel["type"] == "xychart")
    # See the lap scatter: without it Grafana rewrites the options.
    assert float(gg["pluginVersion"].rsplit(".", 1)[0]) >= 11.1
    assert gg["options"]["series"] == [{}]
    corners = next(panel for panel in panels if panel["title"] == "Corner by corner")
    assert "v_track_sections" in corners["targets"][0]["rawSql"]
    # Sections are compared as fractions of the lap: the car's lap distance
    # and the track map's never agree to the metre.
    assert "start_fraction" in corners["targets"][0]["rawSql"]


def test_stint_report_covers_stops_driver_consistency_fuel_and_open_stops() -> None:
    dashboard = _dashboard("laps.json")
    stint_panels = [p for p in _panels(dashboard) if p["type"] != "row" and p["id"] >= 20]
    sql = "\n".join(p["targets"][0]["rawSql"] for p in stint_panels)
    text = json.dumps(stint_panels).lower()

    assert len(stint_panels) == 5
    assert "v_pit_stops" in sql
    assert "v_lap_fuel" in sql
    assert "median" in text
    assert "driving time" in text
    assert "open" in text
    for panel in stint_panels:
        panel_sql = panel["targets"][0]["rawSql"]
        # Every panel names who or which stop a row is about.
        assert '"Driver' in panel_sql or '"Stop"' in panel_sql, panel["title"]
        # Seconds as a Grafana unit render as decimal minutes past 60.
        assert panel["fieldConfig"]["defaults"].get("unit") != "s", panel["title"]
        if panel["type"] == "barchart":
            # A panel-wide unit also formats the text labels along the bars'
            # axis: a clock unit turned every stop's label into NaN.
            assert "unit" not in panel["fieldConfig"]["defaults"], panel["title"]
