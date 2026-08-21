# P4.3 steady state — 2026-08-21, tick 20 ms

One 35-minute run at `OPENLAPS_TICK_MS=20`, on the bench profile with
replayed CAN and synthetic GPS. **This is half of P4.3**: the brief calls for
a run at 20 ms and one at 10 ms, and the 10 ms series has not been run.

Provenance: `t20-vehicle.manifest.json`, `t20-pit.manifest.json`. Both record
`git_sha a5ffb85` with `git_dirty: true` — an untracked `tools/rp2040_bootloader.sh`
was in the tree; no tracked file differed. Catalog `3abb3d5a…`, profile
`profiles/example-club-racer-bench`, 2101 samples per side, 2101 merged rows
with zero unmatched.

## Offered load at the NATS layer

`LINK_BUDGET.md` §3 models **544.4 kbit/s** at 20 ms.

| window | samples | mean | p50 | vs modelled |
| --- | ---: | ---: | ---: | ---: |
| all samples | 2100 | 507.6 kbit/s | 529.6 | **−6.8%** |
| load only (>100 kbit/s) | 2015 | **529.0 kbit/s** | 529.7 | **−2.8%** |

**Quote −2.8%.** The all-sample mean is diluted by the baseline periods §9
mandates — the probe starts 15 s before the load and runs 60 s after it stops,
and those 85 near-zero samples pull the mean down without saying anything
about steady state. The median over all samples (529.6) agrees with the
load-window mean (529.0) to within 0.7 kbit/s, so either is a sound estimator;
the arithmetic mean over the full CSV is not.

`tools/link_probe.py --summary` prints only the diluted figure. Worth either
teaching it the load window or noting in the runbook that the median is the
number to cite.

The model is confirmed to within 3% at 20 ms. §7's claim that tick length is
a weak lever cannot be tested until the 10 ms run exists.

## The reverse channel

§8's third caveat, reported first-class:

| series | mean | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: |
| `rev_nats_kbit_s` | 16.6 | 16.8 | 22.4 | 44.0 |
| reverse ÷ forward | 0.031 | 0.032 | 0.042 | 0.083 |
| ntrip bytes/s | 2159.7 | 2098.3 | 3045.8 | 5326.6 |

The reverse direction runs at **~3.1% of forward** and is dominated by RTCM
carried pit→vehicle, not by protocol acknowledgement. That is consistent with
§8's claim that the sourcing/ack pattern is structurally different from the
predecessor's per-message QoS-1 PUBACKs, whose reverse traffic scaled with
forward message count. Here forward message rate is ~150/s while the reverse
is a near-constant correction stream. An explicit numeric comparison against
the predecessor baseline is still owed.

## Ingest and pipeline health

| series | mean | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: |
| `ingest_rows_per_s` | 2965.3 | 3118.5 | 3174.8 | 4806.9 |
| `ingest_lag_ms` | 19.6 | 21.1 | 28.1 | 50.5 |
| `ingest_wall_lag_ms` | −28.3 | −28.4 | −22.3 | −1.2 |
| `flushes_per_s` | 4.74 | 4.99 | 5.00 | 6.00 |
| `consumer_num_pending` | 0.33 | 0.00 | 3.00 | 42.00 |

**6,228,235 samples, 18 laps, 54 sectors** written. Zero on every error
counter for the whole run: `unknown_seq_batches`, `bad_version_batches`,
`dropped_flushes`, `samples_dropped`, `db_errors`, `num_redelivered`,
`slow_consumers`. `consumer_num_pending` at p50 0 means the pit tracked live
throughout rather than draining a backlog.

Measured ingest at p50 3118.5 rows/s against the predicted mix of 3037.8
samples/s is **+2.7%** — the bench delivered slightly more than
`bench_check --predict` expects, well inside the loop-seam noise of a
replayed fixture.

## What this run does not answer

Four of P4.3's six required outputs are still open, three of them for reasons
outside the run:

- **Wire bytes on `:7422`, both directions**, against §2's 6–13% framing
  estimate. `nft` counters are unavailable — `sudo -n nft` is refused for the
  operator's user on both hosts, so `wire_source` is `procnetdev` and the
  counters are whole-interface totals that cannot be attributed to the
  leafnode port. The framing estimate still has not met a measurement.
- **Airtime efficiency** (`wire goodput ÷ PHY rate`), replacing §5's assumed
  0.5. Not measurable: no radio in the path. The vehicle reaches the pit over
  wired `enp2s0`, and the pit reaches back through a NAT on `eth0`.
- **Absolute source-to-row latency** against the under-500 ms target. Needs
  P4.8's GNSS discipline, which was not in effect: the UM980 held no fix
  (RMC status `V`, one satellite at 22 dB-Hz), so the fix-gated PPS was silent
  and chrony ran on holdover plus NTP. Measured inter-host offset was
  **51.9 ms ±11.2**, which is far too coarse for a 500 ms target to be
  asserted honestly. `ingest_wall_lag_ms` (−28.3 mean) is a proxy for the
  writer's own lag only, and carries that offset inside it.
- **Signal-mix ground truth** from `v_samples_named` over the full run,
  against the 5.3 s fixture's assumed mix. The data exists — 6.2 M rows are
  in TimescaleDB — and this analysis has not yet been done.

## Also observed

- **live-decoder published nothing for the entire run** (`publish_rate 0.0`,
  `registries 0`, no consumer on `TELE_VEHICLE`, no errors logged), as it did
  in the discarded run before it. Its configuration is correct. The MQTT
  branch of the pit is therefore untested by both runs. Under investigation.
- **`sourcing_backlog` reads ~4,520,452 and means nothing.** It is the gap
  between the two streams' sequence origins (5,304,449 − 784,003) after each
  was purged independently, and it is constant across the run. Anyone reading
  the summary cold will see it as a catastrophic backlog.
