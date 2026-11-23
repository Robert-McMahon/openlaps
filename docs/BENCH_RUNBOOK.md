# Bench runbook

The operator procedure for the bench measurement runs: steady state, the
dropout ladder, and range and degradation. The instrument is
`tools/link_probe.py`. The sources are `tools/bench-hardware.yaml`,
`tools/bench_gps.py` and `tools/bench_check.py`. This runbook describes how
to run them so that two runs give comparable numbers.

This document does not repeat the [deployment guide](operations/index.md).
Bring-up order, the `OPENLAPS_NATS_URL` distinction, the subject-less
sourced stream, the per-hop verification and the secrets rules are
described there. This runbook covers what is specific to the bench: clock
discipline, wire counters, the clean-slate procedure, and what a run must
record to count as a result.

Every run writes a manifest. A figure in `docs/LINK_BUDGET.md` must cite
the manifest it came from. A number without a manifest is not a result
(`docs/bench/README.md`).

---

## 1. Topology as built

| | Vehicle | Pit |
| --- | --- | --- |
| Host | SBC, **192.168.12.176** | **192.168.12.118** |
| Runs | vehicle nats-server, agent | full `deploy/pit-compose.yaml` |
| NATS client | `4222` | `4222` |
| NATS monitoring | `8222` | `8222` |
| Leafnode | **listens** on `7422` | **dials** the vehicle's `7422` |
| Health | — | `8080`–`8083` |
| Radio | HaLowLink unit, local to the SBC | HaLowLink unit, local to the pit |

The pit dials the vehicle and the vehicle never dials the pit
(`docs/ARCHITECTURE.md`, The vehicle-to-pit link). For this reason the two
`nft` rulesets in `deploy/nft/` mirror each other and are not one shared
file (see §4).

The table shows the leafnode connecting directly. With the optional link
tunnel (the pit stack's `link` profile and `openlaps-link.service` on the
vehicle) the pit dials UDP `7424` on the vehicle instead, and the leafnode
bytes travel inside that QUIC connection. With
`OPENLAPS_LIVE_SOURCE=direct` and no tunnel, the pit also sends to UDP
`7423` on the vehicle. See `docs/WIRE_FORMAT.md`, sections "Live link" and
"Link tunnel".

The bench runs the real deployment: all application services enabled for
the run, the real agent and the real leafnode. A reduced stack would not
measure the system that will be run.

**Two probes, one per host, merged afterwards.** Neither probe reaches
across the link it is measuring. A probe that polls the far end over the
radio adds its own load, and it loses its samples during a sever, which are
the samples the run needs.

---

## 2. One-time host preparation

Do these once per host. They are not part of a run, and §6's bring-up
assumes they are already done.

### Both hosts

**`nft` counters, and read access to them.** Load the ruleset (§4) and give
the probe's user a way to read counters without becoming root. `nft`
requires `CAP_NET_ADMIN` even to list counters, and the probe must not run
as root: it writes the run CSV, and a root-owned file is inconvenient for
the rest of the analysis.

```bash
sudo tee /etc/sudoers.d/openlaps-bench >/dev/null <<'EOF'
openlaps ALL=(root) NOPASSWD: /usr/sbin/nft -j list counters
EOF
```

```bash
sudo visudo -c -f /etc/sudoers.d/openlaps-bench
```

Then pass `--nft-command "sudo -n nft -j list counters"` to the probe. `-n`
is required. Without it, a probe that has lost its sudo timestamp blocks on
a password prompt for the rest of the run. With it, the probe records a
reason and continues.

**`chrony`**, for §3. Install it on both hosts; the configuration is per
role and is in §3.

**SSH to the local router**, for the radio adapter. Use key-based access
through `ssh-agent` and `~/.ssh/config`. The probe runs the system `ssh`
and has no password option, so that no secret is stored or appears in shell
history.

```
# ~/.ssh/config
Host halow-vehicle
    HostName 192.168.12.2
    User root
    IdentityFile ~/.ssh/id_ed25519_halow
```

Confirm the adapter's command works before a run depends on it. The
chipset's reporting varies, and which of these commands answers differs per
unit:

```bash
ssh halow-vehicle "iw dev wlan0 station dump"
```

```bash
ssh halow-vehicle "ubus call iwinfo assoclist '{\"device\":\"wlan0\"}'"
```

Whichever answers, pass `--radio-adapter auto`. The probe records which one
it used in `radio_source`.

### Vehicle SBC only

**`vcan0`**, for the `vcan0` signal-source mode:

```bash
sudo modprobe vcan && sudo ip link add dev vcan0 type vcan && sudo ip link set up vcan0
```

