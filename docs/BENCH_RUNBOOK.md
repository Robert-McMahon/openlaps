# Bench runbook — Phase 4 measurement runs

The operator document for P4.3 (steady state), P4.4 (dropout ladder) and
P4.5 (range and degradation). P4.0 built the instrument
(`tools/link_probe.py`), P4.1 built the sources
(`profiles/example-club-racer-bench/`, `tools/bench_gps.py`,
`tools/bench_check.py`); this turns them into something a person can run
twice and get comparable numbers from.

**This document does not restate `deploy/README.md`.** Bring-up order, the
`OPENLAPS_NATS_URL` distinction, the subject-less sourced stream, the
per-hop verification and the secrets rules all live there and are not
duplicated here. What follows is the bench-specific delta plus the parts
`deploy/README.md` has no reason to know about: clock discipline, wire
counters, the clean-slate procedure, and what a run has to emit to count as
a result.

**A number without provenance is not a result** (`docs/plan/PHASE4.md` →
ground rules). Every run writes a manifest. A figure that later appears in
`docs/LINK_BUDGET.md` cites the manifest it came from, or it does not go in.

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

The pit dials the vehicle and never the reverse (`deploy/README.md`), which
is why the two `nft` rulesets in `deploy/nft/` are mirrors of each other
rather than one shared file — see §4.

The bench runs the **real deployment**, not a reduced stack: all seven pit
services, the real agent, the real leafnode (locked decision 2). Measuring
a reduced stack would measure something we are not going to run.

**Two probes, one per host, merged afterwards** (locked decision 3). Neither
probe reaches across the link it is measuring: a probe polling the far end
over the radio adds its own load and loses exactly the samples that matter
during a sever.

---

## 2. One-time host preparation

Do these once per host. They are not part of a run, and §6's bring-up
assumes they are already done.

### Both hosts

**`nft` counters, and read access to them.** Load the ruleset (§4) and give
the probe's user a way to read counters without becoming root. `nft` needs
`CAP_NET_ADMIN` even to *list*, and the probe must not run as root — it
writes the run CSV, and a root-owned artefact is a nuisance for the rest of
the analysis.

```bash
sudo tee /etc/sudoers.d/openlaps-bench >/dev/null <<'EOF'
openlaps ALL=(root) NOPASSWD: /usr/sbin/nft -j list counters
EOF
```

```bash
sudo visudo -c -f /etc/sudoers.d/openlaps-bench
```

Then pass `--nft-command "sudo -n nft -j list counters"` to the probe. `-n`
matters: without it a probe that has lost its sudo timestamp blocks on a
password prompt for the rest of the run instead of recording a reason and
carrying on.

**`chrony`**, for §3. Install it on both hosts; the configuration is per
role and is in §3.

**SSH to the local router**, for the radio adapter. Key-based, through
`ssh-agent` and `~/.ssh/config` — the probe shells out to the system `ssh`
and has no password option, deliberately (`docs/plan/PHASE4.md` → ground
rules: no secrets, and nothing that lands in shell history).

```
# ~/.ssh/config
Host halow-vehicle
    HostName 192.168.12.2
    User root
    IdentityFile ~/.ssh/id_ed25519_halow
```

Confirm the adapter's command works before a run depends on it — the
chipset's reporting varies, and which of these answers is a per-unit fact,
not a guess:

```bash
ssh halow-vehicle "iw dev wlan0 station dump"
```

```bash
ssh halow-vehicle "ubus call iwinfo assoclist '{\"device\":\"wlan0\"}'"
```

Whichever answers, pass `--radio-adapter auto` and let the probe record
which one it used in `radio_source`.

### Vehicle SBC only

**`vcan0`**, for the `vcan0` signal-source mode:

```bash
sudo modprobe vcan && sudo ip link add dev vcan0 type vcan && sudo ip link set up vcan0
```

**`can-utils`** for `canplayer`, and `uv sync` in the checkout.

