# Pit installation

## 1. Prepare the host

Install Docker with the Compose plugin and chrony. Install the checked-in pit
chrony configuration before the application stack. The config path and unit
name depend on the distro.

Debian and Ubuntu:

```bash
sudo install -m 0644 deploy/chrony/pit.conf /etc/chrony/chrony.conf
sudo systemctl restart chrony
chronyc sources -v
```

Arch (including Omarchy):

```bash
sudo install -m 0644 deploy/chrony/pit.conf /etc/chrony.conf
sudo systemctl restart chronyd
chronyc sources -v
```

The vehicle source should appear with `^*` or `^+` once it has been polled a
few times. A persistent `^?` means the vehicle's chronyd is not reachable or
does not allow this subnet.

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

The provisioner also reads the vehicle stream's age cap over the leafnode and
prints a `WARNING` when the pit's `OPENLAPS_PIT_STREAM_MAX_AGE_H` is shorter
than the vehicle's `OPENLAPS_TELE_MAX_AGE_H`. Keep them equal: an empty pit
stream resumes sourcing from the vehicle's oldest message, so a shorter pit cap
turns a pit outage into a re-ingest of data Timescale already holds. Changing
the value takes effect on the next `up`; the provisioner converges the
existing stream.

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
