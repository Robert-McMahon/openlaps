# Deploying openlaps

Two stacks, one link between them. The vehicle stack runs on the car's SBC
and owns the data; the pit stack runs in the garage and consumes it. They
meet at a single NATS leafnode connection, and everything else follows from
that.

| | Vehicle | Pit |
| --- | --- | --- |
| Compose file | `vehicle-compose.yaml` (+ the target's overlay) | `pit-compose.yaml` |
| NATS config | `nats/vehicle.conf` | `nats/pit.conf` |
| JetStream domain | `veh` | `pit` |
| Streams | `TELE`, `CMD` (created by the agent) | `TELE_VEHICLE` (created by `provision_pit_streams.py`) |
| Services | agent, go2rtc (`--profile video`) | ingest-writer, live-decoder, timing-extrapolator, session-control, ntrip-client |
| Read surface | — | Grafana on `:3000` |

## Which SBC the vehicle stack runs on

One variable, `OPENLAPS_TARGET`, resolved against `targets/`. A **target** is
a board: the device nodes it exposes, the host wiring the agent needs
(`hardware.yaml`, ADR 0010), and the encoder pipeline its silicon can
actually run (`go2rtc.yaml`). Two ship — `radxa-x4` and `luckfox-omni3576` —
and `targets/README.md` is how to use one or add another.

The profile is never edited to move between boards. A profile describes the
car; a target describes what the car is bolted to.

### Upgrading an existing vehicle stack to targets

Two coupled changes, and a containerised car needs both applied together.

The compose stack used to map the host's GNSS device to `/dev/ttyUSB0` inside
the container so that the profile's `port: /dev/ttyUSB0` would find it. It now
maps the device **at its own path** and tells the agent that path through the
target's `hardware.yaml`, because a rewrite and an overlay cannot both be the
answer. So on the car:

1. Set `OPENLAPS_TARGET` in `deploy/.env` (`radxa-x4` is the default if you
   set nothing, and matches what the compose file did before).
2. Check that `OPENLAPS_SERIAL_DEVICE` and the target's `serial0.port` name
   the device the car actually has — `/dev/ttyACM0` for the RP2040 relay
   (ADR 0009), which is what `example.env` has always documented. If your car
   is on something else, that is a one-line edit in `hardware.yaml` and the
   matching line in `.env`.

A stack running the agent **directly** (systemd, not compose) is unaffected
until you opt in: `OPENLAPS_HARDWARE` is unset in `/etc/openlaps/agent.env`
unless you put it there, and an unset overlay means the profile is used
exactly as written.

**The pit dials the vehicle**, never the reverse. The vehicle runs a
leafnode *listener* on `:7422`; the pit config carries the `remotes:` entry.
That direction is load-bearing: `docs/ARCHITECTURE.md` requires that the car
need no inbound reachability from the pit LAN, which is what lets it sit
behind NAT or a WSL2 host.

The leafnode is the only thing that crosses **for telemetry and control**.
Video is the one deliberate exception — a second, independent crossing in
the same pit-dials-vehicle direction; see "The car camera" below.

## `OPENLAPS_NATS_URL` means different things in the two stacks

It is always **the local server for this stack** — the vehicle's server in
`vehicle-compose.yaml`, the pit's in `pit-compose.yaml`. Same variable name,
two stacks, one `.env` each. Nothing at the pit ever points at the car
directly; the leafnode is the only thing that crosses.

That includes session-control, which publishes *into the vehicle's
JetStream domain* — it does so by naming the domain
(`nc.jetstream(domain="veh")`) on a connection to its own local server, not
by connecting to the car.

## The sourced stream, and the one thing to get right about it

The pit's `TELE_VEHICLE` sources the vehicle's `TELE` across the leafnode
and **declares no subjects of its own**. That is a measurement, not a
preference: the leafnode propagates `tele.<vehicle>.>` to the pit server as
ordinary core NATS, so a pit stream that also declared that subject would
capture each message directly *and* again through sourcing — measured at
exactly 2×.

The consequence lands on every pit consumer. `nats-py` resolves a stream
from a subject by asking the server which streams declare it; a subject-less
stream matches nothing, so a bare `js.subscribe("tele.<vehicle>.>")` raises
`NotFoundError` at the pit. **Every pit service must name the stream**, and
each takes it as configuration:

| Service | Variable | Default |
| --- | --- | --- |
| ingest-writer | `OPENLAPS_INGEST_STREAM` | `TELE_VEHICLE` |
| live-decoder | `OPENLAPS_LIVE_STREAM` | `TELE_VEHICLE` |
| timing-extrapolator | `OPENLAPS_TIMING_STREAM` | `TELE_VEHICLE` |
| ntrip-client (GGA feed only) | `OPENLAPS_NTRIP_STREAM` | `TELE_VEHICLE` |

All four must name the stream `provision_pit_streams.py` actually created.
A service pointed at a stream that does not exist fails loudly at startup,
which is the desired outcome — the alternative is a service that looks
healthy and silently receives nothing.

`tools/decode.py` needs the same: `--stream TELE_VEHICLE` against the pit,
and nothing against the vehicle, whose `TELE` does declare its subjects.

## The domain string is coupled across two files

`jetstream { domain: veh }` in `nats/vehicle.conf` must equal
`OPENLAPS_VEHICLE_JS_DOMAIN` in the pit's `.env` (default `veh`), and both
must match the `$JS.veh.API` the pit stream sources through. A mismatch is
a *silent* failure: the publish goes to a domain that does not exist and
times out, with nothing wrong-looking in either server's log.

## Secrets

None of it is in this repository, and none of it should be.

- **TLS material and the leafnode password** — generated per deployment,
  mounted at `/etc/nats/tls`. `nats/README.md` has the full sequence. The
  vehicle takes `NATS_LEAF_PASSWORD`; the pit takes the whole remote URL as
  `NATS_LEAFNODE_URL` (`tls://leaf:<password>@<vehicle-host>:7422`), because
  `nats-server` expands `$VAR` only as a complete token and cannot assemble
  a URL from parts.
- **`OPENLAPS_SESSION_API_KEY` is mandatory at the pit.** session-control
  binds `OPENLAPS_SESSION_HOST`, and a container has to bind `0.0.0.0` to be
  reachable at all — but `SessionControlSettings.from_env` *refuses to
  start* on a non-loopback bind without an API key. This is not optional and
  not something to discover at first bring-up; generate one
  (`openssl rand -base64 32`) before you run `up`.
- **`TIMESCALE_PASSWORD`**, and **`NTRIP_USER`/`NTRIP_PASSWORD`** if you are
  running RTK. NTRIP credentials live only at the pit and never reach the
  vehicle (ADR 0006).
- **`GRAFANA_ADMIN_PASSWORD` and `GRAFANA_DB_PASSWORD`**, both mandatory —
  the pit stack refuses to start without them. The second is *not*
  `TIMESCALE_PASSWORD`: Grafana connects to the database as a read-only role
  (`GRAFANA_DB_USER`, default `grafana_ro`) that holds `SELECT` on the views
  and nothing else. The grafana container is given an explicit list of
  environment variables rather than the pit's `.env`, so it cannot see the
  owner credential even by accident.

## Bring-up

Build the shared image once per stack. All five entry points live in it —
four services plus the one-shot migrator — because they share every
dependency they have.

```bash
docker build -f deploy/Dockerfile -t openlaps:local .
```

### 1. Vehicle

Pick the target and fill in its deploy variables, once:

```bash
cat deploy/targets/luckfox-omni3576/target.env >> deploy/.env
```

Bring the CAN interfaces up on the host. The agent configures no bitrate and
brings no link up; that is host state. `can_up.py` reads the same profile and
target overlay the agent reads, so the bitrate lives in one place:

```bash
sudo ./.venv/bin/python tools/can_up.py --profile profiles/example-club-racer \
  --hardware deploy/targets/luckfox-omni3576/hardware.yaml
```

(The venv's interpreter, not `uv run`: this runs as root, and `sudo uv run`
resolves against root's environment rather than yours.)

`deploy/systemd/openlaps-can.service` is the same thing at boot, as root,
ordered before the agent — which is where it has to run, because configuring
an interface needs `CAP_NET_ADMIN` and the agent must not have it.

```bash
docker compose -f deploy/vehicle-compose.yaml \
  -f deploy/targets/luckfox-omni3576/compose.yaml up -d
```

Drop the second `-f` for a target with no compose overlay; `radxa-x4` has
none, which is why the bare `docker compose -f deploy/vehicle-compose.yaml
up -d` still works there.

Confirm the agent created its streams. Both should be listed, `TELE` with
subject `tele.<vehicle>.>`:

```bash
nats --server nats://127.0.0.1:4222 stream ls
```

#### GNSS timing head and clock discipline

The Radxa X4 cannot capture PPS on the N100 directly. P4.8 therefore flashes
the onboard RP2040 as a timing head. This **replaces Radxa's stock GPIO
firmware**. The complete firmware build and flash procedure is in
`firmware/timing-head/README.md`.

Default external wiring is UM980 PPS (active high) to RP2040 GPIO2, UM980
COM2 TX to RP2040 GPIO5/UART1 RX, and a shared ground. On X4 v1.110 these are
header pins 38 and 11 respectively; on older X4 boards they are pins 22 and
6. The UART build uses the X4's internal RP2040 UART0 link and appears on the
N100 as `/dev/ttyS4` — there is no external host-UART wire. The USB build
appears as `/dev/ttyACM0`. All external signals are 3.3 V TTL. Do not select
between transports on an assumed jitter advantage; run the one-hour
comparison in the bench runbook.

Install `chrony`, then install `deploy/chrony/vehicle.conf` on the SBC,
`deploy/chrony/pit.conf` at the pit, and the shim unit:

On the Ubuntu vehicle image, the chrony systemd drop-in starts chronyd with the
`openlaps` GID, making its refclock socket `root:openlaps` with mode `0660`, and
gives that group read/traverse access to the runtime directory. This lets the
unprivileged shim submit samples without a privileged post-creation
`chown`/`chmod` race or world-writable clock input.

```bash
sudo apt-get install chrony python3-serial
getent group openlaps >/dev/null || sudo groupadd --system openlaps
id -u openlaps >/dev/null 2>&1 || \
  sudo useradd --system --gid openlaps --home-dir /opt/openlaps --shell /usr/sbin/nologin openlaps
sudo install -m 0644 deploy/chrony/vehicle.conf /etc/chrony/chrony.conf
sudo install -D -o root -g root -m 0755 tools/timing_head_shim.py \
  /usr/local/libexec/openlaps/timing_head_shim.py
# chrony's SOCK wire format lives beside it, shared with the GPIO PPS shim.
sudo install -D -o root -g root -m 0644 tools/chrony_sock.py \
  /usr/local/libexec/openlaps/chrony_sock.py
sudo install -m 0644 deploy/systemd/timing-head-shim.service /etc/systemd/system/
sudo install -d /etc/systemd/system/chrony.service.d
sudo install -m 0644 deploy/systemd/chrony-openlaps-sock.conf \
  /etc/systemd/system/chrony.service.d/openlaps-sock.conf
sudo install -d /etc/openlaps
printf '%s\n' 'TIMING_HEAD_ARGS=--device /dev/ttyACM0 --chrony-socket /run/chrony/openlaps-timing.sock' \
  | sudo tee /etc/openlaps/timing-head.env
sudo systemctl daemon-reload
sudo systemctl restart chrony
sudo systemctl enable --now timing-head-shim
chronyc sources -v
```

The `GPS` SOCK refclock is preferred and internet NTP remains fallback.
Pulling PPS must make chrony select NTP by slew; restoring it must reselect
GPS, also without a step. The host collector publishes that state as
`sys.host.clock_*` telemetry.

### 2. Pit

Install the pit clock configuration before starting the application stack:

```bash
sudo apt-get install chrony
sudo install -m 0644 deploy/chrony/pit.conf /etc/chrony/chrony.conf
sudo systemctl restart chrony
chronyc sources -v
```

```bash
docker compose -f deploy/pit-compose.yaml up -d
```

`depends_on` sequences the rest, so this is one command rather than a
runbook:

1. `timescaledb` becomes healthy.
2. **`migrate` runs to completion.** Migrations are a bring-up *step*, not a
   service — `openlaps-migrate` applies plain SQL files inside a transaction,
   records applied versions, and serialises on an advisory lock, so it is
   safe to re-run and exits 0 with nothing to do. Every DB-touching service
   declares `depends_on: condition: service_completed_successfully` against
   it. Modelling it as an entrypoint line in each service instead would put
   four containers in a race over the same DDL on every restart.
3. `nats` becomes healthy and connects its leafnode.
4. **`provision-streams` runs to completion**, creating or converging
   `TELE_VEHICLE`.
5. The five services start, and Grafana with them.

**The first bring-up needs internet.** Grafana installs its pinned plugins
— `grafana-mqtt-datasource` (the live gauge feed), `grafana-clock-panel`
(the count-up clocks) and `innius-video-panel` (the car camera) — before it
finishes starting. They land in the `grafana-data` volume and are not
fetched again, so a pit that will be offline at the track only has to have
been online once. A fresh volume on a disconnected pit is a Grafana that
does not come up.

### 3. Verify each hop

**The leafnode is up.** `leafs` should be 1 at both ends:

```bash
curl -s http://127.0.0.1:8222/leafz | head -30
```

**Sourcing is flowing.** `TELE_VEHICLE` should exist, declare no subjects,
and its message count should be climbing toward the vehicle's `TELE`:

```bash
nats --server nats://127.0.0.1:4222 stream report
```

**Batches decode at the pit.** This is the end of the NATS half — registry
resolution, `scale`/`offset`, real channel names:

```bash
uv run tools/decode.py --server nats://127.0.0.1:4222 --stream TELE_VEHICLE --watch
```

**The MQTT bridge is publishing.** One topic per channel, JSON payloads:

```bash
mosquitto_sub -h 127.0.0.1 -p 1883 -t 'openlaps/#' -v
```

**Rows are landing.** Read the *views*, not the base tables — they are the
stable surface (`docs/PIT_SCHEMA.md`):

```bash
docker compose -f deploy/pit-compose.yaml exec timescaledb psql -U openlaps -d openlaps -c "SELECT count(*), max(time) FROM v_samples_named;"
```

**Every service is healthy.** Ports are fixed defaults, one per service:

| Service | Port | Variable |
| --- | --- | --- |
| session-control | 8080 | `OPENLAPS_SESSION_PORT` (operator API *and* `/health`) |
| ingest-writer | 8081 | `OPENLAPS_INGEST_HEALTH_PORT` |
| live-decoder | 8082 | `OPENLAPS_LIVE_HEALTH_PORT` |
| ntrip-client | 8083 | `OPENLAPS_NTRIP_HEALTH_PORT` |
| timing-extrapolator | 8084 | `OPENLAPS_TIMING_HEALTH_PORT` |
| grafana | 3000 | fixed (`GET /api/health`) |

```bash
for port in 8080 8081 8082 8083 8084; do echo "--- $port"; curl -fsS "http://127.0.0.1:$port/health"; echo; done
```

**Grafana is up and both datasources are green.** The web UI is on
`:3000`; these are the two things worth checking without it. `Database
Connection OK` means the read-only role exists and the views are readable;
`MQTT Connected` means the websockets listener answered.

```bash
curl -fsS http://127.0.0.1:3000/api/health
```

```bash
for uid in timescale mqtt-live; do echo "--- $uid"; curl -fsS -u "admin:$GRAFANA_ADMIN_PASSWORD" "http://127.0.0.1:3000/api/datasources/uid/$uid/health"; echo; done
```

**Control reaches the car.** Start a session at the pit and confirm the
agent stamps it onto lap events:

```bash
curl -fsS -X POST -H "Authorization: Bearer $OPENLAPS_SESSION_API_KEY" -H 'Content-Type: application/json' -d '{"session_type":"practice","driver":"Driver A"}' http://127.0.0.1:8080/session/start
```

```bash
nats --server nats://<vehicle-host>:4222 stream view CMD
```

## Bringing it up without a car

`tools/replay.py` (P3.7) drives the **real** collectors and the **real**
agent pipeline from recorded inputs into a real JetStream, so a bench
bring-up needs no vehicle and no hardware:

```bash
uv run tools/replay.py --server nats://127.0.0.1:4222 --rate 1.0 --loop
```

The cycle's sources share one clock, so a CAN capture shorter than the GPS
trace leaves every CAN-fed gauge frozen for the rest of each cycle. Until a
full-length event capture exists, `tools/stitch_candump.py` repeats the
capture we have to span the trace (`--target-seconds 92` for the default
one) — dashboard-exercising data, not measurement data: totalizers reset at
every seam.

For a **measurement** bench rather than a bring-up one — the real agent
reading real interfaces, with load injected below it at the socketCAN and
serial boundaries — `docs/BENCH_RUNBOOK.md` is the operator document, and it
defers to this file for everything above.

`tools/lap_simulator.py` adds synthetic laps through the real timing engine.
It labels its data with a `<track>_sim` track name, so everything it wrote
is trivially filterable — and deletable — from the pit database afterwards.

## Changing what the garage watches

Edit `pit-config/live-decoder.yaml`. First matching rule wins, in file
order; a channel matched by no rule is not published at all. **No restart is
needed** — the live-decoder polls the file's mtime and also reloads on
`SIGHUP` — and **nothing on the vehicle changes**. Which channels the pit
displays is a pit concern; it must never require editing the car's profile
or restarting the agent.

A config that fails to parse on reload is logged and the previous one stays
in force, so a typo mid-session does not take the gauges down. `/health` on
8082 lists any rule matching nothing in the current registry, which is where
"why isn't my gauge updating?" gets answered.

The mount is a bind of the *directory*, not of the single file. Most editors
save by writing a temporary file and renaming it over the original, which
changes the inode; a single-file bind would keep pointing at the old one and
the edit would never be seen.

Widening the selection is cheap up to a point, and the point is the browser,
not the broker. `defaults.total_max_hz` is the valve; the file's header
records what the shipped selection actually measures against it, and
`/health`'s `aggregate_sheds` says whether the valve has ever had to act. If
you add rules, re-measure rather than assume -- a per-channel `max_hz` above
about 10 Hz buys a human nothing on a gauge, and every one of them is a
message the dashboard's browser tab has to handle.

## Changing a dashboard

`pit-config/grafana/dashboards/*.json` is provisioned into Grafana's
`openlaps` folder with `allowUiUpdates: false`, so **a panel changed in the
browser and not committed does not exist** — the UI will not let you save it
back. Edit, export the JSON model, commit it, and
`docker compose -f deploy/pit-compose.yaml restart grafana`. The full
procedure and the rules panels have to follow are in
`pit-config/grafana/dashboards/README.md`.

Datasources are provisioned the same way and are equally read-only in the
UI. Panels reference them by uid — `timescale` for SQL, `mqtt-live` for the
gauge feed — and those uids are stable on purpose: without an explicit one
Grafana generates a per-install value and a dashboard authored anywhere else
fails to load.

SQL panels read the **views** (`docs/PIT_SCHEMA.md`), which is not a
convention here but a grant: Grafana's database role has `SELECT` on the
views and no access at all to `samples` or `laps`.

## The car camera

Video is deliberately **not telemetry**. The vehicle runs
[go2rtc](https://github.com/AlexxIT/go2rtc) beside the agent — USB camera,
hardware-encoded on the SBC, cabin audio in Opus — and the pit operator's
browser plays it *directly from the car*
(`http://<vehicle-host>:1984/stream.html?src=car_h264&mode=mse`), embedded in
the `video` Grafana dashboard. No video byte touches NATS, JetStream or the database, so
a dead camera costs telemetry nothing, and dropping video is the degradation
plan's cheapest lever: close the browser tab.

This is the one crossing beside the leafnode, in the same direction — the
pit dials the vehicle on `:1984` (API/MSE), `:8554` (RTSP) and `:8555`
(WebRTC, TCP and UDP). The stream definitions and encoder bitrates live in
`targets/<target>/go2rtc.yaml`; they are sized against
`docs/LINK_BUDGET.md` §5's ~1.4 Mbit/s video reservation, which the
`openlaps_video_*` counters in `nft/bench-*.nft` exist to test.

**What the encode actually is depends on the board**, and that is the one
part of this stack that cannot be shared between targets. The X4 encodes
H.265 (`car`) plus an H.264 fallback (`car_h264`) on its iGPU through VAAPI;
the Luckfox Omni3576 encodes `car_h264` only, through Rockchip MPP, because
its VEPU will not give MPP an HEVC context. The dashboard's `stream` variable
already defaults to `car_h264`, so the default path works against either, and
`mode=mse` is only required for the H.265 stream. Each target's `go2rtc.yaml`
carries its own pipelines and its own reasons.

The service is opt-in because a host without a camera cannot start the
container:

```bash
docker compose -f deploy/vehicle-compose.yaml \
  -f deploy/targets/<target>/compose.yaml --profile video up -d
```

Verify from the pit before burying it in a dashboard: `curl -fsS
http://<vehicle-host>:1984/api/streams` lists `car`, and the URL above plays
in a browser. The dashboard's `camera` variable defaults to the bench
vehicle's address and is overridable in the browser at the track — the
committed JSON never needs an event-day edit.

## Tear-down

Stop the services, keep the data:

```bash
docker compose -f deploy/pit-compose.yaml down
```

Discard the JetStream store, the database and the session state as well —
this is destructive and unrecoverable:

```bash
docker compose -f deploy/pit-compose.yaml down -v
```

The vehicle stack is the same, with `vehicle-compose.yaml`. Note that `-v`
there drops the agent's registry and session state, so it re-publishes
`registry_seq` from scratch on the next start; pit history is unaffected,
because `channel_key` is resolved per generation and stable across them
(`docs/PIT_SCHEMA.md`).

## Running the agent without docker

`systemd/openlaps-agent.service` is the alternative for an SBC running the
agent directly, which is what the bench does today. Run one or the other,
not both — they would fight over the serial port.
