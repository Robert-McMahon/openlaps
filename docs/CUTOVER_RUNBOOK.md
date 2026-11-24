# Commissioning runbook

The operator procedure for the first use of openlaps on a car: the
commissioning gates, the sequence for the day, the decision points, and
what to check after the first session. It gives numbered steps, exact
commands and expected outputs, with each warning placed at the step it
applies to.

This is a fresh installation. It is validated end to end on the bench by
replaying recorded data, and then used on the car (ADR 0001, amendment
2026-08-22).

This document does not repeat the [deployment guide](operations/index.md).
Bring-up order, the `OPENLAPS_NATS_URL` distinction, the subject-less
sourced stream, per-hop verification and the secrets rules are described
there. Every command below has been run on the bench. None has yet been run
on a car, and the sections say so where it matters.

---

## 1. Go/no-go: the gates, with answers

ADR 0001 gates the first session on **replay parity plus the garage bench
test**. These are the commissioning gates. Each gate cites the document and
manifest that closed it, or states that it is open. A number without
provenance is not a result (`docs/bench/README.md`). The open gates matter
most to the person deciding whether to go.

| # | Gate | State | Provenance |
| --- | --- | --- | --- |
| 1 | Replay parity | **PASS** | `docs/bench/timing-parity.md`, `docs/bench/timing-parity.manifest.json` |
| 2 | Steady state at modelled load | **PASS, with caveats below** | `docs/bench/2026-08-21-steady-state/summary.md` and its five manifests |
| 3 | Dropout and recovery ladder | **NOT RUN** | no `docs/bench/dropout.md` exists |
| 4 | Range and RF degradation | **NOT RUN** | no `docs/bench/range.md` exists |
| 5 | Commissioning report | **NOT WRITTEN** | this section stands in for it |

### Gate 1 — replay parity: PASS

The full June-2025 event (24 h 39 m, 1 767 617 GPS fixes) was replayed
through the real collectors, pipeline, timing engine, wire, leafnode,
sourced stream, ingest-writer and `v_laps`. The result was **768 lap
completions, 2 354 sector crossings and 39 pit crossings, every one
matching the reference replay of the same event**, with identical lap
numbering and lap times agreeing to a median of 0.24 µs (one float64 ulp).
The gate passed with no allowance used. Scope: it validates the
GPS-to-timing path and crosses no radio. It does not cover CAN-side
fidelity.

### Gate 2 — steady state: PASS, with caveats

Three 35-minute bench runs at the modelled signal mix (tick 20 ms twice,
10 ms once): offered load within −4.7%…+3.1% of `docs/LINK_BUDGET.md` §3's
model, zero on every loss counter at both ends (`unknown_seq_batches`,
`bad_version_batches`, `dropped_flushes`, `samples_dropped`, `mqtt_drops`,
`slow_consumers`, `db_errors`), ingest `lag_ms` p95 under 30 ms, and the
5.3 s CAN fixture shown representative of a 33.6-minute run to 0.2%.

Caveats that the bench did not close:

- **Absolute source-to-row latency is unmeasured.** The UM980 held no fix
  indoors, so the fix-gated PPS stayed silent and `lag_ms` above is
  relative, not source-to-row. The under-500 ms claim is still a model.
- **Framing overhead is unresolved.** Two instrumented runs contradict each
  other (ratios 1.082 and a physically impossible 0.905). The leading
  hypothesis is leafnode S2 compression, unconfirmed. Do not quote either
  number.
- **Airtime efficiency at saturation is unmeasured.** The bench offered
  ~470 kbit/s to a 32.5 Mbit/s link at −13 dBm. The 0.5 assumed in
  `docs/LINK_BUDGET.md` §5 governs behaviour at range and has not been
  measured.
- **30 of 163 catalog channels were never exercised**: the PDM and keypad
  I/O (eg `car.boost_button`). 18% of the mapped set is untested by every
  bench run, so a defect confined to those channels would not have shown.
