# Pit image registry

The pit runs a Docker registry that holds every image the complete stack
runs from, so the stack starts and restarts with no internet, and a vehicle
pulls the openlaps image from the pit rather than building it. It is a
plain private registry (`deploy/registry-compose.yaml`) used two ways:

- **As a Docker Hub mirror.** Each host's daemon lists it under
  `registry-mirrors`. A compose file still says `nats:2.12-alpine`; the
  daemon asks the pit first and falls through to Docker Hub only on a miss.
  No `image:` line names the registry, so nothing changes between an online
  and an offline deployment.
- **As the home of the openlaps image.** The pit builds it and pushes it as
  `openlaps:<git describe>` and `openlaps:latest`. Vehicles pull
  `<pit>:5000/openlaps:<tag>` via `OPENLAPS_IMAGE` in their `deploy/.env`.
  Building on a car needs PyPI and ghcr.io; pulling from the pit needs
  neither.

It is deliberately not a pull-through cache. A pull-through cache refuses
pushes, so the openlaps image would need a second registry, and it expires
content on a timer, which is exactly what an offline pit cannot afford. The
cost is one explicit fill step before leaving internet behind.

## 1. Start it on the pit

```bash
docker compose -f deploy/registry-compose.yaml up -d
curl -s http://localhost:5000/v2/   # {}
```

It is its own compose project, not part of `pit-compose.yaml`: it has to be
up before the pit stack pulls, it survives `docker compose -f
deploy/pit-compose.yaml down`, and it needs none of that file's secrets.
`OPENLAPS_REGISTRY_PORT` in `deploy/.env` moves it off 5000.

## 2. Fill it, while online

```bash
uv run tools/registry_sync.py            # or --dry-run first
```

The list of images is not kept anywhere: the tool asks `docker compose
config --images` for the pit stack and for the vehicle stack once per
target under `deploy/targets/`, so a bumped tag or a new service is mirrored
on the next run. Third-party images are copied registry-to-registry with
every architecture in their manifest list, which is what lets the arm64
Luckfox and the amd64 Radxa pull the same `nats:2.12-alpine` from the same
mirror. Images the pit's daemon already holds are copied at the digest the
pit runs; re-running skips whatever the registry already has.

The openlaps image is rebuilt from the checkout first (`--no-build` pushes
the existing `openlaps:local` instead) and is amd64 only, which covers every
board that runs the containerised agent. `--no-openlaps` mirrors the
third-party images alone.

Run it after every `docker build` you want a vehicle to receive, and once
more as the last online step before an event.

## 3. Point the daemons at it

Every host that should pull from the pit needs both entries in
`/etc/docker/daemon.json`, then a daemon reload. Both settings are
reloadable; `systemctl reload docker` applies them without restarting a
single container.

Vehicle (the pit is `192.168.12.203` on the car LAN):

```json
{
  "insecure-registries": ["192.168.12.203:5000"],
  "registry-mirrors": ["http://192.168.12.203:5000"]
}
```

```bash
sudo systemctl reload docker
docker info --format '{{json .RegistryConfig.Mirrors}}'   # ["http://192.168.12.203:5000/"]
```

Pit, so that it can re-pull its own images from itself after a prune or a
Docker reinstall (loopback needs no `insecure-registries` entry):

```json
{
  "registry-mirrors": ["http://localhost:5000"]
}
```

Merge into the existing file rather than replacing it; the pit's already
sets a log driver and the bridge address.

## 4. Use it from a vehicle

Set the image the compose stack runs in the vehicle's `deploy/.env`:

```text
OPENLAPS_IMAGE=192.168.12.203:5000/openlaps:latest
```

Then pull and start as usual:

```bash
docker compose -f deploy/vehicle-compose.yaml pull
docker compose -f deploy/vehicle-compose.yaml up -d
```

`nats` and `go2rtc` arrive through the mirror; `agent` arrives by name.
`latest` moves with every sync, so `pull` is what rolls a car forward; pin
`openlaps:<git describe>` from the sync's output when a car must not move.
The Luckfox's plain `docker run` lines need no change: the mirror setting
covers them too.

## Checking it

```bash
curl -s http://192.168.12.203:5000/v2/_catalog
curl -s http://192.168.12.203:5000/v2/library/nats/tags/list
docker compose -f deploy/registry-compose.yaml logs -f   # one GET per pull
```

A pull that reaches Docker Hub instead of the pit shows up as no line in
those logs. The usual cause is a daemon that was edited but not reloaded.

## Limits

- **No authentication, plain HTTP.** Anything on the LAN can push. That is
  the same trust boundary as the stack's other published ports; put TLS
  and `htpasswd` in front of it before it faces anything wider.
- **Only Docker Hub names are mirrored.** The stack's runtime images are all
  on Docker Hub, and the sync refuses anything that is not, rather than
  skipping it. The build-time `ghcr.io/astral-sh/uv` and PyPI are why the
  openlaps image is built at the pit, online.
- **Grafana's plugin** is still fetched on the first start into its named
  volume, as the [cutover runbook](../CUTOVER_RUNBOOK.md) says; the registry
  does not change that. Warm the volume online.
- **The registry image itself** lives in the pit daemon's cache. `docker
  save registry:3.1.1 -o registry.tar` somewhere safe covers a pit that has
  to reinstall Docker with no uplink.
- **Old openlaps tags accumulate.** With `REGISTRY_STORAGE_DELETE_ENABLED`
  on, `docker compose -f deploy/registry-compose.yaml exec registry
  registry garbage-collect --delete-untagged /etc/distribution/config.yml`
  reclaims layers no tag references. Untagging is a manifest `DELETE`
  against the API; it is rarely worth the trouble.
