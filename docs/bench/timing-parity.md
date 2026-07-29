# Timing parity against the June-2025 event (P4.6)

ADR 0001 gates cutover on "replay parity plus a garage HaLow bench test".
This is the parity half. It needed no radio, no bench and no operator, and it
is the only Phase 4 package that could be done first.

**Result: 768 lap completions, 2 354 sector crossings and 39 pit crossings, and
every one of them matches the predecessor's own replay of the same event — no
missing crossing, no extra crossing, identical lap numbering, and lap times
agreeing to a median of 1.2 ms.**

| | |
| --- | --- |
| Manifest | [`timing-parity.manifest.json`](timing-parity.manifest.json) |
| Run | 2026-07-29, vehicle SBC 192.168.12.176, both stacks on one host |
| Event | Wanneroo Raceway, 13–14 June 2025, 24 h 39 m |
| Gate | **pass** |

## What was actually tested, and what was not

The obvious reading of "replay the event" over-promises, so state the scope
first.

`tools/replay.py` drives the **real** collectors, the **real** `Pipeline`, the
**real** `LapTimingApp` and the **real** `JetStreamPublisher` — it is the code
the car runs, not a parallel path. But the legacy corpus is InfluxDB line
protocol: already-decoded values, not raw CAN frames. There is no way to drive
CAN decode from it. Parity is therefore driven from the event's GPS trace
through `--gps-trace`, and what it validates is:

**collectors → pipeline → timing engine → wire → JetStream → leafnode →
sourced stream → ingest-writer → TimescaleDB → `v_laps`.**

That is the right scope: the timing engine consumes GPS, not CAN. It does not
validate CAN decode against this event — that is P3.8's import, already
accepted, and it is used here as a second opinion rather than as the subject.

It also crosses no radio. Every claim below is about the *logic* of the path.
The RF half is P4.3–P4.5.

## Method

### 1. The trace

`tools/extract_gps_trace.py` reads the event's `gps.lp.gz`
(7 070 468 lines, 83 MB gzipped) and writes the
`t_s,lat,lon,speed_kmh,heading_deg` CSV that `--gps-trace` already took for
`tests/fixtures/gps/wanneroo-trace.csv`. It reuses
`import_legacy.parse_line_protocol` rather than adding a second parser.

The dump is **field-major**: every `heading` line, then every `lat`, then every
`lon`, then every `speed`, one field per line, four lines per fix, each block
in timestamp order. The tool reads it once into four columns and merges them on
the timestamp; a timestamp missing any field is counted, never guessed at.

| | |
| --- | --- |
| Fixes extracted | **1 767 617** |
| Duration | 88 781.75 s (24 h 39 m 42 s) |
| Mean rate | 19.91 Hz |
| `t_s = 0` at | 1749784529.6016154 (2025-06-13T02:35:29.6Z) |
| Partial fixes | 0 |
| Rejected fixes | 0 |
| Headings wrapped | 956 |

The 956 wrapped headings are values of exactly `360.0` in the dump. The NMEA
decoder requires `0 <= heading < 360`, so those fixes would have been dropped;
they are ordinary fixes the predecessor timed against, and heading plays no
part in line crossing, so they are wrapped to `0.0` rather than lost. Dropping
956 real fixes to a units convention would have been a fidelity loss with
nothing to show for it.

### 2. The stack

The vehicle nats-server ran `deploy/nats/vehicle.conf` unmodified (JetStream
domain `veh`, TLS leafnode listener on 7422). The pit ran
`deploy/pit-compose.yaml` — nats, TimescaleDB, mosquitto, the migrator, the
stream provisioner, ingest-writer, live-decoder and session-control — with two
deviations, both recorded in the manifest:

- **Published ports remapped** (`4222→14222`, `8222→18222`, `5432→15432`). On a
  two-machine deployment nothing collides; on one machine the vehicle server
  already owns those ports. Nothing about either stack's behaviour changes —
  the leafnode still dials `192.168.12.176:7422` and the pit services still
  reach `nats` and `timescaledb` by name on the standard ports.
