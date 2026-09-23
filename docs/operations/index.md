# Deployment overview

openlaps has two independently managed stacks joined by one NATS leafnode.

| | Vehicle | Pit |
| --- | --- | --- |
| Primary config | `deploy/vehicle-compose.yaml` | `deploy/pit-compose.yaml` |
| Durable stream | `TELE` | `TELE_VEHICLE`, sourced from the vehicle |
| Main services | NATS, agent, optional go2rtc | NATS, TimescaleDB, MQTT, Grafana and ten application services |
| Operator surface | service logs and NATS monitoring | Grafana, session control and notifier |

## Deployment sequence

1. Choose a [vehicle target](../hardware/index.md).
2. Generate [NATS TLS material](nats-security.md) outside the repository.
3. Create separate `deploy/.env` files for vehicle and pit hosts.
4. Install and start the [vehicle](vehicle.md).
5. Install and start the [pit](pit.md).
6. Run every check in [Verify the stack](verification.md).
7. Before an event without internet, fill the [pit image registry](registry.md).

## Network direction

The pit dials the vehicle's leafnode listener at `:7422`. The vehicle must be
reachable from the pit radio network. Video, when enabled, is a separate direct
pit-to-vehicle connection on go2rtc ports.

## Configuration rules

- `OPENLAPS_NATS_URL` always names the local NATS server for that host.
- `TELE_VEHICLE` must not declare subjects; it receives data only by sourcing
  the vehicle's `TELE` stream.
- Pit consumers must explicitly name `TELE_VEHICLE` because a subject-less
  stream cannot be discovered from a subject.
- The vehicle JetStream domain in `deploy/nats/vehicle.conf` must match
  `OPENLAPS_VEHICLE_JS_DOMAIN` at the pit.
- CAN interfaces are configured on the host before the agent starts.

## Data and teardown

Named volumes hold pit data. The vehicle JetStream store lives at
`NATS_STORE_DIR`. A normal `docker compose down` keeps this data; adding `-v`
or deleting the vehicle store is destructive.
