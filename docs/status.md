# Project status

The vehicle and pit pipelines are implemented and are described by the
checked-in Compose files and systemd units. This page lists what works, what
has not yet been validated, and what does not exist.

## Implemented

- Multi-CAN, serial GNSS and host collectors.
- Profile-driven channel catalog and vehicle hardware overlays.
- Vehicle lap timing, report-by-exception, batching and JetStream publishing.
- Sensor fusion of IMU and GNSS in the vehicle agent (`fused.*` channels).
- A compiled CAN ingest path (`rust/canfast`) alongside the Python one.
- TLS leafnode transport and a sourced pit stream with dropout recovery.
- Live frames: the vehicle sends the newest values of the channels a
  dashboard is showing, by UDP, separately from the stream.
- Link tunnel (`openlaps-link`): one QUIC connection that sends live frames
  ahead of the stream.
- Operation over more than one path (HaLow and Starlink through a Tailscale
  overlay network).
- TimescaleDB migrations, ingestion and stable query views.
- Live decoding, timing extrapolation and session control.
- Pit monitoring, watch models, strategy state and field timing ingestion.
- Grafana dashboards, alert ledger, annunciator, ntfy and Discord fan-out.
- Car camera streaming and recording, and remote ECU tuning over USB/IP.
- Radxa X4 and Luckfox Omni3576 vehicle targets.

## Outstanding validation

- Long-range and degraded-RF tests have not been checked in.
- Live frames and the link tunnel have been tested on the bench only, with
  a rate limit that adds no loss or jitter. They have not been run over a
  radio at a track.
- A cellular link has not been tested.
- Dropout testing under the commissioning conditions remains outstanding.
- On-car watch false-positive acceptance remains outstanding.
- The critical alert path has a 10-second Grafana evaluation floor; an
  under-10-second worst-case phone notification has not been demonstrated.

## Not implemented

- Race forecasting that combines strategy and the full field.

## Where each thing is defined

| Concern | Source |
| --- | --- |
| CLI entry points | `pyproject.toml` |
| Deployed services and ports | `deploy/*-compose.yaml` |
| Configuration examples | `example.env`, profile and target YAML |
| Wire schema | `proto/telemetry.proto` and `WIRE_FORMAT.md` |
| Database shape | `src/pit/db/migrations/` |
| Historical plans | `docs/plan/` (archive only) |

Update this page when a capability is added or validation evidence is
checked in. The historical plans do not describe the current status.