**`can-utils`** for `canplayer`, and `uv sync` in the checkout.

**If the agent runs under `deploy/systemd/openlaps-agent.service`, it cannot
see the GPS feeder.** The unit sets `PrivateTmp=true`, so the agent has its
own `/tmp` namespace and does not see the `/tmp/openlaps-bench-gps` symlink
that `tools/bench_gps.py` publishes. For the agent, the profile's serial
port does not exist. The symptom is a collector that never produces
`position.*`, which looks the same as the void-fix problem that
`bench_gps.py` exists to solve. Either run the agent in the foreground for
bench runs (§6, the current practice) or add a drop-in:

```bash
sudo systemctl edit openlaps-agent   # [Service] \n PrivateTmp=false
```

---

## 3. Clock discipline

`docs/ARCHITECTURE.md` (Link dropout and recovery) states that
ingest-writer's `lag_ms` is relative: the two hosts' monotonic clocks share
no epoch, so `batch_epoch_mono_ns` cannot give a one-way latency. Absolute
source-to-row latency must be measured end to end with a shared time
reference. The latency distribution of a steady-state run is only as good
as the clock discipline in this section, so establish it first and record
it.

### One authority: chrony and the system clock

The agent does not steer the clock from GNSS. Every collector correlates its
monotonic capture timestamp against the current `CLOCK_REALTIME`, so a chrony
slew is observed and no second in-process loop corrects the same clock.
Clock health comes from `chronyc tracking` through the host
collector as `sys.host.clock_offset_s`, `sys.host.clock_source`,
`sys.host.clock_stratum` and `sys.host.clock_root_dispersion_s`.

**Never discipline anything from `tools/bench_gps.py`.** The feeder provides
position only. `rmc_sentence` writes a fixed `000000.00` time field and a
fixed date (`tools/_bench.py`), so it carries no usable time, and feeding it
to `gpsd` as a reference clock would be wrong.

### Buildable preferred configuration: RP2040 GNSS receiver interface

Wire and flash the GNSS receiver interface exactly as
`firmware/gnss-receiver-interface/README.md` describes. The UM980 driver
configures fix-gated, active-high GPS PPS per the
receiver manual's `CONFIG PPS` options and sends 1 Hz ZDA plus GGA on COM2.
The RP2040 pairs each edge only with the sentence that follows it; a sentence
over 900 ms late is rejected. The host shim subtracts the firmware-known
edge-to-transmit interval and feeds a full sample to chrony's SOCK refclock.

Install the supplied configuration instead of transcribing it:

```bash
sudo apt-get install chrony python3-serial
getent group openlaps >/dev/null || sudo groupadd --system openlaps
id -u openlaps >/dev/null 2>&1 || \
  sudo useradd --system --gid openlaps --home-dir /opt/openlaps --shell /usr/sbin/nologin openlaps
sudo install -m 0644 deploy/chrony/vehicle.conf /etc/chrony/chrony.conf
sudo install -D -o root -g root -m 0755 tools/gnss_receiver_interface_shim.py \
  /usr/local/libexec/openlaps/gnss_receiver_interface_shim.py
# chrony's SOCK wire format lives beside it, shared with the GPIO PPS shim.
sudo install -D -o root -g root -m 0644 tools/chrony_sock.py \
  /usr/local/libexec/openlaps/chrony_sock.py
sudo install -m 0644 deploy/systemd/gnss-receiver-interface-shim.service \
  /etc/systemd/system/
sudo install -d /etc/systemd/system/chrony.service.d
sudo install -m 0644 deploy/systemd/chrony-openlaps-sock.conf \
  /etc/systemd/system/chrony.service.d/openlaps-sock.conf
sudo install -d /etc/openlaps
printf '%s\n' \
  'GNSS_RECEIVER_INTERFACE_ARGS=--device /dev/ttyACM0 --chrony-socket /run/chrony/openlaps-timing.sock' \
  | sudo tee /etc/openlaps/gnss-receiver-interface.env
sudo systemctl daemon-reload
sudo systemctl restart chrony
sudo systemctl enable --now gnss-receiver-interface-shim
```

For the UART build, change the device in `GNSS_RECEIVER_INTERFACE_ARGS` to
the X4 UART node and retain 115200 baud. On the pit:

```bash
sudo apt-get install chrony
sudo install -m 0644 deploy/chrony/pit.conf /etc/chrony/chrony.conf
sudo systemctl restart chrony
chronyc sources -v
```

Internet sources remain configured on both hosts. chrony selects the source
and handles fallback. Do not add failover code and do not issue
`chronyc makestep` during pull/restore testing.

### Measure it, then record it

