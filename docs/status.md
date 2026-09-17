# Project status

The vehicle and pit pipelines are implemented and represented in the checked-in
Compose files. This page separates current capability from historical phase
plans.

## Implemented

- Multi-CAN, serial GNSS and host collectors.
- Profile-driven channel catalog and vehicle hardware overlays.
- Vehicle lap timing, report-by-exception, batching and JetStream publishing.
- TLS leafnode transport and a sourced pit stream with dropout recovery.
- TimescaleDB migrations, ingestion and stable query views.
- Live decoding, timing extrapolation and session control.
- Pit monitoring, watch models, strategy state and field timing ingestion.
- Grafana dashboards, alert ledger, annunciator, ntfy and Discord fan-out.
- Radxa X4 and Luckfox Omni3576 vehicle targets.

## Outstanding validation

- Long-range and degraded-RF tests have not been checked in.
- Dropout testing under the commissioning conditions remains outstanding.
- On-car watch false-positive acceptance remains outstanding.
- The critical alert path has a 10-second Grafana evaluation floor; an
  under-10-second worst-case phone notification has not been demonstrated.

## Not implemented

- Race forecasting that combines strategy and the full field.

## Sources of truth

| Concern | Source |
| --- | --- |
| CLI entry points | `pyproject.toml` |
| Deployed services and ports | `deploy/*-compose.yaml` |
| Configuration examples | `example.env`, profile and target YAML |
| Wire schema | `proto/telemetry.proto` and `WIRE_FORMAT.md` |
| Database shape | `src/pit/db/migrations/` |
| Historical intent | `docs/plan/` — archive only |

Update this page when capability lands or validation evidence is checked in.
Do not infer current status from phase plans.
