# Dashboard maintenance

Grafana dashboards in `deploy/pit-config/grafana/dashboards/` are provisioned
read-only. The repository is the source of truth; a browser edit is temporary.

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
- Keep shared relational template variables aligned with `car.json`.
- The video dashboard connects directly from the browser to vehicle go2rtc;
  video does not pass through NATS or TimescaleDB.

## Units and axes

Use units from the channel catalog rather than assuming them. The example
profile contains signals from sources with different temperature conventions.
IMU axis orientation is a vehicle installation assumption and must be checked
on the car before relying on g-g plots.