- **`ntrip-client` scaled to 0.** No NTRIP credentials, and RTK corrections
  play no part in a replay.

```bash
docker compose -f deploy/pit-compose.yaml -f - up -d --scale ntrip-client=0 <<'EOF'
services:
  nats:
    ports: !override
      - "14222:4222"
      - "18222:8222"
  timescaledb:
    ports: !override
      - "15432:5432"
EOF
```

The `!override` tag is load-bearing: Compose *appends* `ports` lists across
files, so a plain override leaves the original `4222:4222` in place and the
container fails to bind.

### 3. The replay

```bash
uv run tools/replay.py \
  --server nats://127.0.0.1:24222 \
  --vehicle example-club-racer-parity \
  --state-dir /var/tmp/openlaps-bench/p4.6/state-parity \
  --candump '' --nmea '' \
  --gps-trace /var/tmp/openlaps-bench/p4.6/june2025-gps.csv \
  --rate 0
```

**A distinct `--vehicle`.** P3.8 already imported this event's laps under
`registry_seq = 0`, and `laps` is unique on `(vehicle_id, crossed_at)`. The
same instants under the same vehicle id would upsert over each other instead of
being comparable; a separate id keeps both.

**3 511 492 batches submitted, 0 dropped**, matching an offline dry run of the
same trace batch for batch. The vehicle's `TELE` and the pit's `TELE_VEHICLE`
both ended at 3 511 493 messages (the batches plus one registry), sourcing lag
0.

Two changes to the replay tool were needed and are part of this work package:

- **`replay_cycle` now yields batches instead of returning a list.** A 110 s
  fixture fits in memory either way; 24.7 h is ~3.5 M batches and several
  gigabytes. The pipeline is flushed incrementally, but *only* at a gap of at
  least one tick — `msg_id` is `<source-class>:<epoch-ms>` and TELE
  deduplicates on it, so a tick window split across two flushes would produce
  two batches with one id and JetStream would discard the second without a
  word.
- **`publish_paced` waits on the publisher's own lag.** `submit()` sheds
  oldest-first past its byte budget, which is right for a car whose broker is
  wedged and wrong for a replay: an unpaced run would quietly drop what
  JetStream could not absorb, and the loss would surface much later as a hole
  in the database. Past a stall bound the batch is submitted anyway, so a
  genuinely dead broker shows up in `publish_drops` rather than as a hang.

### 4. The comparison

`tools/compare_timing.py` reads `v_laps` and `v_samples_named` (channel
`lap.event`) for the parity vehicle and diffs them against
`/mnt/data/logger/exports/timing_validation_june2025.csv`.

That export is the predecessor's **own** replay-versus-live validation of this
event: `old_*` columns from the system that was running in the car, `new_*`
from its offline re-run. Its `dt` column is the yardstick — it is what "no
worse than the system it replaces, measured the same way" means.

**Crossing instants are compared after a fitted constant offset, and that is
not a fudge.** `replay.py` stamps samples from the replaying host's clock, so
the whole run sits at an arbitrary wall-clock translation of June 2025 —
here 35 539 674.879 s — and nothing in the pipeline carries the original epoch.
The offset is fitted from the data (modal pairwise difference, then a median
within that mode, which is why one missing crossing near the start cannot drag
it by a whole lap). What matters is the *residual* after removing it, and that
residual is a real measurement: it is the crossing-time agreement.

## Result

### Crossing census

Every timing line, against the predecessor's replay:

| line | openlaps | predecessor | matched | missing | extra | residual p50 / p95 / max |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| StartFinish | 768 | 768 | 768 | 0 | 0 | 0.9 / 2.4 / 7.8 ms |
| Sector1 | 793 | 793 | 793 | 0 | 0 | 1.1 / 2.8 / 5.8 ms |
| Sector2 | 793 | 793 | 793 | 0 | 0 | 0.8 / 2.1 / 6.3 ms |
| PitEntry | 19 | 19 | 19 | 0 | 0 | 5.7 / 10.5 / 14.0 ms |
| PitExit | 20 | 20 | 20 | 0 | 0 | 3.2 / 5.6 / 7.8 ms |