Let chrony settle (a few minutes at `minpoll 4`), then on **each** host:

```bash
chronyc tracking
```

```bash
chronyc sources -v
```

Take **RMS offset** from `chronyc tracking` as the number to quote, and
**Root dispersion** as the bound. Confirm `GPS` is selected with the internet
unplugged, then pull and restore PPS while internet is present. Both
transitions must slew without a step and must appear in
`sys.host.clock_source`.

Characterise USB CDC and UART for at least one hour each. Record the
firmware delay distribution, chrony offset/jitter distribution, root
dispersion, sample/rejection counts, firmware SHA-256, transport and exact
wiring in a manifest under `docs/bench/`. This bench has no independent time
reference: report agreement with good internet NTP as a gross-error bound,
not as proof of microsecond absolute accuracy.

Pass both into every probe invocation for the run:

```bash
--clock-method "chrony SOCK GNSS receiver interface on vehicle; pit to 192.168.12.176" \
--clock-offset-ms 0.42 \
--clock-source GPS
```

**Do not report millisecond latency figures that the clock discipline
cannot support.** If chrony has not converged, say so in `--note` and mark
the latency section of the write-up as not measured.

---

## 4. Wire counters

`tools/link_probe.py` reads two named counters, `openlaps_leaf_out` and
`openlaps_leaf_in`. If it finds neither, it falls back to `/proc/net/dev`,
and it records which source it used in `wire_source`. The fallback counts
every byte on the interface, not only the leafnode's, and `--summary` does
not treat the two as equivalent, so load the counters.

```bash
sudo nft -c -f deploy/nft/bench-vehicle.nft   # syntax check, changes nothing
```

```bash
sudo nft -f deploy/nft/bench-vehicle.nft      # vehicle SBC
```

```bash
sudo nft -f deploy/nft/bench-pit.nft          # pit
```

Both rulesets only count: every chain has policy `accept` and no rule takes
a verdict, so loading one on a live bench cannot drop a packet. Each file
documents its port-to-direction mapping and why the two are mirrored. Read
the header before assuming they are interchangeable.

They are not persisted across a reboot, on purpose. A persisted ruleset
would keep counting from a load that nobody remembers, and a
framing-overhead ratio would then be computed against counters that started
in an earlier session. Loading them is a bring-up step.

Verify before measuring anything:

```bash
sudo nft -j list counters | python3 -m json.tool | head -40
```

A reload zeroes the counters. `link_probe` treats a decrease as an unknown
interval, not as a negative rate, so a reload mid-run costs one sample.
There is no reason to do it.

The leaf counters match TCP port `7422` only. Live frames (UDP `7423`) are
not counted. With the link tunnel enabled, the leafnode bytes cross the
link inside QUIC on UDP `7424`, so these counters do not measure what the
tunnel puts on the wire. The rulesets have not been extended for either.

---

## 5. Signal sources: choose a mode and record it

The bench reproduces the modelled signal mix without a car. Signals are
injected below the agent, at the socketCAN and serial boundaries.

**The car and the bench use one profile.** `profiles/example-club-racer/` is
the car. `tools/bench-hardware.yaml` is a host-wiring overlay on top of it
(ADR 0010) that moves two transports and declares the receiver absent, and
changes nothing else. A bench bandwidth figure is therefore a figure for
the car: the catalog, the DBCs and the wire encodings are the same files,
not a copy that must be kept in step. The overlay's comments give the
reason for each of its three lines.

| Mode | CAN interface | How | Gives up | Use when |
| --- | --- | --- | --- | --- |
| **Physical** | `can0`, IMU powered | a copy of the overlay with its `buses:` block deleted | nothing | the IMU is on the bench |
| **Virtual** | `vcan0` | `OPENLAPS_HARDWARE=tools/bench-hardware.yaml` | real arbitration, the USB adapter | the IMU is not |

Physical mode still needs an overlay, because **GPS is synthetic in both
modes** (below) and the serial half of the overlay provides that.
Deleting the `buses:` block is the only edit: an overlay overrides only what
it names, so `can0` falls back to the profile's own `interface: can0`.

**In physical mode, power the IMU.** Classic CAN needs at least one other
node to assert the ACK slot. With nothing else on the bus the adapter goes
error-passive and then bus-off. This looks like a driver or permissions
fault and is neither.

`can-utils` is installed on the SBC and `canplayer` replays the checked-in
captures into either interface.

GPS is synthetic in **both** modes. Indoors the UM980 reports a void fix and
the NMEA decoder drops any RMC whose status is not `A`, so a bench with a
real receiver and no sky view produces no GPS stream. Nothing reports an
error, and every other source looks healthy. About 250 samples/s are then
missing, and the bandwidth figure is too low. Run `tools/bench_gps.py`.