**If the agent runs under `deploy/systemd/openlaps-agent.service`, it cannot
see the GPS feeder.** The unit sets `PrivateTmp=true`, so the agent gets its
own `/tmp` namespace and never sees the `/tmp/openlaps-bench-gps` symlink
`tools/bench_gps.py` publishes — the profile's serial port simply does not
exist as far as the agent is concerned. This presents as a collector that
never produces `position.*`, which is indistinguishable at a glance from the
void-fix problem `bench_gps.py` exists to solve. Either run the agent in the
foreground for bench runs (§6, and what the bench does today) or add a
drop-in:

```bash
sudo systemctl edit openlaps-agent   # [Service] \n PrivateTmp=false
```

---

## 3. Clock discipline

`docs/ARCHITECTURE.md` → "Link dropout and recovery" is explicit that
ingest-writer's `lag_ms` is **relative**: the two hosts' monotonic clocks
share no epoch, so `batch_epoch_mono_ns` cannot yield a one-way latency, and
"anything wanting absolute source-to-row latency has to measure it end to
end with a shared time reference". P4.3's latency distribution is only as
good as what this section establishes, so establish it first and record it.

### Read this before configuring anything

**The agent's clock is the system clock on this profile, and will report
itself as such.** `SteeredClock` steers toward GNSS only when the canonical
channel `position.time_unix_ms` is present among the mapped channels
(`src/agent/pipeline.py`, `docs/AGENT_DESIGN.md` → Clock discipline). The
NMEA decoder emits `lat`, `lon`, `speed`, `heading` and `mode` and no time
field (`src/collectors/serial/nmea.py`), and the example catalog maps five
`position.*` channels with no time among them. So on this deployment the
mechanism is dormant: `sys.agent.clock_source` reads `system`, always, and
`sys.agent.clock_offset_ms` is the boot-time system-to-monotonic offset
rather than anything GNSS-derived.

The consequence for this phase is simple and worth stating rather than
discovering: **host time discipline is the entire story.** Record
`sys.agent.clock_source` in the manifest anyway (`--clock-source`) — it is
the corroborating evidence that the agent was *not* independently steering,
which is what lets a chrony offset be read as the whole uncertainty.

**Never discipline anything from `tools/bench_gps.py`.** The feeder is a
position source, not a time source: `rmc_sentence` writes a fixed
`000000.00` time field and a fixed date (`tools/_bench.py`), and feeding it
to `gpsd` as a reference clock would be circular as well as wrong.

### Preferred: both hosts to the SBC, SBC GPS-disciplined

Only available when the SBC has a real receiver with a real fix — which is
not the indoor case, so treat it as the outdoor-run configuration.

On the SBC (`/etc/chrony/chrony.conf`):

```
refclock SHM 0 refid GPS precision 1e-1 offset 0.0 delay 0.2
allow 192.168.12.0/24
local stratum 10
```

On the pit:

```
server 192.168.12.176 iburst prefer minpoll 4 maxpoll 6
```

### Fallback, and the normal indoor case: one common NTP source

When the SBC has no fix, do **not** leave the pit chasing a stratum-10 local
clock that is itself free-running. Point both hosts at the same upstream and
say so in the manifest:

```
server 192.168.12.1 iburst minpoll 4 maxpoll 6
```

This is worse than the preferred configuration in a specific, reportable
way: it bounds the *relative* offset between the two hosts without bounding
either against true time. That is exactly what a source-to-row latency
measurement needs, so it is fine — provided the reported uncertainty is the
measured one and not an assumed one.

### Measure it, then record it

Let chrony settle (a few minutes at `minpoll 4`), then on **each** host:

```bash
chronyc tracking
```

```bash
chronyc sources -v
```

Take **RMS offset** from `chronyc tracking` as the number to quote, and
**Root dispersion** as the bound. The pit's offset relative to the SBC is
the figure P4.3's latency distribution is uncertain by; if it is not small
compared with the sub-500 ms target `docs/ARCHITECTURE.md` sets, the latency
result is not reportable and the run has found that out cheaply.

Pass both into every probe invocation for the run:

```bash
--clock-method "chrony, both hosts to 192.168.12.1 (SBC has no fix)" \
--clock-offset-ms 0.42 \
--clock-source system
```

