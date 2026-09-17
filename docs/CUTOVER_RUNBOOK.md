# Cutover runbook — go/no-go and the first sessions

The operator document for putting the fresh stack on the car. Written for
someone tired, in a hurry, in a garage, on a phone: numbered steps, exact
commands, expected outputs, and the warnings placed where the mistake would
be made.

**There is no cutover event.** Owner direction (2026-08-22, recorded as ADR
0001's amendment): this is a **fresh installation**, validated end-to-end on
the bench by replaying recorded data, then used. The old stack plays no part
in this document — it is not installed alongside, not kept warm, and not a
fallback. The word "cutover" survives only in this file's name, which other
documents already cite.

**This document does not restate the [deployment guide](operations/index.md).** Bring-up order, the
`OPENLAPS_NATS_URL` distinction, the subject-less sourced stream, per-hop
verification and the secrets rules live there. What follows is the
go/no-go, the sequencing for a day with a car in it, the decision points,
and what to look at after the first session. Every command below has been
run on the bench; none has yet been run on a car, and the sections say so
where it matters.

---

## 1. Go/no-go: the gates, with answers

ADR 0001 gates the first session on **replay parity plus the garage bench
test**. A number without provenance is not a result (`docs/bench/README.md`),
so each gate cites the document and manifest that closed it — or states
plainly that it is open. The person deciding to go needs the open ones more
than the closed ones.

| # | Gate | State | Provenance |
| --- | --- | --- | --- |
| 1 | Replay parity (P4.6) | **PASS** | `docs/bench/timing-parity.md`, `docs/bench/timing-parity.manifest.json` |
| 2 | Steady state at modelled load (P4.3) | **PASS, with caveats below** | `docs/bench/2026-08-21-steady-state/summary.md` and its five manifests |
| 3 | Dropout and recovery ladder (P4.4) | **NOT RUN** | no `docs/bench/dropout.md` exists |
| 4 | Range and RF degradation (P4.5) | **NOT RUN** | no `docs/bench/range.md` exists |
| 5 | Commissioning report (P4.7) | **NOT WRITTEN** | this section is the stand-in, and says so |

### Gate 1 — replay parity: PASS

The full June-2025 event (24 h 39 m, 1 767 617 GPS fixes) replayed through
the real collectors, pipeline, timing engine, wire, leafnode, sourced
stream, ingest-writer and `v_laps`: **768 lap completions, 2 354 sector
crossings, 39 pit crossings, every one matching the predecessor's own
replay**, identical lap numbering, lap times agreeing to a median of
0.24 µs — one float64 ulp. The gate passed with no allowance used.
Scope caveat, restated from the source: it validates the GPS→timing path
and crosses no radio; CAN-side fidelity is P3.8's import.

### Gate 2 — steady state: PASS, with caveats

Three 35-minute bench runs at the modelled signal mix (tick 20 ms twice,
10 ms once): offered load within −4.7%…+3.1% of `docs/LINK_BUDGET.md` §3's
model, zero on every loss counter at both ends (`unknown_seq_batches`,
`bad_version_batches`, `dropped_flushes`, `samples_dropped`, `mqtt_drops`,
`slow_consumers`, `db_errors`), ingest `lag_ms` p95 under 30 ms, and the
5.3 s CAN fixture shown representative of a 33.6-minute run to 0.2%.

The caveats the bench could not close, restated rather than dropped:

- **Absolute source-to-row latency is unmeasured.** The UM980 held no fix
  indoors, so the fix-gated PPS stayed silent and `lag_ms` above is
  relative, not source-to-row. The under-500 ms claim is still a model.
- **Framing overhead is unresolved.** Two instrumented runs contradict each
  other (ratios 1.082 and a physically impossible 0.905); the leading
  hypothesis is leafnode S2 compression, unconfirmed. Do not quote either
  number.
- **Airtime efficiency at saturation is unmeasured.** The bench offered
  ~470 kbit/s to a 32.5 Mbit/s link at −13 dBm; §5's assumed 0.5 governs
  behaviour at range and has never met a measurement.
- **30 of 163 catalog channels were never exercised** — the PDM and keypad
  I/O (`car.boost_button` and kin). 18% of the mapped set is untested by
  every bench run; a defect confined to those channels would not have shown.
- **No pit-side wire measurement exists** (the pit stack ran inside a VM
  the host's counters cannot see).

### Gate 3 — dropout ladder: NOT RUN

Nobody has severed the RF path and measured recovery. What stands in its
place, stated for what it is worth and no more: the recovery *design* (ADR
0002 — JetStream sourcing resumes, `ingest_cursor` makes redelivery
idempotent), the container-level sever test
(`tests/test_deploy_topology.py`), and one unplanned full-scale event —
during the parity run the docker daemon restarted with ~3 M messages on the
pit stream, and restarting the ingest-writer was the entire recovery: zero
gaps, zero duplicates (`docs/bench/timing-parity.md` → "An unplanned
durability test"). **Unmeasured:** catch-up drain rate while live traffic
continues, the vehicle-side loss boundary (`TELE` is capped at 8 GiB /
72 h; the publisher queue sheds oldest-first past its byte budget), and
behaviour across the 2-minute dedup window.

### Gate 4 — range: NOT RUN

−13 dBm is two radios in one room. The operational range limit for
full-rate telemetry — the RSSI/MCS at which the sourcing backlog stops
recovering — is unknown, and the §7 levers are unmeasured. The first
sessions will sample real range whether anyone plans to or not; §5 below
says what to record.

### The decision this leaves

Gates 1 and 2 say the stack computes the right numbers and carries the
modelled load without loss. Gates 3 and 4 bound what you can *rely on* —
the live view at range and under dropouts — not what you can *lose*: per
§2, data survives every pit-side failure. With no old stack in the loop,
"go" is not a one-way door; it is the decision to run a session on the new
stack, and the worst credible outcome is listed below. The call is the pit
operator's, and there is only one of those.

---

## 2. If it goes badly

**There is no tested rollback. There is no rollback at all.** The old stack
is out of scope by owner direction; nothing in this document or any other
switches back to it, and its bootability was already not being relied upon
(ADR 0001, amendment 2026-08-22).

What actually protects the session: **the car records locally and durably
regardless of what the pit is doing.** The vehicle's JetStream file store
(`TELE`, 8 GiB / 72 h) keeps every batch on the SBC's disk; the pit sources
from it when the link allows and drains the backlog when it returns, and
the ingest cursor makes that resumption idempotent — the mechanism the
parity run's unplanned daemon restart exercised at full scale with zero
loss. A dead pit, a dead radio, or a botched bring-up therefore costs the
**live view**, not the data.

So the honest failure ladder is:

1. **Pit degraded or down** → run the session anyway; the car is recording.
   Fix the pit, and sourcing catches up on its own. Do not restart the
   vehicle stack to fix a pit problem.
2. **Vehicle stack down** → that is the one loss scenario. The agent
   restarts via compose (`restart: unless-stopped`) and re-registers on its
   own; an SBC that will not boot ends telemetry for the session, exactly
   as it would have on any stack.
3. **In doubt mid-session** → leave the car alone, note the time, read
   `/health` at the pit afterwards. Every counter above is cumulative and
   will still be there.

---

## 3. The sequence

Three stages: a full-stack bench rehearsal with sample data (§3.1), the
car installation (§3.2), and first power-on with the car in it (§3.3).
Durations are bench-measured where stated, otherwise honest estimates.

### 3.1 Bench rehearsal with sample data — do this first (~1 h)

The whole point of the fresh-stack framing: prove the complete pit stack
end-to-end with recorded data before any of it meets a car. This is
the deployment guide's replay path, run as a drill.

1. **Bring up the pit stack** as described in [Pit installation](operations/pit.md)
   (`docker compose -f deploy/pit-compose.yaml up -d`), with a
   populated `.env`. **The first bring-up needs internet** — Grafana
   fetches its one plugin into the `grafana-data` volume; a fresh volume on
   a disconnected pit is a Grafana that does not come up. Warm it now, not
   at the track. (~2 min once images are built; the first build is longer.)

2. **Replay recorded telemetry through the real agent pipeline** into a
   local vehicle-side nats-server, or run the measurement bench of
   `docs/BENCH_RUNBOOK.md` if the bench hardware is set up:

   ```bash
   uv run tools/replay.py --server nats://127.0.0.1:4222 --rate 1.0 --loop
   ```

   > **Know what replay's default fixtures look like on a dashboard, or
   > you will debug a healthy stack.** Each `--loop` cycle is the ~110 s
   > GPS trace with the whole 30 s engine-start candump fixture replayed
   > once at its head. On the gauges that is `car.*` alive for ~30 seconds
   > out of every ~2 minutes and dead in between, and at the ingest-writer
   > it is a **constant** `wall_lag` of ~30 s — the paced publish runs
   > behind by the length of the CAN prelude. Both are artefacts of the
   > harness, not faults in the stack; the tell for a *real* problem is a
   > lag that climbs without resetting or `num_pending` growing on the pit
   > consumer. For a continuous full-mix rehearsal — every gauge live at
   > steady state, ~3,000–3,100 samples/s (the current fixtures predict
   > 3,015.4/s offered; the 2026-08-21 P4.3 runs measured 3,091.0/s into
   > `v_samples_named`) — run the measurement bench instead
   > (`docs/BENCH_RUNBOOK.md` §5–§6: `vcan0`, two looped `canplayer`s,
   > `bench_gps`, the real agent), which is the configuration every P4.3
   > number was measured on.

3. **Verify every hop** using [Verify the stack](operations/verification.md), in
   order: `leafs` is 1 at both ends, `TELE_VEHICLE` climbing toward `TELE`,
   `tools/decode.py` resolving real names, `mosquitto_sub` showing JSON,
   rows landing in `v_samples_named`, all enabled `/health` endpoints
   answering, Grafana and both datasources (`timescale`, `mqtt-live`)
   green. (~10 min)

4. **Exercise the surfaces a session will use.** Open the car dashboard
   (Grafana `:3000`, folder `openlaps`, dashboard `Car and engine`, uid
   `car`) and watch live gauges move; open the session UI (session-control,
   `:8080`), start a session, change driver, end it. Synthetic laps, if
   wanted, via `tools/lap_simulator.py` — it labels everything `<track>_sim`
   so it is trivially deletable afterwards.

5. **Read the health counters before calling it done.** All of
   `unknown_seq_batches`, `bad_version_batches`, `dropped_flushes`,
   `samples_dropped`, `db_errors` zero on `:8081/health`; `mqtt_drops` and
   `aggregate_sheds` zero and `unmatched_rules` empty on `:8082/health`;
   `stalled` false. A rehearsal that ends with a non-zero counter is a
   rehearsal that found something — chase it now, on the bench, where it is
   cheap.

### 3.2 What gets installed on the car, in what order

Everything vehicle-side, per [Vehicle installation](operations/vehicle.md) — none of it
new, all of it bench-run:

1. **The SBC image**: a checkout, `uv sync`, and the docker image
   (`docker build -f deploy/Dockerfile -t openlaps:local .`). (~5 min)
2. **Clock chain**: chrony (`deploy/chrony/vehicle.conf`), the flashed
   RP2040 timing head (`firmware/timing-head/README.md`), and the shim
   unit. Bench-verified — but **the fix-gated PPS has never had a fix**;
   the first sky view is the first real test of it (§5).
3. **CAN**: `can0` up on the host before the agent —
   `sudo ip link set can0 up type can bitrate 1000000`. The agent
   configures no bitrate; that is host state, and forgetting it presents
   as a silent, healthy-looking agent with no `car.*` channels.
4. **The HaLow radio**, powered and associated.
5. **The vehicle stack**: `docker compose -f deploy/vehicle-compose.yaml
   up -d`, then confirm `TELE` and `CMD` exist via
   `nats --server nats://127.0.0.1:4222 stream ls`. If the agent is run
   under systemd instead (`deploy/systemd/openlaps-agent.service`), run
   one or the other, never both — they fight over the serial port.

### 3.3 First power-on with the car (~30 min before first session)

In this order; each hop that fails invalidates everything after it, so do
not skip ahead.

1. **Vehicle up** (§3.2 step 5) and streams present. (~2 min)
2. **Clock**: `chronyc sources -v` on the SBC shows `GPS` selected once the
   receiver has a fix. If it does not, the session still runs —
   `sys.host.clock_*` telemetry records what the clock was doing, and
   internet NTP (if present) or holdover carries it. Note it and move on.
3. **Pit up**: `docker compose -f deploy/pit-compose.yaml up -d` on the
   pit host (chrony already installed per `deploy/README.md`). `depends_on`
   sequences the migrator and stream provisioner; watch both run to
   completion. (~2 min)
4. **Every hop**, same list as §3.1 step 3, now across the real radio.
   (~10 min)
5. **Control reaches the car**: start a session from the session UI on
   `:8080` (or the `curl` in `deploy/README.md`) and confirm the command
   arrives — `nats --server nats://<vehicle-host>:4222 stream view CMD`.
6. **Dashboard sanity**: gauges moving at `:3000`, and lap rows appearing
   once the car crosses the line.

---

### 3.4 Phones: the alert path, end to end (~10 min, before the car leaves the trailer)

The single most important line in `docs/plan/PHASE7.md`. An alert path
that worked on the bench and has never been seen to reach a phone in the
garage is decoration.

1. **The pit stack is up with `ntfy` and `notifier` healthy**
   (`curl -fsS http://127.0.0.1:8087/v1/health`, `:8086/health`), and
   `OPENLAPS_NOTIFIER_PUBLIC_URL` and `OPENLAPS_NTFY_PUBLIC_URL` in the
   pit's `.env` name the pit host's **LAN address**, not localhost — a
   phone cannot follow `localhost` anywhere.
2. **On every phone that is meant to hear an alert:** install the ntfy
   app (F-Droid, Play, App Store), add the server
   `http://<pit-host>:8087`, subscribe to `openlaps-critical` and — for
   the engineers, not the drivers — `openlaps-warning`. On Android, turn
   on **instant delivery** for the self-hosted server in the app's
   settings, or notifications arrive when Android feels like it -- the
   persistent "listening for incoming notifications" entry is the proof
   it is on, and the app's battery-optimisation prompt must be accepted.
   Join the pit wifi. **iPhones** are different: iOS allows no
   background connection, so they only get a push if
   `OPENLAPS_NTFY_UPSTREAM_URL` is set (`example.env`), the pit has
   internet at that moment, and the phone can reach Apple's push
   service -- and the server entry in the app must match
   `OPENLAPS_NTFY_PUBLIC_URL` exactly, since the relay topic is a hash
   of it. No ntfy.sh account is needed, and the alert content never
   leaves the LAN; only a wake-up does.

   **Therefore: the wall gets an Android.** Whoever holds the pit wall
   carries an Android phone (or a tablet left on the wall) with instant
   delivery on. That device is the one path that needs no internet and
   no Apple, and it is the one that must buzz in step 4. iPhones are
   welcome as extras, never as the only phone subscribed.
3. **Open the annunciator** at `http://<pit-host>:8086/`, enable sound,
   and confirm the header reads **path alive**. If it does not within a
   minute, Grafana is not sending — fix that before anything else.
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
proceed on "it probably works".

## 4. Decision points

What specifically would make the operator stop, and what stopping means.
**The call is the pit operator's.** There is no second role; the escalation
path is a decision, not a phone call. Remember §2 before aborting anything:
an abort costs the live view, never the recording.

| Seeing | Meaning | Do |
| --- | --- | --- |
| `leafs` 0 at either end after bring-up | radio or leafnode config; nothing crosses | Fix before the session if time allows; otherwise **run without live telemetry** — the car records regardless |
| `TELE` exists but `TELE_VEHICLE` not climbing | sourcing broken (domain string, provisioner) | Same as above; pit-side fix, car untouched |
| `unknown_seq_batches` climbing at `:8081/health` | pit has not seen this registry generation | Wait — the writer rescans and recovers on its own. Do not clean-slate a car; that procedure (`docs/BENCH_RUNBOOK.md` §8) is for benches |
| `agent_status_age_s` climbing past ~10 s | vehicle agent stopped publishing | Check the agent container/unit on the SBC. This is the one stop-the-car item, and only because the car is not recording either |
| `stalled: true` or `db_errors` climbing | pit database trouble | Session runs; fix the pit afterwards, sourcing catches up |
| Gauges frozen but SQL panels advancing | live-decoder or mosquitto only | Cosmetic; note it, fix after |
| `lag_ms` growing without recovering | link cannot carry the load at this range | Expected beyond the unmeasured range limit (gate 4). The backlog drains when the car comes closer; watch, record, do not restart anything |

**Mid-session, the default action is no action.** Every recovery path in
this stack is designed to work unattended; the failure mode a restart
introduces is worse than the lag it is meant to cure.

---

## 5. After the first session

The handful of things worth reading once real data has been through the
stack — each one either closes a caveat from §1 or tunes a knob that has
only ever met a model. Record numbers with their provenance; a number
without it is not a result.

1. **Ingest lag at session length.** `lag_ms` and `wall_lag_ms` on
   `:8081/health` — the first `wall_lag_ms` with a GPS-disciplined vehicle
   clock is the first real source-to-row latency figure this project has
   (gate 2's first caveat).
2. **Was anything rate-capped harder than intended?** `suppressed` per
   channel and `aggregate_sheds` on `:8082/health`. Non-zero
   `aggregate_sheds` means the valve acted; per-channel `suppressed` says
   which gauges were starved. The fix is `deploy/pit-config/live-decoder.yaml`,
   reloaded live — no restart, nothing on the car.
3. **Are the trace panels usable at session length?** Open the `Car and
   engine` dashboard over the whole session's range. If panels crawl, that
   is the trace read surface's first real test failing — note the range and
   panel, it is a view/aggregate problem, not a Grafana setting.
4. **Did the silent 30 wake up?** The first real button press exercises
   channels no bench run ever did. Spot-check via the pit database, e.g.:

   ```bash
   docker compose -f deploy/pit-compose.yaml exec timescaledb psql -U openlaps -d openlaps -c "SELECT count(*) FROM v_samples_named WHERE channel = 'car.boost_button';"
   ```

5. **The radio, at real range.** `slow_consumers` on both `/varz`
   endpoints, `nats_reconnects` on the pit health surfaces, and whether
   `lag_ms` excursions map to where the car was on track. This is gate 4's
   data arriving for free; write down RSSI if the routers were reachable.
6. **`reconnects`** on `:8083/health`, if RTK ran — the probe's
   `ntrip_reconnects` series moved on the bench (4 in 35 min) and nobody
   knows what a normal number looks like yet.
7. **Expect `catalog.yaml`'s RBE and rate caps to want tuning** — and
   expect that to be a config push, not a code change. That is the claim
   the catalog design makes; the first session is where it starts being
   held to it.