Record the mode, the profile path and its registry content hash in the
manifest (`--signal-source`, `--profile`). Two runs in different modes are
not comparable, and the manifest is the record that shows which mode a run
used.

Manifests from runs before 2026-09-09 name a profile that no longer exists:
an `example-club-racer-bench` directory that was a byte-for-byte copy of the
example profile carrying the three lines the overlay carries now. Those
manifests are left as written, because they record what ran. Compare
against their registry content hash, not their path. The hash is unchanged
because the catalog was identical.

---

## 6. Bring-up for a run

The [vehicle](operations/vehicle.md) and [pit](operations/pit.md) installation
guides are the base procedure. These are the bench-specific deltas.

### Vehicle SBC

1. **Clean slate** if this is not the first run of a series (§8). Do it
   before anything starts producing.

2. **GPS feeder first**, before the agent, so the symlink exists when the
   serial collector opens its port:

   ```bash
   uv run tools/bench_gps.py --loop
   ```

3. **CAN load.** Both fixtures are needed. They are different parts of the
   bus and `canplayer` takes one file per invocation. Use two shells, or two
   backgrounded processes:

   ```bash
   canplayer -I tests/fixtures/candump/candump-sample.log -l i vcan0=can0
   ```

   ```bash
   canplayer -I tests/fixtures/candump/candump-imu-sample.log -l i vcan0=can0
   ```

   The fixtures are 45,861 frames / 30.0 s and 1,255 frames / 4.99 s. `-l i`
   loops. **The loop seam is a timestamp discontinuity.** It does not affect
   offered load, but per-signal rates must not be read across one.

4. **nats-server**, then **the agent**. In the foreground, because of the
   `PrivateTmp` issue in §2:

   ```bash
   OPENLAPS_PROFILE=profiles/example-club-racer \
   OPENLAPS_HARDWARE=tools/bench-hardware.yaml \
   OPENLAPS_TICK_MS=20 \
   OPENLAPS_NATS_URL=nats://127.0.0.1:4222 \
   uv run openlaps-agent
   ```

   `OPENLAPS_TICK_MS` is the run variable. The steady-state measurement
   requires one series at `20` (the deployed value in `example.env`) and
   one at `10`.

### Pit

`docker compose -f deploy/pit-compose.yaml up -d`, unchanged from the pit
installation guide. There is one bench-specific note. The bench keeps
`vehicle.id: example-club-racer` but has its **own** `.registry-state.json`,
so a pit that has already seen the real profile's generations will
reject bench batches with `unknown_seq_batches` until it rescans the catalog
subject. The writer recovers without intervention, and §8's clean slate
avoids the situation. It is listed here because otherwise the first bench
run appears broken.

---

## 7. Verify before you measure

In this order. A check that fails invalidates every number after it.

**The mix is right.** This check is required. A bench that has lost GPS or
the IMU still produces a plausible bandwidth figure, for the wrong signal
set:

```bash
uv run tools/bench_check.py
```

Its defaults are the bench rig (the example profile plus
`tools/bench-hardware.yaml`), so a bare run checks the mode this runbook
describes. `--profile` and `--hardware` override either half.

It also prints the bench's predicted mix against `LINK_BUDGET.md` §2's
modelled 4,262 samples/s. **The two do not agree**: the bench offers
~3,000/s, about 71%. The gap is in the model, not in the wiring. The tool's
`--predict` output has the per-class breakdown. Reconciling the two is part
of the steady-state measurement. Until that is done, read a bench bandwidth
figure as measuring ~71% of the load §3 predicts, and **say so in the
write-up**.

**Every hop is up.** [Verify the stack](operations/verification.md),
unchanged: `leafs` is 1 at both ends, `TELE_VEHICLE` is climbing toward
`TELE`, `tools/decode.py` resolves real channel names at the pit, rows are
landing in `v_samples_named`, all four `/health` endpoints answer. With the
link tunnel enabled, `leafs` is still 1 at both ends, because the leafnode
connects through the tunnel.

**The health counters that invalidate a run are zero.** If any of these is
non-zero, the run measured a degraded system:

| Counter | Where |
| --- | --- |
| `slow_consumers` | `/varz`, both servers |
| `sys.agent.publish_drops` | agent telemetry |
| `unknown_seq_batches` | ingest-writer `/health` |
| `bad_version_batches` | ingest-writer `/health` |
| `dropped_flushes` | ingest-writer `/health` |

**Chrony has converged** (§3), and the offset you are about to record is the
measured one.

---

## 8. Clean slate between runs