**Reporting millisecond latency figures the clock discipline cannot support
would be worse than reporting none.** If chrony has not converged, say so in
`--note` and mark the latency section of the write-up as not measured.

---

## 4. Wire counters

`tools/link_probe.py` reads two named counters, `openlaps_leaf_out` and
`openlaps_leaf_in`, and falls back to `/proc/net/dev` when it finds neither
— recording which source it used in `wire_source`, because a run must never
be silently less precise than it looks. The fallback is every byte on the
interface rather than just the leafnode's, and `--summary` refuses to treat
the two as equivalent, so load the counters.

```bash
sudo nft -c -f deploy/nft/bench-vehicle.nft   # syntax check, changes nothing
```

```bash
sudo nft -f deploy/nft/bench-vehicle.nft      # vehicle SBC
```

```bash
sudo nft -f deploy/nft/bench-pit.nft          # pit
```

Both rulesets are counting-only — every chain policies `accept` and no rule
takes a verdict — so loading one on a live bench cannot drop a packet. Each
file documents its own port-to-direction mapping and why the two are
mirrored; read the header before assuming they are interchangeable.

They are deliberately **not** persisted across a reboot. A bench ruleset
that survives a reboot invisibly is one nobody remembers loading, and the
first symptom is a framing-overhead ratio computed against counters that
have been running since Tuesday. Loading them is a bring-up step.

Verify before measuring anything:

```bash
sudo nft -j list counters | python3 -m json.tool | head -40
```

A reload zeroes the counters. `link_probe` treats a decrease as an unknown
interval rather than a negative rate, so reloading mid-run costs one sample,
not a run — but there is no reason to do it.

---

## 5. Signal sources: choose a mode and record it

The bench reproduces the modelled signal mix without a car, injected *below*
the agent at the socketCAN and serial boundaries (locked decision 4).
`profiles/example-club-racer-bench/README.md` has the full reasoning; this
is the choice.

| Mode | CAN interface | Profile | Gives up | Use when |
| --- | --- | --- | --- | --- |
| **Physical** | `can0`, IMU powered | example profile, or a local copy of the bench profile with `interface: can0` | nothing | the IMU is on the bench |
| **Virtual** | `vcan0` | `profiles/example-club-racer-bench` | real arbitration, the USB adapter | the IMU is not |

**The trap in physical mode:** classic CAN needs at least one other node to
assert the ACK slot. With nothing else on the bus the adapter goes
error-passive and then bus-off, which presents as a driver or permissions
fault and is neither. Power the IMU.

GPS is synthetic in **both** modes. Indoors the UM980 reports a void fix and
the NMEA decoder drops any RMC whose status is not `A`, so a bench with a
real receiver and no sky view produces *no* GPS stream — silently, while
every other source looks healthy. That is ~250 samples/s missing, and a
bandwidth figure without it is wrong in the direction that flatters the
result. Run `tools/bench_gps.py`.

Record the mode, the profile path and its registry content hash in the
manifest (`--signal-source`, `--profile`). Two runs in different modes are
not comparable and the manifest is what stops them being compared.

---

## 6. Bring-up for a run

`deploy/README.md` → "Bring-up" is the procedure. These are the deltas.

### Vehicle SBC

1. **Clean slate** if this is not the first run of a series — §8, and do it
   before anything starts producing.

2. **GPS feeder first**, before the agent, so the symlink exists when the
   serial collector opens its port:

   ```bash
   uv run tools/bench_gps.py --loop
   ```

3. **CAN load.** Both fixtures are needed — they are different parts of the
   bus and `canplayer` takes one file per invocation. Two shells, or two
   backgrounded processes:

   ```bash
   canplayer -I tests/fixtures/candump/candump-sample.log -l i vcan0=can0
   ```

   ```bash
   canplayer -I tests/fixtures/candump/candump-imu-sample.log -l i vcan0=can0
   ```

   The fixtures are 8,000 frames / 5.29 s and 1,255 frames / 4.99 s. `-l i`
   loops. **The loop seam is a timestamp discontinuity** — harmless for
   offered load, but per-signal rates must not be read across one.