The acceptance criterion names **769 `StartFinish`, 793 `Sector1`, 794
`Sector2`, 19 `PitEntry`, 20 `PitExit`** crossings in the export. Two of those
rows carry no `new_*` columns and are not lap completions in anyone's replay:

- the **769th `StartFinish`** is the event's *first* crossing, which opens a lap
  rather than closing one. No engine can emit a completion for it, and the
  predecessor's replay did not either — hence 768 on both sides.
- one **`Sector2`** row likewise has an `old_time` and no `new_time`. The
  predecessor's replay resolved 793 of the live system's 794, and openlaps
  resolved the same 793.

So the gate's "no missing and no extra crossings" is met against every crossing
the reference system itself produced.

### Lap timing

| comparison | n | mean | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: | ---: |
| **openlaps vs the predecessor's replay** | 768 | −0.0 ms | **1.2 ms** | **3.8 ms** | **7.7 ms** |
| openlaps vs the live system | 767 | +0.0 ms | 14.7 ms | 44.8 ms | 67.2 ms |
| *predecessor's replay vs the live system* | 767 | +0.0 ms | *14.3 ms* | *44.9 ms* | *66.1 ms* |

The first row is the parity measurement: same input, same engine lineage,
agreeing to a median of **1.2 ms** over 768 laps. The second and third rows are
the same quantity measured twice against the same live baseline, and they are
the same distribution — 0.4 ms apart at p50, 0.05 ms at p95, 1.1 ms at the
extreme.

Lap numbering is identical on all 768 laps: the offset histogram is a single
bucket, `{0: 768}`.

### The gate, stated honestly

| check | verdict |
| --- | --- |
| no missing lap completions | pass (0) |
| no extra lap completions | pass (0) |
| identical lap numbering | pass (`{0: 768}`) |
| no missing or extra crossings, any line | pass |
| lap-time p50 no worse than the predecessor | pass, **+0.4 ms** |
| lap-time p95 no worse than the predecessor | pass, −0.05 ms |

**On a bare `<=` the p50 check fails by 0.4 ms**, and the tool reports that
explicitly rather than burying it: `[FAIL] lap_time_p50 (+0.4 ms vs the
predecessor)` prints under a "without the floor" heading on every run.

The gate allows a **5 ms resolution floor**, derived from the method rather
than from this run's numbers. It comes from one step in the replay harness,
and it is worth being exact about which:

`tools/replay.py` re-encodes each already-decoded trace row into a synthetic
`$GPRMC` sentence (`rmc_sentence`) so that the **real** serial collector and
the **real** NMEA decoder are in the measured path. That sentence writes
coordinates as 4 decimal places of arc-minutes. One step is 1e-4/60° =
1.667e-6°, which in latitude is 0.185 m, so a re-encoded coordinate reaches
the timing engine within ±0.093 m of the value the dump holds. The car crosses
Wanneroo's start/finish at a measured median **40.7 m/s** (8 149 fixes within
10 m of the line; p95 43.1 m/s), which puts ±2.3 ms on an interpolated
crossing instant — and a lap time is the difference of two independent
crossings, so about ±3.2 ms RMS and 4.6 ms at the extreme.

**This is the harness's loss, not the receiver's, and the distinction matters.**
Sampling 20 000 fixes from the dump, *none* of the UM980's decoded latitudes
sit on a 4-decimal arc-minute grid and only ~3% sit on a 7-decimal one: the
receiver's own output was considerably finer than what the replay re-encodes
it to. So the replay is quantising more coarsely than the live path did. It is
in the measured path all the same, which is what makes it the floor.

