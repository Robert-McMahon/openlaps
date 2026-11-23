# Vehicle installation

## 1. Choose a target

The base Compose defaults match the Radxa X4. Other boards may require a target
`.env` and Compose overlay. The Luckfox Omni3576 currently uses systemd plus
plain `docker run` on its shipped Docker 20.10; follow its dedicated guide.

- [Radxa X4](../hardware/radxa-x4.md)
- [Luckfox Omni3576](../hardware/luckfox-omni3576.md)

## 2. Build and configure

From the repository root:

```bash
docker build -f deploy/Dockerfile -t openlaps:local .
cp example.env deploy/.env
```

Or skip the build: point the vehicle's daemon at the
[pit image registry](registry.md) and set `OPENLAPS_IMAGE` to the pit's
`openlaps` image, which needs no internet on the car.

Edit `deploy/.env`. At minimum, set the TLS directory, leafnode password,
vehicle identifier and target-specific device paths. Keep comments on separate
lines when copying values: Compose treats text after some blank assignments as
a value.

## 3. Bring up CAN

```bash
sudo ./.venv/bin/python tools/can_up.py \
  --profile profiles/example-club-racer \
  --hardware deploy/targets/radxa-x4/hardware.yaml
```

The systemd `openlaps-can.service` performs the same privileged host action at
boot. The agent itself does not need `CAP_NET_ADMIN`.

### CAN stall watchdog

A CAN controller can wedge: the kernel keeps counting frames while delivering
none to any socket, and taking the link down to recover hangs the driver (seen
on the Luckfox Omni3576, 2026-11-08; a vendor driver bug, fixed by the kernel
patch under `deploy/targets/luckfox-omni3576/patches/`, so the watchdog is the
safety net rather than the cure). The agent reports that state as
`sys.agent.stall_s.<bus>` and the pit's `can-bus-stalled` alarm fires on it.
On the board, `deploy/systemd/openlaps-can-watchdog.service` runs
`tools/can_watchdog.py` as root: it watches every bus the profile names on a
socket of its own, never touches the interface, and reboots the box when the
stall persists for 10 s. Set `OPENLAPS_CAN_WATCHDOG_ACTION=reboot` in
`/etc/openlaps/agent.env` for a race weekend; the default `log` only records
the stall in the journal. Reboots are budgeted at three per hour, remembered
in `/var/lib/openlaps/can-watchdog-reboots.json`.

```bash
sudo ./.venv/bin/python tools/can_watchdog.py --dry-run   # what it would watch, and do
```

## 4. Start the stack

Radxa X4:

```bash
docker compose -f deploy/vehicle-compose.yaml up -d
```

Enable the optional camera:

```bash
docker compose -f deploy/vehicle-compose.yaml --profile video up -d
```

For a target with an overlay, add its Compose file with another `-f` argument.

## 5. Check local services

```bash
curl -fsS http://127.0.0.1:8222/healthz
nats --server nats://127.0.0.1:4222 stream ls
```

Expect `TELE` and `CMD`. Continue with [stack verification](verification.md)
after the pit is running.

## Live frames (optional)

Live frames let the pit's gauges stay current when the stream is behind. Set
these in the agent's environment (`deploy/.env` for Compose,
`/etc/openlaps/agent.env` for systemd) and restart the agent:

```text
OPENLAPS_LIVE_PORT=7423
OPENLAPS_LIVE_KEY=<openssl rand -hex 32>
```

The agent sends nothing until a pit asks. The key signs every datagram and
must equal the pit's `OPENLAPS_LIVE_KEY`. Without a key the link works
unsigned and logs a warning.

## Link tunnel (optional)

The tunnel carries live frames ahead of the stream in one QUIC connection.
The vehicle end is a systemd service on every target. `tools/deploy_vehicle.sh`
builds the `openlaps-link` binary and installs it in `/opt/openlaps/bin`.
Install the unit once:

```bash
sudo install -m 0644 deploy/systemd/openlaps-link.service /etc/systemd/system/
sudo install -d -o openlaps -g openlaps -m 0700 /etc/openlaps/tls
sudo install -o openlaps -g openlaps -m 0600 \
  <tls dir>/{server-cert,server-key,ca}.pem /etc/openlaps/tls/
sudo systemctl daemon-reload && sudo systemctl enable --now openlaps-link
```

It uses the leafnode's server certificate and listens on UDP 7424. The
vehicle's `deploy/nats/vehicle.conf` already announces `127.0.0.1:7422` to
the pit, which keeps the leafnode inside the tunnel when it reconnects. The
pit side is described in [Pit installation](pit.md).

## Direct systemd deployment

`deploy/systemd/openlaps-agent.service` runs the agent from `/opt/openlaps`.
Set `OPENLAPS_PROFILE`, `OPENLAPS_HARDWARE`, `OPENLAPS_NATS_URL` and the state
directory in `/etc/openlaps/agent.env`. Run either systemd or the containerized
agent, never both.