4. **nats-server**, then **the agent**. In the foreground, because of the
   `PrivateTmp` issue in §2:

   ```bash
   OPENLAPS_PROFILE=profiles/example-club-racer-bench \
   OPENLAPS_TICK_MS=20 \
   OPENLAPS_NATS_URL=nats://127.0.0.1:4222 \
   uv run openlaps-agent
   ```

   `OPENLAPS_TICK_MS` is the run variable. P4.3 wants one series at `20`
   (the deployed value, `example.env:44`) and one at `10`.

### Pit

`docker compose -f deploy/pit-compose.yaml up -d` — unchanged from
`deploy/README.md`. The only bench-specific note is the one the bench
profile's README raises: because the bench profile keeps
`vehicle.id: example-club-racer` but has its **own** `.registry-state.json`
beside it, a pit that has already seen the real profile's generations will
reject bench batches with `unknown_seq_batches` until it rescans the catalog
subject. The writer recovers on its own, and §8's clean slate avoids the
situation entirely. It is listed here because otherwise the first bench run
looks broken.

---

## 7. Verify before you measure

In this order. Each one that fails invalidates every number after it.

**The mix is right.** This is the gate, and it is not optional — a bench
that has silently lost GPS or the IMU still produces a perfectly plausible
bandwidth figure, of the wrong signal set:

```bash
uv run tools/bench_check.py --profile profiles/example-club-racer-bench
```

It also prints the bench's predicted mix against `LINK_BUDGET.md` §2's
modelled 4,087 samples/s, and **the two do not agree** — the bench offers
~2,400/s, about 59%. That gap is a modelling gap, not a wiring fault; the
bench profile's README has the per-class breakdown and the reasons.
Reconciling it is P4.3's signal-mix ground truth. Until then, read a bench
bandwidth figure as measuring ~59% of the load §3 predicts, and **say so in
the write-up** rather than letting a reader infer otherwise.

**Every hop is up.** `deploy/README.md` → "Verify each hop", unchanged:
`leafs` is 1 at both ends, `TELE_VEHICLE` is climbing toward `TELE`,
`tools/decode.py` resolves real channel names at the pit, rows are landing
in `v_samples_named`, all four `/health` endpoints answer.

**The health counters that invalidate a run are zero.** Any of these
non-zero means the run measured a degraded system rather than the system:

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

**A half-purged run silently mixes two conditions, and the resulting numbers
are unattributable rather than merely wrong.** Do all of it, in this order,
with nothing producing.

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
> A recreated `TELE_VEHICLE` plus a surviving cursor at seq 500,000 means
> the writer discards **everything** until the new stream climbs past it —
> a run that looks healthy at every hop and lands no rows. The same applies
> to `docker compose down -v` on the *vehicle* while the pit keeps its
> database. If you do recreate a stream, step 3 is mandatory, not optional.

**2. Drop the durable consumer**, so it rebuilds from the (now empty) stream
start rather than from a stale ack floor:

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

**4. Leave `.registry-state.json` alone.** The generation counter is
supposed to climb across runs; resetting it re-registers a *new* channel set
under an *old* generation number, and `channel_map` is keyed on
`(vehicle_id, registry_seq, wire_id)` — the pit would then hold two meanings
for one key. If you genuinely want a fresh sequence, delete the pit's
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

One per host, started within a few seconds of each other — `--merge` aligns
on timestamp with a tolerance, so they do not need to share a phase.

**Vehicle:**

```bash
uv run tools/link_probe.py --role vehicle \
  --out runs/2026-08-04-steady-t20-veh.csv \
  --nft-command "sudo -n nft -j list counters" \
  --radio-adapter auto --radio-host halow-vehicle --radio-iface wlan0 \
  --radio-config "2 MHz, MCS4, ch 9" \
  --tick-ms 20 --profile profiles/example-club-racer-bench \
  --signal-source "canplayer x2 on vcan0 + bench_gps 50 Hz" \
  --clock-method "chrony, both hosts to 192.168.12.1" \
  --clock-offset-ms 0.42 --clock-source system \
  --duration 2100 \
  --note "P4.3 steady state, tick 20 ms"
```