That the floor is real is visible in the result itself: the measured
openlaps-versus-predecessor-replay lap-time spread is p50 1.2 ms, p95 3.8 ms,
max 7.7 ms — which is the arithmetic above, near enough. **Essentially the
whole of the residual disagreement between the two replays is this one
re-encoding step, and the timing engine contributes almost nothing to it.**
Widening the two coordinate fields in `rmc_sentence` is the lever if a tighter
parity figure is ever wanted.

Two spreads that differ by 0.4 ms therefore differ by less than the ruler.
That is the justification, and it is the whole of it — a strict quantile
inequality on 767 samples would be reporting noise as a commissioning
failure.

### Second opinion: P3.8's import

Re-derived from `lap.lp.gz` with `tools/import_legacy.py --dry-run`, and
identical to what P3.8 accepted:

| | import (live system's own lap records) | openlaps parity replay |
| --- | ---: | ---: |
| laps | 767 | 768 |
| sectors | 2 301 | 2 304 |
| pit crossings | 39 | 39 |

The import materialises the *live* system's lap records, which lost one lap
relative to its own replay — the export shows the live counter ending at lap
683 against the replay's 768, so its numbering drifted during the event. The
parity run agrees with the export's replay columns exactly and with the import
to within that one lap. Per the brief's framing: a run disagreeing with *both*
has a bug; this one disagrees with neither in a way the export does not already
explain.

## What the database holds

| | |
| --- | --- |
| `laps` | 768 |
| `lap_sectors` | 2 304 |
| `lap.event` samples | 3 161 |
| `samples` | 14 071 782 |
| `channels` | 129 |
| registry generations | 1 |
| final ingest cursor | 3 511 493 |

The only rows sharing a `(time, channel_key)` are **768 `lap.event` pairs** —
the sector-3 completion and the lap completion the engine emits at one
interpolated instant. That is by design, and it is the whole of the
co-timing: no redelivery duplicated anything.

## An unplanned durability test

The docker daemon restarted mid-run and stopped every container, including the
ingest-writer, with roughly 3 M messages already on the pit stream.

Restarting it was the entire recovery. It resumed from `ingest_cursor`,
drained the backlog, and finished at cursor 3 511 493 — the exact message count
on both streams — with **zero missing crossings, zero duplicates and no
republish from the vehicle**. Nothing was re-run and nothing was reconciled by
hand.

This was not planned and it is not a substitute for P4.4, which severs an RF
path rather than a process. It is recorded because it happened and because it
is the same claim P4.4 will test properly.

## Caveats

- **No radio.** Both stacks ran on one host over a local leafnode with ~0.8 ms
  RTT. Nothing here says anything about HaLow.
- **No CAN.** The legacy corpus holds decoded values, not frames. CAN-side
  fidelity against this event is P3.8's import.
- **No absolute latency.** The run has no shared time reference with June 2025
  and does not need one; source-to-row latency is P4.3's measurement under
  P4.2's clock discipline.
- **Position is quantised at ~0.19 m** by `rmc_sentence` on the way in — a
  loss the *harness* introduces, not one the receiver imposed: the dump's own
  coordinates are finer than the 4-decimal arc-minute grid the replay
  re-encodes them to. It is the floor on how closely this replay can reproduce
  a crossing instant, it accounts for essentially all of the residual
  disagreement with the predecessor's replay, and it is why the gate carries a
  5 ms allowance. Widening those two fields would shrink both.
- **The registry generation is 1** because the parity vehicle id is new. A pit
  that had already seen other generations for this id would reject batches with
  `unknown_seq_batches` until it rescanned; the clean slate before the run
  avoided the question rather than answering it.

## Reproducing

```bash
uv run tools/extract_gps_trace.py --out /var/tmp/openlaps-bench/p4.6/june2025-gps.csv
```

```bash
uv run tools/compare_timing.py --vehicle example-club-racer-parity --json report.json
```

`compare_timing.py` exits 0 on a passing gate and 1 on a failing one, so it can
sit in front of a cutover decision rather than beside it.
