# Dashboards live here, not in the browser

Every JSON file in this directory is provisioned into Grafana's `openlaps`
folder as read-only. **These files are the source of truth and the browser
is not.** The provider
(`../provisioning/dashboards/openlaps.yaml`) sets `allowUiUpdates: false`
and `disableDeletion: true` deliberately: a panel changed in the UI cannot
be saved back, so a panel changed in the UI and not committed does not
exist.

That is not administrative tidiness. The predecessor stack allowed UI
updates, and its eight dashboards ended up defined by whatever the running
Grafana happened to hold — unreviewable, undiffable, and gone with the
volume.

## Changing a dashboard

1. Open it in Grafana, edit until it is right.
2. **Dashboard settings → JSON Model**, or the share/export menu with
   *Export for sharing externally* off, and copy the JSON.
3. Paste it into the file here, commit it, and review the diff.
4. `docker compose -f deploy/pit-compose.yaml restart grafana` — or wait for
   the provider's rescan.

Keep the exported `uid` stable across edits; it is what a link to the
dashboard resolves against. Strip the `id` field (Grafana assigns it per
install) and leave `version` alone.

## What panels may read

- Datasource references are by uid: `timescale` for SQL,
  `mqtt-live` for the live gauge feed. Never by name, and never the
  "default" datasource.
- SQL panels read the **views** — `v_samples_named`, `v_laps`, and whatever
  `docs/PIT_SCHEMA.md` lists after them. Grafana's database role has
  `SELECT` on those and nothing else, so a panel querying `samples`
  directly does not fail review, it fails at runtime.
- Units are physical and the catalog says which. `docs/CATALOG.md` keeps the
  donor car's real inconsistency: the Haltech ECU reports temperatures in
  **Kelvin**, the FDI IMU reports its board temperature in Celsius. A panel
  showing `car.coolant_temp` with no unit set reads ~370 and looks entirely
  plausible. Read `units` from the view; never assume.

## Copyable relational variable block

`car.json` is the canonical source for the shared relational template
variables. New SQL dashboards should copy its `vehicle`, `session`, `driver`,
`stint`, and `lap` entries together so their chaining does not drift:

- `session` is scoped by `vehicle` and the selected dashboard time range;
- `driver` is scoped by `vehicle`, `session`, and time range;
- `stint` is scoped by `session` and uses `stint_number` as its value;
- `lap` is scoped by `vehicle`, `session`, `driver`, and time range, uses
  `lap_id` as its value, and labels each option with lap number, driver, and
  crossing time.

All five queries use the stable views rather than base tables. Keep the
variable names unchanged: panel SQL and Grafana's chained-variable refresh
behaviour depend on them.