**Pit** — same clock and radio arguments, its own router, and no
`--tick-ms`/`--profile` (they are vehicle facts and belong on the vehicle's
manifest):

```bash
uv run tools/link_probe.py --role pit \
  --out runs/2026-08-04-steady-t20-pit.csv \
  --nft-command "sudo -n nft -j list counters" \
  --radio-adapter auto --radio-host halow-pit --radio-iface wlan0 \
  --clock-method "chrony, both hosts to 192.168.12.1" \
  --clock-offset-ms 0.42 --clock-source system \
  --duration 2100 \
  --note "P4.3 steady state, tick 20 ms"
```

Each writes `<out>.manifest.json` beside its CSV. `runs/` is a local
directory — raw CSVs are not checked in (§10).

**Start the probes before the load and stop them after it.** A run that
begins mid-stream has no baseline sample to difference the first interval
against, and the first row of a run carries no derived values at all.

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
10 ms. `sourcing_backlog` — the vehicle's `TELE` `last_seq` minus the pit's
`TELE_VEHICLE` `last_seq` — is the single most informative column in a
dropout run and the series whose failure to return to zero defines P4.5's
cliff.

**An unreachable endpoint is a null and a reason, never a zero.** If a
column is null for part of a run, read `reasons` before concluding anything
about it: "the link carried nothing" and "we could not ask" are different
measurements, and conflating them is how a dropout gets reported as idle.

---

## 10. Artefacts

`docs/bench/` holds the checked-in half of a run. Raw per-sample CSVs are
not checked in — they are large, they are local, and the manifest records
their absolute path (`.gitignore` carries the rule).

**One directory per run series**, for anything with probe output:

```
docs/bench/2026-08-04-steady-state/
    summary.md                      # the analysis, with numbers and their manifests
    t20-vehicle.manifest.json
    t20-pit.manifest.json
    t10-vehicle.manifest.json
    t10-pit.manifest.json
    notes.md                        # operator notes, verbatim, including what went wrong
```

A package that produces a single write-up and no probe output keeps the flat
`<name>.md` + `<name>.manifest.json` pair instead — P4.6's timing parity is
the existing example, and it stays where it is.

`notes.md` is not optional and not a tidy summary. Deviations from this
runbook, anything the operator noticed, and anything that went wrong belong
in it verbatim. P4.6's manifest records a docker daemon restart mid-run and
a first run discarded for an encoder bug; that is the standard.

---

## 11. Teardown

Stop the probes, the agent, `canplayer` and `bench_gps`. Then
`deploy/README.md` → "Tear-down" as written.

Unload the counters if the host is going back to normal duty:

```bash
sudo nft delete table inet openlaps_bench
```

Copy the raw CSVs off to wherever the manifests say they are before
reclaiming the disk. They are the only copy.

---

## 12. Acceptance: is the bench an instrument yet?

The test is not that a run completes. It is that **two consecutive baseline
runs agree** — same mode, same tick, same radio configuration, clean slate
between them, §7's mix check green for both.

Compare, from `--summary` on each:

| Series | Tolerance |
| --- | --- |
| `fwd_nats_kbit_s` mean | ±5% |
| `fwd_wire_kbit_s` mean | ±5% |
| `reverse_ratio` mean | ±10% |
| `framing_overhead` mean | ±5% |
| `sourcing_backlog` p95 | both ≈ 0 on a healthy link |

The wider tolerance on `reverse_ratio` is deliberate: it is a small number
divided by a large one, and the reverse channel carries RTCM only when
ntrip-client has an upstream, which is a per-run condition rather than a
property of the link. Record whether RTK was running.

**If two baselines do not agree within these, the bench is not yet an
instrument and no result from it means anything.** Find the difference
before running P4.3 — the usual causes are an incomplete clean slate, a
`canplayer` that died and was not noticed, and counters left loaded from a
previous session.
