# Verify the stack

Run these checks after installation, upgrades and configuration changes.

## Container state

```bash
docker compose -f deploy/pit-compose.yaml ps
```

Long-lived services should be running and healthy. `migrate` and
`provision-streams` should have exited successfully.

## Leafnode and sourced stream

```bash
curl -fsS http://127.0.0.1:8222/leafz
nats --server nats://127.0.0.1:4222 stream report
```

The pit should report one leafnode. `TELE_VEHICLE` should exist, declare no
subjects, and advance toward the vehicle's `TELE` sequence.

## Application health

```bash
for port in 8080 8081 8082 8083 8084 8085 8086 8088 8089 8090; do
  printf '%s ' "$port"
  curl -fsS "http://127.0.0.1:$port/health" || true
  printf '\n'
done
curl -fsS http://127.0.0.1:8087/v1/health
curl -fsS http://127.0.0.1:3000/api/health
```

Port `8083` may be unavailable when NTRIP is intentionally disabled.

| Port | Service |
| ---: | --- |
| 8080 | session-control |
| 8081 | ingest-writer |
| 8082 | live-decoder |
| 8083 | ntrip-client |
| 8084 | timing-extrapolator |
| 8085 | pit-monitor |
| 8086 | notifier |
| 8087 | ntfy |
| 8088 | strategy |
| 8089 | timing-feed |
| 8090 | watch |

## Decode and storage

```bash
uv run tools/decode.py \
  --server nats://127.0.0.1:4222 \
  --stream TELE_VEHICLE --watch
```

```bash
docker compose -f deploy/pit-compose.yaml exec timescaledb \
  psql -U openlaps -d openlaps \
  -c "SELECT count(*), max(time) FROM v_samples_named;"
```

## Live and dashboards

```bash
mosquitto_sub -h 127.0.0.1 -p 1883 -t 'openlaps/#' -v
```

Confirm both Grafana data sources are healthy and dashboards show current data.
Provisioned dashboards query stable database views and use MQTT only for live
panels.

## Control path

Start a test session through session-control, then inspect the vehicle `CMD`
stream and a resulting lap event. This proves pit-to-vehicle control separately
from vehicle-to-pit telemetry.