**A partly purged run mixes two conditions, and its numbers cannot be
attributed to either.** Do all of it, in this order, with nothing
producing.

Stop the agent, `canplayer` and `bench_gps` first. Leave both nats-servers
and the pit stack running.

**1. Purge the streams — purge, do not delete.**

```bash
nats --server nats://127.0.0.1:4222 stream purge TELE -f          # vehicle
```

```bash
nats --server nats://127.0.0.1:4222 stream purge TELE_VEHICLE -f  # pit
```

> **Purge keeps the sequence counter climbing; delete-and-recreate restarts
> it at 1.** `ingest_cursor` holds the last stream sequence written, and the
> writer skips anything at or below it (`src/pit/ingest_writer/writer.py`).
> With a recreated `TELE_VEHICLE` and a surviving cursor at seq 500,000,
> the writer discards **everything** until the new stream climbs past it.
> Such a run looks healthy at every hop and lands no rows. The same applies
> to `docker compose down -v` on the *vehicle* while the pit keeps its
> database. If you do recreate a stream, step 3 is mandatory.

**2. Drop the durable consumer**, so that it rebuilds from the (now empty)
stream start and not from a stale ack floor:

```bash
nats --server nats://127.0.0.1:4222 consumer rm TELE_VEHICLE ingest-writer -f
```

**3. Truncate the pit tables and reset the cursor**, in one transaction:

```bash
docker compose -f deploy/pit-compose.yaml exec -T timescaledb \
  psql -U openlaps -d openlaps -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;
TRUNCATE samples;
TRUNCATE lap_sectors, laps CASCADE;
DELETE FROM ingest_cursor WHERE consumer = 'ingest-writer';
COMMIT;
SQL
```

**4. Leave `.registry-state.json` alone.** The generation counter is meant
to climb across runs. Resetting it re-registers a new channel set under an
old generation number, and `channel_map` is keyed on
`(vehicle_id, registry_seq, wire_id)`, so the pit would then hold two
meanings for one key. If you do want a fresh sequence, delete the pit's
registry rows in the same operation as step 3 (`DELETE FROM
channel_registry WHERE vehicle_id = 'example-club-racer'` cascades to
`channel_map`) and say why in the manifest notes.

**5. Restart the ingest-writer** so it re-reads the cursor it no longer has:

```bash
docker compose -f deploy/pit-compose.yaml restart ingest-writer
```

**6. Confirm the slate is clean** before bringing sources back up:

```bash
docker compose -f deploy/pit-compose.yaml exec -T timescaledb \
  psql -U openlaps -d openlaps -c "SELECT count(*) FROM samples;"
```

```bash
nats --server nats://127.0.0.1:4222 stream report
```

---

## 9. Running the probes

One per host, started within a few seconds of each other. `--merge` aligns
on timestamp with a tolerance, so they do not need to share a phase.

**Vehicle:**

```bash
uv run tools/link_probe.py --role vehicle \
  --out runs/2026-08-04-steady-t20-veh.csv \
  --nft-command "sudo -n nft -j list counters" \
  --radio-adapter auto --radio-host halow-vehicle --radio-iface wlan0 \
  --radio-config "2 MHz, MCS4, ch 9" \
  --tick-ms 20 --profile profiles/example-club-racer \
  --signal-source "canplayer x2 on vcan0 + bench_gps 50 Hz" \
  --clock-method "chrony, both hosts to 192.168.12.1" \
  --clock-offset-ms 0.42 --clock-source system \
  --duration 2100 \
  --note "steady state, tick 20 ms"
```

**Pit**: same clock and radio arguments, its own router, and no
`--tick-ms`/`--profile` (they are vehicle facts and belong in the vehicle's
manifest):

```bash
uv run tools/link_probe.py --role pit \
  --out runs/2026-08-04-steady-t20-pit.csv \
  --nft-command "sudo -n nft -j list counters" \
  --radio-adapter auto --radio-host halow-pit --radio-iface wlan0 \
  --clock-method "chrony, both hosts to 192.168.12.1" \
  --clock-offset-ms 0.42 --clock-source system \
  --duration 2100 \
  --note "steady state, tick 20 ms"
```

Each writes `<out>.manifest.json` beside its CSV. `runs/` is a local
directory. Raw CSVs are not checked in (§10).

**Start the probes before the load and stop them after it.** A run that
begins mid-stream has no baseline sample to difference the first interval
against, and the first row of a run carries no derived values.

**Merge and summarise afterwards**, on either host:

```bash
uv run tools/link_probe.py --merge runs/…-veh.csv runs/…-pit.csv \
  --out runs/2026-08-04-steady-t20-merged.csv
```

