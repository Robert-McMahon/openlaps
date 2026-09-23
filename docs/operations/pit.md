# Pit installation

## 1. Prepare the host

Install Docker with the Compose plugin and chrony. Install the checked-in pit
chrony configuration before the application stack:

```bash
sudo install -m 0644 deploy/chrony/pit.conf /etc/chrony/chrony.conf
sudo systemctl restart chrony
chronyc sources -v
```

## 2. Configure

```bash
cp example.env deploy/.env
```

Fill the required values, including:

- `NATS_TLS_DIR` and `NATS_LEAFNODE_URL`;
- `TIMESCALE_PASSWORD`;
- `GRAFANA_ADMIN_PASSWORD` and a distinct `GRAFANA_DB_PASSWORD`;
- `OPENLAPS_SESSION_API_KEY`;
- the vehicle identifier and public pit URLs.

The Compose file reads `deploy/.env`. Never commit it.

If RTK is not used, stop the NTRIP service after startup. Its host and
mountpoint are intentionally required, so an unconfigured service fails loudly:

```bash
docker compose -f deploy/pit-compose.yaml stop ntrip-client
```

## 3. Start

```bash
docker compose -f deploy/pit-compose.yaml up -d
```

Compose waits for TimescaleDB and NATS, runs the database migrations and stream
provisioner once, then starts their dependants. The first Grafana start needs
internet to download pinned plugins; later starts use the named volume.

For an event with no uplink, start the [pit image registry](registry.md) and
fill it while the pit is still online; every image the stack needs, and the
openlaps image the vehicles pull, then comes from the pit.

## 4. Operator surfaces

| Surface | URL |
| --- | --- |
| Grafana | `http://<pit-host>:3000` |
| Session control | `http://<pit-host>:8080` |
| Notifier/annunciator | `http://<pit-host>:8086` |
| ntfy | `http://<pit-host>:8087` |

Proceed to [Verify the stack](verification.md) before using it at an event.
