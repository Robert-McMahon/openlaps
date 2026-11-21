# Dashboard maintenance

Grafana dashboards in `deploy/pit-config/grafana/dashboards/` are provisioned
read-only. The repository is the source of truth; a browser edit is temporary.

## The dashboards

| File | Title | For |
| --- | --- | --- |
| `pitwall.json` | Pit wall | The one race screen: lap and sector timing, fuel and pit window, four engine vitals, map, standings |
| `car.json` | Car: live | Live gauges and lap context over MQTT |
| `car-traces.json` | Car: traces | Car and engine history; one-second aggregate or raw samples |
| `reliability.json` | Reliability | Slow trends: oil pressure per 1000 rpm, temperatures, knock, PDM currents |
| `fuel.json` | Strategy | Fuel, laps to dry, pit window and stop plan from the strategy service |
| `laps.json` | Session report | One session: laps, driver pace, pit stops, stints, driving time |
| `driver.json` | Driver | Coaching: one lap against a reference lap by distance (speed, time lost, throttle, brake, gear, g-g) and corner by corner |
| `system.json` | System status | GNSS, clocks, agent publish path, ingest, NATS, hosts |

## Change a dashboard

1. Edit and test the dashboard in Grafana.
2. Export its JSON model without the external-sharing transformation.
3. Preserve the dashboard `uid`, remove the installation-specific `id`, and
   replace the matching JSON file in the repository.
4. Review the diff and run the dashboard tests.
5. Restart Grafana or wait for the provider rescan.

```bash
uv run pytest tests/test_grafana_dashboards.py
docker compose -f deploy/pit-compose.yaml restart grafana
```

## Contracts

- Use datasource UID `timescale` for SQL and `mqtt-live` for live values.
- SQL panels query stable views, never base tables.
- Numeric MQTT payloads retain `value`; display-only timing panels may use the
  publisher's race-formatted `display` field.
- A dashboard carries only the pickers its panels read, and takes them from
  the shared definitions (`car-traces.json` has the session picker, `laps.json` the
  driver and stint pickers). The session picker lists every session with
  laps, newest first, and is not bound to the time range.
- The session report (`laps.json`) is session-scoped: every
  panel filters on `$session`, none reads the time range, and the time picker
  is hidden. It reads `v_session_laps`, whose `session_lap` counts laps in
  crossing order; `lap_number` restarts with the vehicle agent and is not
  shown. `clean` laps (valid, timed, no pit entry or exit inside) are the
  ones pace statistics use.
- The driver dashboard (`driver.json`) is session-scoped too. It compares
  the `$lap` and `$lap_ref` laps on a 100 ms grid binned to 10 m of
  `timing.distance`; channels the car sends only on change (gear, brake
  switch) are carried forward. Its corner table needs the track's sections
  in `v_track_sections`: see "Lap sections" in `docs/TRACK_SETUP.md`.
- An XY chart panel must carry `"pluginVersion"` (11.1 or later). Without it
  Grafana takes the options for the old XY chart format and rewrites them on
  load, drawing "Err" or one series. Put units and axis limits in overrides
  on the plotted fields: panel-wide they also format the x field.
- Show lap and stop times as `m:ss` text or with a `time:` unit on a
  millisecond value. Grafana's seconds unit renders decimal minutes.
- The car camera is a link, not a panel: `Car camera` on every dashboard
  opens go2rtc's player through the pit's video relay (`:1984`). Video does
  not pass through Grafana, NATS or TimescaleDB.
- Every dashboard carries the same three links: the other `openlaps`-tagged
  dashboards, session control, and the car camera.

## Units and axes

Use units from the channel catalog rather than assuming them. The example
profile contains signals from sources with different temperature conventions.
IMU axis orientation is a vehicle installation assumption and must be checked
on the car before relying on g-g plots.