```bash
uv run tools/link_probe.py --summary runs/2026-08-04-steady-t20-merged.csv --tick-ms 20
```

`--summary` prints per-series mean / p50 / p95 / max plus the comparison
against `LINK_BUDGET.md` §3's modelled 544.4 kbit/s at 20 ms and 552.8 at
10 ms. `sourcing_backlog` (the vehicle's `TELE` `last_seq` minus the pit's
`TELE_VEHICLE` `last_seq`) is the most informative column in a dropout run.
In a range run, the point at which it stops returning to zero is the range
limit.

**An unreachable endpoint is recorded as a null and a reason, never as a
zero.** If a column is null for part of a run, read `reasons` before
concluding anything about it. "The link carried nothing" and "the probe
could not ask" are different measurements. Treating them as the same
reports a dropout as an idle link.

---

## 10. Artefacts

`docs/bench/` holds the checked-in half of a run. Raw per-sample CSVs are
not checked in: they are large and local, and the manifest records their
absolute path (`.gitignore` carries the rule).

**One directory per run series**, for anything with probe output:

```
docs/bench/<date>-<what>/           # e.g. 2026-08-04-steady-state
    summary.md                      # the analysis, with numbers and their manifests
    t20-vehicle.manifest.json
    t20-pit.manifest.json
    t10-vehicle.manifest.json
    t10-pit.manifest.json
    notes.md                        # operator notes, verbatim, including what went wrong
```

A package that produces a single write-up and no probe output keeps the flat
`<name>.md` + `<name>.manifest.json` pair instead. The timing parity report
is the existing example.

`notes.md` is required, and it is not a summary. Deviations from this
runbook, anything the operator noticed, and anything that went wrong belong
in it verbatim. For example, the timing parity manifest records a docker
daemon restart mid-run and a first run discarded for an encoder bug.

---

## 11. Teardown

Stop the probes, the agent, `canplayer` and `bench_gps`. Stop the Compose
stacks with `docker compose ... down`; omit `-v` to preserve stored data.

Unload the counters if the host is going back to normal duty:

```bash
sudo nft delete table inet openlaps_bench
```

Copy the raw CSVs off to wherever the manifests say they are before
reclaiming the disk. They are the only copy.

---

## 12. Acceptance: is the bench an instrument yet?

A completed run is not sufficient. The test is that **two consecutive
baseline runs agree**: same mode, same tick, same radio configuration, clean
slate between them, §7's mix check green for both.

Compare, from `--summary` on each:

| Series | Tolerance |
| --- | --- |
| `fwd_nats_kbit_s` mean | ±5% |
| `fwd_wire_kbit_s` mean | ±5% |
| `reverse_ratio` mean | ±10% |
| `framing_overhead` mean | ±5% |
| `sourcing_backlog` p95 | both ≈ 0 on a healthy link |

The wider tolerance on `reverse_ratio` is intended. It is a small number
divided by a large one, and the reverse channel carries RTCM only when
ntrip-client has an upstream, which is a condition of the run and not a
property of the link. Record whether RTK was running.

**If two baselines do not agree within these tolerances, no result from the
bench can be used.** Find the difference before running the steady-state
series. The usual causes are an incomplete clean slate, a `canplayer` that
died and was not noticed, and counters left loaded from a previous session.

# Endurance alert firing drill

The rules exercised here are rendered from
`profiles/example-club-racer/alarms.yaml` by `tools/gen_alert_rules.py`
(`docs/CATALOG.md` → `alarms.yaml` schema).  Change a limit there, re-run the
generator, and commit both files together; `tests/test_gen_alert_rules.py`
fails when the provisioning file is stale.  The thresholds in the table
below are the *rendered* values in catalog units: temperatures are Kelvin
because the ECU reports Kelvin, even though `alarms.yaml` declares them in
Celsius.

Run this drill against a disposable bench database, never against the race
archive.  Open Grafana's **Reliability watch** dashboard (`uid=reliability`),
its Alerting page, and the **annunciator** at `http://<pit-host>:8086/`
first.  The provisioned `openlaps-local` contact point posts only to the
notifier (`http://notifier:8086/grafana-alerts`, the compose service name)
and requires no external account.  The notifier records every alert in the
`alert_events` table (`SELECT * FROM v_alert_events ORDER BY time DESC`),
shows it on the annunciator with a tone, and repeats it until someone
acknowledges.  The annunciator is therefore where to check what fired
during a session and who saw it.  A failed delivery does not prevent
Grafana showing the rule as Firing.

