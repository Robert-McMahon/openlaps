# Deployment overview

openlaps has two separately managed stacks joined by one NATS leafnode.

| | Vehicle | Pit |
| --- | --- | --- |
| Primary config | `deploy/vehicle-compose.yaml`, or systemd units on some targets | `deploy/pit-compose.yaml` |
| Durable stream | `TELE` | `TELE_VEHICLE`, sourced from the vehicle |
| Main services | NATS, agent, optional go2rtc, optional `openlaps-link` | NATS, TimescaleDB, MQTT, Grafana and ten application services, optional `link` |
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

The pit starts every connection, so the vehicle must be reachable from the
pit over at least one IP path.

| Port on the vehicle | Use | Needed |
| --- | --- | --- |
| TCP 7422 | NATS leafnode | Always, unless the link tunnel is used |
| UDP 7423 | Live frames | With `OPENLAPS_LIVE_SOURCE=direct`, unless they go through the tunnel |
| UDP 7424 | Link tunnel | With the `link` profile |
| go2rtc ports | Video | With the `video` profile |

The path can be a radio network (eg HaLow), an overlay network such as
Tailscale over HaLow and Starlink, or both. Where several addresses reach
the vehicle, list them: the tunnel and the live frames each accept more than
one. [Architecture](../ARCHITECTURE.md#the-vehicle-to-pit-link) describes how
each part chooses a path. `tools/car_link.sh` on the pit host shows the path
Tailscale is using and can force Starlink only.

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
