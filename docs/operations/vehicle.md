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

## Direct systemd deployment

`deploy/systemd/openlaps-agent.service` runs the agent from `/opt/openlaps`.
Set `OPENLAPS_PROFILE`, `OPENLAPS_HARDWARE`, `OPENLAPS_NATS_URL` and the state
directory in `/etc/openlaps/agent.env`. Run either systemd or the containerized
agent, never both.