- **No pit-side wire measurement exists** (the pit stack ran inside a VM
  the host's counters cannot see).

### Gate 3 — dropout ladder: NOT RUN

Nobody has severed the RF path and measured recovery. What exists in its
place is the recovery design (ADR 0002: JetStream sourcing resumes, and
`ingest_cursor` makes redelivery idempotent), the container-level sever
test (`tests/test_deploy_topology.py`), and one unplanned full-scale event.
During the parity run the docker daemon restarted with ~3 M messages on the
pit stream, and restarting the ingest-writer was the entire recovery: zero
gaps, zero duplicates (`docs/bench/timing-parity.md` → "An unplanned
durability test"). **Unmeasured:** catch-up drain rate while live traffic
continues, the vehicle-side loss boundary (`TELE` is capped at 8 GiB /
72 h; the publisher queue sheds oldest-first past its byte budget), and
behaviour across the 2-minute dedup window.

### Gate 4 — range: NOT RUN

−13 dBm is two radios in one room. The operational range limit for
full-rate telemetry (the RSSI/MCS at which the sourcing backlog stops
recovering) is unknown, and the ways to reduce the load in
`docs/LINK_BUDGET.md` §6 are unmeasured. The first sessions will sample
real range in any case. §5 below says what to record.

### The decision this leaves

Gates 1 and 2 show that the stack computes the right numbers and carries
the modelled load without loss. Gates 3 and 4 limit what can be relied on
(the live view at range and under dropouts). They do not limit what can be
lost: per §2, data survives every pit-side failure. "Go" is the decision to
run a session on this stack, and the worst credible outcomes are listed in
§2. The decision is the pit operator's.

---

## 2. If it goes badly

**There is no rollback procedure.** This is the only installation, and
there is no other stack to switch to (ADR 0001, amendment 2026-08-22).

The session is protected by local recording: **the car records locally and
durably whatever the pit is doing.** The vehicle's JetStream file store
(`TELE`, 8 GiB / 72 h) keeps every batch on the SBC's disk. The pit sources
from it when the link allows and drains the backlog when it returns, and
the ingest cursor makes that resumption idempotent. The parity run's
unplanned daemon restart exercised this mechanism at full scale with zero
loss. A dead pit, a dead radio, or a failed bring-up therefore costs the
**live view** and not the data.

The failure cases are:

1. **Pit degraded or down** → run the session anyway; the car is recording.
   Fix the pit, and sourcing catches up without intervention. Do not
   restart the vehicle stack to fix a pit problem.
2. **Vehicle stack down** → this is the only case that loses data. The
   agent restarts via compose (`restart: unless-stopped`) and re-registers
   without intervention. An SBC that will not boot ends telemetry for the
   session.
3. **In doubt mid-session** → leave the car alone, note the time, read
   `/health` at the pit afterwards. Every counter above is cumulative and
   will still be there.

---

## 3. The sequence

Three stages: a full-stack bench rehearsal with sample data (§3.1), the
car installation (§3.2), and first power-on with the car in it (§3.3).
Durations are bench-measured where stated, otherwise estimates.

### 3.1 Bench rehearsal with sample data — do this first (~1 h)

This proves the complete pit stack end to end with recorded data before it
is used with a car. It is the deployment guide's replay path, run as a
drill.

1. **Bring up the pit stack** as described in [Pit installation](operations/pit.md)
   (`docker compose -f deploy/pit-compose.yaml up -d`), with a
   populated `.env`. **The first bring-up needs internet**: Grafana
   fetches its one plugin into the `grafana-data` volume, and with a fresh
   volume on a disconnected pit Grafana does not come up. Do this before
   going to the track. (~2 min once images are built; the first build is
   longer.)

2. **Replay recorded telemetry through the real agent pipeline** into a
   local vehicle-side nats-server, or run the measurement bench of
   `docs/BENCH_RUNBOOK.md` if the bench hardware is set up:

   ```bash
   uv run tools/replay.py --server nats://127.0.0.1:4222 --rate 1.0 --loop
   ```

   > **Know what replay's default fixtures look like on a dashboard before
   > debugging.** Each `--loop` cycle is the ~110 s
   > GPS trace with the whole 30 s engine-start candump fixture replayed
   > once at its head. On the gauges that is `car.*` alive for ~30 seconds
   > out of every ~2 minutes and dead in between, and at the ingest-writer
   > it is a **constant** `wall_lag` of ~30 s, because the paced publish
   > runs behind by the length of the CAN prelude. Both are artefacts of
   > the harness, not faults in the stack. A real problem shows as a lag
   > that climbs without resetting, or `num_pending` growing on the pit
   > consumer. For a continuous full-mix rehearsal (every gauge live at
   > steady state, ~3,000–3,100 samples/s; the current fixtures predict
   > 3,015.4/s offered, and the 2026-08-21 steady-state runs measured
   > 3,091.0/s into `v_samples_named`), run the measurement bench instead
   > (`docs/BENCH_RUNBOOK.md` §5–§6: `vcan0`, two looped `canplayer`s,
   > `bench_gps`, the real agent). That is the configuration the
   > steady-state numbers were measured on.

3. **Verify every hop** using [Verify the stack](operations/verification.md), in
   order: `leafs` is 1 at both ends, `TELE_VEHICLE` climbing toward `TELE`,
   `tools/decode.py` resolving real names, `mosquitto_sub` showing JSON,
   rows landing in `v_samples_named`, all enabled `/health` endpoints
   answering, Grafana and both datasources (`timescale`, `mqtt-live`)
   green. (~10 min)

4. **Exercise the surfaces a session will use.** Open the car dashboard
   (Grafana `:3000`, folder `openlaps`, dashboard `Car: live`, uid
   `car`) and watch live gauges move; open the session UI (session-control,
   `:8080`), start a session, change driver, end it. Synthetic laps, if
   wanted, via `tools/lap_simulator.py`. It labels everything `<track>_sim`,
   so its laps are easy to delete afterwards.

5. **Read the health counters before calling it done.** All of
   `unknown_seq_batches`, `bad_version_batches`, `dropped_flushes`,
   `samples_dropped`, `db_errors` zero on `:8081/health`; `mqtt_drops` and
   `aggregate_sheds` zero and `unmatched_rules` empty on `:8082/health`;
   `stalled` false. A rehearsal that ends with a non-zero counter has found
   a fault. Investigate it now, on the bench.

### 3.2 What gets installed on the car, in what order

Everything vehicle-side, per [Vehicle installation](operations/vehicle.md).
All of it has been run on the bench:

1. **The SBC image**: a checkout, `uv sync`, and the docker image
   (`docker build -f deploy/Dockerfile -t openlaps:local .`). (~5 min)
2. **Clock chain**: chrony (`deploy/chrony/vehicle.conf`), the flashed
   RP2040 GNSS receiver interface (`firmware/gnss-receiver-interface/README.md`), and the shim
   unit. Bench-verified, but **the fix-gated PPS has never had a fix**;
   the first sky view is the first real test of it (§5).
3. **CAN**: `can0` up on the host before the agent:
   `sudo ip link set can0 up type can bitrate 1000000`. The agent
   configures no bitrate; that is host state. If it is forgotten, the
   agent looks healthy and produces no `car.*` channels.
4. **The HaLow radio**, powered and associated.
5. **The vehicle stack**: `docker compose -f deploy/vehicle-compose.yaml
   up -d`, then confirm `TELE` and `CMD` exist via
   `nats --server nats://127.0.0.1:4222 stream ls`. If the agent is run
   under systemd instead (`deploy/systemd/openlaps-agent.service`), run
   one or the other, never both, because both open the same serial port.

Ports the pit must reach on the vehicle: TCP 7422 for the leafnode, and
UDP 7423 for live frames if the pit runs `OPENLAPS_LIVE_SOURCE=direct`.
If the optional link tunnel is used, also install and start
`deploy/systemd/openlaps-link.service` on the vehicle (its header has the
install commands) and start the pit stack with `--profile link` and the
settings listed in the `link` service comment in `deploy/pit-compose.yaml`.
The pit then dials UDP 7424 on the vehicle, and the tunnel is the
leafnode's only route. Live frames can keep a direct UDP 7423 address as a
backup. The live link and the tunnel have been run on the bench only
(`docs/WIRE_FORMAT.md`, "Live link" and "Link tunnel"). They have not been
run on a radio at a track.

### 3.3 First power-on with the car (~30 min before first session)

In this order. A hop that fails invalidates everything after it, so do
not skip ahead.

1. **Vehicle up** (§3.2 step 5) and streams present. (~2 min)
2. **Clock**: `chronyc sources -v` on the SBC shows `GPS` selected once the
   receiver has a fix. If it does not, the session still runs:
   `sys.host.clock_*` telemetry records what the clock was doing, and
   internet NTP (if present) or holdover carries it. Note it and continue.
3. **Pit up**: `docker compose -f deploy/pit-compose.yaml up -d` on the
   pit host (chrony already installed per [Pit installation](operations/pit.md)).
   `depends_on` sequences the migrator and stream provisioner; watch both
   run to completion. (~2 min)
4. **Every hop**, same list as §3.1 step 3, now across the real radio.
   (~10 min)
5. **Control reaches the car**: start a session from the session UI on
   `:8080` (or the command in [Verify the stack](operations/verification.md),
   Control path) and confirm the command arrives:
   `nats --server nats://<vehicle-host>:4222 stream view CMD`.
6. **Dashboard sanity**: gauges moving at `:3000`, and lap rows appearing
   once the car crosses the line.

---

### 3.4 Phones: the alert path, end to end (~10 min, before the car leaves the trailer)

This check is required. An alert path that worked on the bench must also
be shown to reach a phone in the garage.

1. **The pit stack is up with `ntfy` and `notifier` healthy**
   (`curl -fsS http://127.0.0.1:8087/v1/health`, `:8086/health`), and
   `OPENLAPS_NOTIFIER_PUBLIC_URL` and `OPENLAPS_NTFY_PUBLIC_URL` in the
   pit's `.env` name the pit host's **LAN address**, not localhost — a
   phone cannot follow `localhost` anywhere.
2. **On every phone that is meant to hear an alert:** install the ntfy
   app (F-Droid, Play, App Store), add the server
   `http://<pit-host>:8087`, subscribe to `openlaps-critical` and, for
   the engineers but not the drivers, `openlaps-warning`. On Android, turn
   on **instant delivery** for the self-hosted server in the app's
   settings, or notifications are delayed by Android's battery
   management. The persistent "listening for incoming notifications" entry
   shows that it is on, and the app's battery-optimisation prompt must be
   accepted.
   Join the pit wifi. **iPhones** are different: iOS allows no
   background connection, so they only get a push if
   `OPENLAPS_NTFY_UPSTREAM_URL` is set (`example.env`), the pit has
   internet at that moment, and the phone can reach Apple's push
   service. The server entry in the app must also match
   `OPENLAPS_NTFY_PUBLIC_URL` exactly, since the relay topic is a hash
   of it. No ntfy.sh account is needed, and the alert content never
   leaves the LAN; only a wake-up does.

   **The pit wall therefore needs an Android device.** Whoever holds the
   pit wall carries an Android phone (or a tablet left on the wall) with
   instant delivery on. That device is the one path that needs no internet
   and no Apple service, and it must buzz in step 4. iPhones can be added
   as extras, but must never be the only phones subscribed.
3. **Open the annunciator** at `http://<pit-host>:8086/`, enable sound,
   and confirm the header reads **path alive**. If it does not within a
   minute, Grafana is not sending. Fix that before anything else.
4. **Press "Test critical delivery".** Every phone from step 2 must
   buzz, the annunciator must sound, and Discord (if the webhook is set)
   must show the embed. Acknowledge from a phone's notification button:
   the annunciator entry must show who acknowledged, within a second.
5. **Sever the internet** (unplug the uplink, leave the pit wifi) and
   repeat step 4. Phones on the wifi still buzz; Discord queues and the
   annunciator's queue counter goes non-zero, then drains when the
   uplink returns.

A phone that did not buzz in step 4 is not subscribed to the right
server, has instant delivery off, or is on a different wifi. Do not
proceed until every phone has buzzed.

## 4. Decision points

What would make the operator stop, and what stopping means.
**The decision is the pit operator's.** There is no second role and nobody
to escalate to. Remember §2 before aborting anything: an abort costs the
live view, never the recording.

| Seeing | Meaning | Do |
| --- | --- | --- |
| `leafs` 0 at either end after bring-up | radio or leafnode config; nothing crosses | Fix before the session if time allows; otherwise **run without live telemetry**. The car records regardless |
| `TELE` exists but `TELE_VEHICLE` not climbing | sourcing broken (domain string, provisioner) | Same as above; pit-side fix, car untouched |
| `unknown_seq_batches` climbing at `:8081/health` | pit has not seen this registry generation | Wait. The writer rescans and recovers without intervention. Do not clean-slate a car; that procedure (`docs/BENCH_RUNBOOK.md` §8) is for benches |
| `agent_status_age_s` climbing past ~10 s | vehicle agent stopped publishing | Check the agent container/unit on the SBC. This is the one stop-the-car item, because the car is not recording either |
| `stalled: true` or `db_errors` climbing | pit database trouble | Session runs; fix the pit afterwards, sourcing catches up |
| Gauges frozen but SQL panels advancing | live-decoder or mosquitto only | Cosmetic; note it, fix after |
| `lag_ms` growing without recovering | link cannot carry the load at this range | Expected beyond the unmeasured range limit (gate 4). The backlog drains when the car comes closer; watch, record, do not restart anything |

With the link tunnel enabled, `leafs` 0 can also mean the tunnel is down
(the vehicle's `openlaps-link` service stopped, or a path that drops UDP
7424). The leafnode has no other route, and it reconnects when the tunnel
returns. With `OPENLAPS_LIVE_SOURCE=direct`, `:8082/health` reports
`live_fallback`. `true` means no live frames are arriving and the gauges
show the pit's copy of the stream, which is late when the link is slow.

**Mid-session, the default action is no action.** Every recovery path in
this stack is designed to work unattended. A restart can cause a worse
failure than the lag it is meant to cure.

---

## 5. After the first session

What to read once real data has been through the stack. Each item either
closes a caveat from §1 or tunes a setting that has so far only been
modelled. Record numbers with their provenance.

1. **Ingest lag at session length.** `lag_ms` and `wall_lag_ms` on
   `:8081/health`. The first `wall_lag_ms` with a GPS-disciplined vehicle
   clock is the first real source-to-row latency figure (gate 2's first
   caveat).
2. **Was anything rate-capped harder than intended?** `suppressed` per
   channel and `aggregate_sheds` on `:8082/health`. Non-zero
   `aggregate_sheds` means the valve acted; per-channel `suppressed` says
   which gauges were starved. The fix is `deploy/pit-config/live-decoder.yaml`,
   reloaded live, with no restart and no change on the car.
3. **Are the trace panels usable at session length?** Open the `Car and
   engine` dashboard over the whole session's range. If panels are slow,
   note the range and panel. It is a view or aggregate problem, not a
   Grafana setting.
4. **Did the 30 untested channels produce data?** The first real button
   press exercises channels that no bench run did. Spot-check via the pit
   database, e.g.:

   ```bash
   docker compose -f deploy/pit-compose.yaml exec timescaledb psql -U openlaps -d openlaps -c "SELECT count(*) FROM v_samples_named WHERE channel = 'car.boost_button';"
   ```

5. **The radio, at real range.** `slow_consumers` on both `/varz`
   endpoints, `nats_reconnects` on the pit health surfaces, and whether
   `lag_ms` excursions map to where the car was on track. This is the data
   for gate 4. Write down RSSI if the routers were reachable.
6. **`reconnects`** on `:8083/health`, if RTK ran. The probe's
   `ntrip_reconnects` series moved on the bench (4 in 35 min) and there is
   no baseline yet for a normal number.
7. **Expect `catalog.yaml`'s RBE and rate caps to need tuning.** That
   should be a configuration change, not a code change, which is what the
   catalog design claims. The first session is the first test of that
   claim.