Before any rule is exercised, confirm the path itself is alive: the
annunciator's header must read **path alive** with a heartbeat age under a
minute.  That indicator is the `notifier-heartbeat` rule, always firing and
re-sent by Grafana every minute; stop Grafana and the indicator goes red
within three minutes.  Then press **Test critical delivery** on the
annunciator: the tone sounds, the alert appears, and acknowledging it with a
name clears it and records the acknowledgement in `alert_acks`.

Connect as the database owner and create this disposable helper.  It writes
through the same registry/sample shape as ingest while keeping every alert
query on the stable `v_samples_named` view:

```sql
CREATE OR REPLACE PROCEDURE bench_alert_sample(
    channel_name text, numeric_value double precision, sample_time timestamptz DEFAULT now()
)
LANGUAGE plpgsql AS $$
DECLARE key bigint;
BEGIN
  INSERT INTO channels (vehicle_id, name, units, value_type)
  VALUES ('example-club-racer', channel_name, '', 1)
  ON CONFLICT (vehicle_id, name) DO UPDATE SET name = excluded.name
  RETURNING channel_key INTO key;
  INSERT INTO samples (time, channel_key, value) VALUES (sample_time, key, numeric_value);
END $$;

-- All car-channel rules are gated on an open session AND an on-track lap
-- event.  Open a bench session first (the session UI does the same through
-- session-control); set status = 'ended' at the end of the drill and prove
-- every car-channel rule falls Normal with the car still "on track".
INSERT INTO sessions (session_id, vehicle_id, session_type, started, status)
VALUES ('bench-drill', 'example-club-racer', 'test', now(), 'active')
ON CONFLICT (session_id) DO UPDATE SET status = 'active', ended = NULL;

-- This event puts the bench in the on-track state; substitute "pit" to
-- prove the same rules remain Normal in the pits.
WITH channel AS (
  INSERT INTO channels (vehicle_id, name, units, value_type)
  VALUES ('example-club-racer', 'lap.event', '', 4)
  ON CONFLICT (vehicle_id, name) DO UPDATE SET name = excluded.name
  RETURNING channel_key
)
INSERT INTO samples (time, channel_key, value_text)
SELECT now(), channel_key, '{"type":"lap_completed","pit_status":"track"}' FROM channel;
```

Exercise one rule at a time, wait for its configured `for` period plus two
10-second evaluation intervals (Grafana's base interval, which 12.4.9
refuses to lower; see the comment in `deploy/pit-compose.yaml`), verify
**Firing**, then write the reset value and verify **Normal**.  Every
continuous rule carries a recovery threshold: a value between the firing
and clear thresholds must leave a Firing rule Firing, and must leave a
Normal rule Normal.  Temperatures below are Kelvin, not Celsius.

**Latency budget.**  With `oil-pressure-low` reset, note the wall
clock, `CALL bench_alert_sample('car.oil_pressure', 150);` (RPM already at
4000), and note the time the annunciator shows it (or the `time` column of
the `alert_events` row).
The budget is under 10 s for a critical alarm.  Through Grafana the path is
3 s `for` plus up to 10 s of evaluation plus 0 s `group_wait`: 3-13 s, which
is over budget in the worst case.  Record
the measurement below each time the Grafana pin or the path changes.

| Date | Grafana | Path | Crossing to notification | Notes |
| --- | --- | --- | --- | --- |
| 2026-09-16 | 12.4.9 | Grafana rules | _not measured_ | evaluation floor cannot be lowered below 10 s; see pit-compose.yaml |

| Rule uid | Firing sample | Reset sample |
| --- | --- | --- |
| `oil-pressure-low` | `CALL bench_alert_sample('car.rpm', 4000); CALL bench_alert_sample('car.oil_pressure', 150);` | `CALL bench_alert_sample('car.oil_pressure', 350);` (clears above 250) |
| `coolant-temperature-high` | `CALL bench_alert_sample('car.coolant_temp', 384.15);` | `CALL bench_alert_sample('car.coolant_temp', 363.15);` (clears below 378.15) |
| `oil-temperature-high` | `CALL bench_alert_sample('car.oil_temp', 399.15);` | `CALL bench_alert_sample('car.oil_temp', 373.15);` |
| `battery-voltage-low` | `CALL bench_alert_sample('car.battery_v', 11.0);` | `CALL bench_alert_sample('car.battery_v', 13.8);` |
| `knock-high` | `CALL bench_alert_sample('car.knock_level1', 90);` | `CALL bench_alert_sample('car.knock_level1', 0);` |
| `engine-protection-active` | `CALL bench_alert_sample('car.engine_protection_severity', 2);` | `CALL bench_alert_sample('car.engine_protection_severity', 0);` |
| `publish-lag-high` | `CALL bench_alert_sample('sys.agent.publish_lag_ms', 750);` | `CALL bench_alert_sample('sys.agent.publish_lag_ms', 0);` |
| `can-bus-stalled` | `CALL bench_alert_sample('sys.agent.stall_s.can0', 8);` The real path needs a wedged controller and cannot be staged on a bench: the signature (kernel `rx_packets` rising, nothing delivered) is proven by `tests/test_can_stall.py`, and the agent reported it live on the Luckfox on 2026-11-08. Do **not** try to provoke it with `ip link set can0 down` -- that is what hung the board. | `CALL bench_alert_sample('sys.agent.stall_s.can0', 0);` (clears below 1) |
| `live-feed-stale` | Stop the replay after an on-track `car.rpm` sample and wait more than 5 seconds. | Restart replay or `CALL bench_alert_sample('car.rpm', 3000);` |
| `notifier-heartbeat` | Always firing; nothing to do. | Stop the grafana container: the annunciator's path indicator goes red within three minutes and `/health` reports `heartbeat.ok: false`. |
| `strategy-warning` | `INSERT INTO watch_findings (finding_id, vehicle_id, monitor, opened_at, severity, peak_score, summary) VALUES (gen_random_uuid(), 'example-club-racer', 'strategy.bench_drill', now(), 'warning', 1.0, '{"message": "bench drill"}');` | `UPDATE watch_findings SET closed_at = now() WHERE monitor = 'strategy.bench_drill' AND closed_at IS NULL;` |
| `strategy-critical` | As above with `'critical'`. The real path: a race plan with `max continuous` a few minutes ahead of the current stint's elapsed time raises `strategy.driver_time` from the strategy service within its poll interval; the dashboard's *Open strategy findings* table shows it. | Close the row as above, or end the session: the service closes every strategy finding when no session is open. |

| `field-warning` | `INSERT INTO watch_findings (finding_id, vehicle_id, monitor, opened_at, severity, peak_score, summary) VALUES (gen_random_uuid(), 'example-club-racer', 'field.bench_drill', now(), 'warning', 2.0, '{"message": "bench drill"}');` | `UPDATE watch_findings SET closed_at = now() WHERE monitor = 'field.bench_drill' AND closed_at IS NULL;` |
| `field-critical` | As above with `'critical'`. The real path: with the session open, a race plan whose *car number* is `27`, and the timing feed replaying its fixture (`OPENLAPS_TIMING_FEED_SOURCE=replay`, `OPENLAPS_TIMING_FEED_REPLAY_FILE=tests/fixtures/natsoft/hand-built.jsonl`), the feed counts four laps for car 27 against however many the bench has written to `laps`; more than a lap apart raises `field.lap_count` within `OPENLAPS_TIMING_FEED_RECONCILE_S`, warning up to three laps apart and critical beyond. `GET :8089/health` shows `feed_laps` beside `vehicle_laps`. | Close the row as above, bring the two counts within a lap of each other, or end the session: the service closes the finding when no session is open. |

The `strategy-*` and `field-*` rules watch `v_watch_findings` rows whose
monitor starts with `strategy.` and `field.`; the drill monitor names above
are ones neither service owns, so a running service leaves the drill row
alone (it adopts and closes only the monitors it judges).

Finally write a `lap.event` with `"pit_status":"pit"`, repeat each car-channel
firing sample, and verify the seven on-track-gated rules remain Normal.  Then
restore `"track"`, `UPDATE sessions SET status = 'ended' WHERE session_id =
'bench-drill'`, repeat the firing samples again, and verify the same seven
stay Normal: that is the overnight case.  The `publish-lag-high` and
`can-bus-stalled` pipeline rules deliberately remain active in both.

### Envelope findings

`watch-critical` and `watch-warning` are generated from the profile and select
open findings of their respective severity. Follow [the watch firing and clean
replay drill](WATCH.md#firing-and-clean-replay-checks): clean on-track replay
must leave both non-firing; a 0.7 oil-pressure injection after learning must
open the critical rule, and an equivalent sustained coolant-temperature
change outside its learned envelope must open the warning rule. Healthy
samples must close the findings and resolve the rules. The short engine-start
fixture alone cannot learn these on-track baselines. Check `/health` on 8090
and both `v_watch_*` views before attributing a non-firing rule to a healthy car.

The other monitor kinds (per-lap drift, the driveline and wheel-speed
ratios, the trigger-error counter and the whole-car model) fire through the
same two rules. Their demonstrated firings are the synthetic drives in
`tests/test_watch_monitors.py`. There is not yet a replayable fault for
each one against the live stack. The drift and ratio kinds need laps
and gears that the engine-start fixture does not have.
