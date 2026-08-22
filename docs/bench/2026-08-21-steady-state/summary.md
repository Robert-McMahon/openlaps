# P4.3 steady state — 2026-08-21

Three 35-minute runs on the bench profile with replayed CAN and synthetic
GPS: one at `OPENLAPS_TICK_MS=20`, one at `10`, as the brief requires, and a
second 20 ms pass (**t20b**) added afterwards to sample the radio at both
ends. All three ran registry generation 4 (163 channels, catalog
`3abb3d5a…`), so they are directly comparable.

Provenance: `t20-vehicle.manifest.json`, `t20-pit.manifest.json`,
`t10-vehicle.manifest.json`, `t20b-vehicle.manifest.json`,
`t20b-pit.manifest.json`. **There is no `t10-pit.manifest.json`** — the pit
probe failed to start for the 10 ms run (see `notes.md`), so that run is
vehicle-side only. Both headline results below are vehicle-side measurements
and are unaffected.

The t20b section near the end is where the radio numbers live, and it
carries two findings that bear on sections above it: a run-to-run spread at
one tick, and a framing-overhead measurement that contradicts the one in
"Framing overhead" below.

## Offered load, and the tick lever

`LINK_BUDGET.md` §3 models 544.4 kbit/s at 20 ms and 552.8 at 10 ms.

| run | load-window mean | p50 | n | vs modelled |
| --- | ---: | ---: | ---: | ---: |
| tick 20 ms | 529.0 kbit/s | 529.7 | 2015 | **−2.8%** |
| tick 10 ms | 570.2 kbit/s | 570.9 | 2015 | **+3.1%** |

Both figures are means over the load window — samples above 100 kbit/s —
not over the whole CSV. §9 requires the probes to bracket the load, so each
CSV carries ~85 near-zero baseline samples that dilute the arithmetic mean
without describing steady state (they pull t20 to 507.6 and t10 to 547.1).
The load-window mean and the all-sample median agree to within 1 kbit/s in
both runs. **`link_probe --summary` prints only the diluted figure**; cite the
table above.

The comparison §7 asked for:

```
modelled  20 -> 10 ms:  544.4 -> 552.8  =  +1.5%
measured  20 -> 10 ms:  529.0 -> 570.2  =  +7.8%
```

**§7's "tick is a weak lever" survives, but the model understates the
sensitivity by about fivefold.** Halving the tick cost 7.8% more offered load
where §3 predicts 1.5%. The mechanism is visible in the message rate:
forward messages went 147.2/s to 191.8/s rather than doubling, because GPS is
source-limited at 50 Hz and cannot fill the extra ticks. The additional bytes
are therefore mostly per-batch overhead — protobuf and NATS headers on
smaller batches — which §3 appears to under-count. Tick remains a weak lever
in absolute terms (under 8% across a 2× change) and this is now measured
rather than asserted.

## Framing overhead — §2's first measurement

Vehicle-side `nft` counters on `:7422` (`openlaps_leaf_out` / `_in`), 10 ms
run, load window, n=2015:

```
framing_overhead   mean 1.082   p50 1.082   ->  8.2% over NATS bytes
§2 estimated 6–13% unmodelled TCP/IP + 802.11 framing
```

**The estimate holds**, landing mid-range — but read it narrowly. The `nft`
counters sit on the host's Ethernet interface, so they count IP-layer bytes
*before* the HaLow bridge adds its own framing. This measures the TCP/IP half
of §2's 6–13% and not the 802.11ah half, which happens downstream of the
counters and is still unmeasured. It is the first time any part of that
figure has met a measurement.

The reverse direction behaves differently and should not be described by the
same ratio: `rev_wire` 40.4 kbit/s against `rev_nats` 16.5, roughly 2.4×,
because that path is mostly small acknowledgement packets where header
overhead dominates payload.

Not measured at 20 ms: that run predates the `nft` grant and fell back to
`/proc/net/dev`, so §2 currently has data at 10 ms only.

## The reverse channel

§8's third caveat, reported first-class:

| run | rev kbit/s (p50) | reverse ÷ forward | ntrip bytes/s (mean) |
| --- | ---: | ---: | ---: |
| tick 20 ms | 16.8 | 3.1% | 2159.7 |
| tick 10 ms | 16.6 | 2.9% | — (pit probe absent) |

The reverse rate is **flat across a 2× change in forward tick**, which is the
sharpest evidence yet for §8's claim that the sourcing/ack pattern is
"structurally different, not just smaller" than the predecessor's per-message
QoS-1 PUBACKs — those scaled with forward message count, and this does not.
It is dominated by RTCM carried pit→vehicle, not by protocol acknowledgement.
An explicit numeric comparison against the predecessor baseline is still owed.

## Ingest and pipeline health

Tick 20 (full probe coverage):

| series | mean | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: |
| `ingest_rows_per_s` | 2965.3 | 3118.5 | 3174.8 | 4806.9 |
| `ingest_lag_ms` | 19.6 | 21.1 | 28.1 | 50.5 |
| `consumer_num_pending` | 0.33 | 0.00 | 3.00 | 42.00 |

Tick 10 (counters only, no time series): **6,227,153 rows, 18 laps, 54
sectors**, against tick 20's 6,228,235 / 18 / 54. Zero on `unknown_seq_batches`,
`samples_dropped`, `dropped_flushes` and `db_errors` in both runs.

live-decoder published at ~100/s throughout the 10 ms run with zero
`mqtt_drops` and zero `aggregate_sheds`. It published **nothing** during the
20 ms run — it was broken then and fixed between the two — so the MQTT branch
of the pit is exercised at 10 ms only.

## Signal-mix ground truth

§8's first caveat is that the modelled rates come from a 5.3 s capture. The
10 ms run gives 2014.6 s of `v_samples_named` to check it against —
6,227,153 rows, 3091.0 samples/s measured.

| class | measured/s | predicted/s | delta |
| --- | ---: | ---: | ---: |
| can | 2031.9 | 2034.6 | −0.1% |
| imu | 751.4 | 752.5 | −0.1% |
| gps | 249.7 | 250.0 | −0.1% |
| host | 0.8 | 0.8 | +0.0% |
| derived | 57.2 | not modelled | — |
| **total** | **3091.0** | **3037.8** | **+1.8%** |

Per device: `haltech` 1263.0 (−0.1%), `haltech2` 691.0 (−0.2%), `imu` 751.4
(−0.1%), `wideband` 77.8 (−0.1%).

**The 5.3 s fixture predicts a 33.6-minute run to within 0.2%, class by class
and device by device.** §8's first caveat can be closed: the short capture is
representative, and the loop seam does not distort the offered mix.

The entire +1.8% total divergence is the derived class, which
`bench_check --predict` does not model. Measured, it breaks down as:

```
lap.*/timing.*   49.2/s        (profile README says ~17/s)
sys.agent.*       8.0/s        (README says ~13/s)
sys.host.*        0.8/s        (added by P4.8)
```

So the README's ~30/s estimate for unmodelled derived traffic is roughly half
the truth, and the error is concentrated in `lap.*`/`timing.*`. Corrected in
`profiles/example-club-racer-bench/README.md`.

### 30 catalog channels are never exercised

130 of the 163 registry channels produced samples. The 30 silent catalog
channels are all CAN — 26 on `haltech2`, 4 on `haltech` — and they are the
PDM and keypad I/O: `car.boost_button`, `car.hazard_light_button`,
`car.head_light_button`, `car.brake_light_current`, `car.fuel_pump_current`
and similar. Nothing pressed a button or energised an output during the 5.3 s
capture, so nothing does during a replay of it.

This is a blind spot rather than a fault: **18% of the mapped channel set is
untested by the bench at any tick**, and a defect confined to those channels
would not show in any run built on these fixtures. Worth stating in the
commissioning report rather than discovering later.

## t20b — the radio, sampled

A third run, 2026-08-21 23:26 → 2026-08-22 00:01 UTC, 2100 s, tick 20 ms
again. It exists because the two runs above crossed HaLow with
`--radio-adapter none` and collected no radio series at all; the ubus
adapter could not be used until the shell-quoting fix (PR #20) let the
probe's `ubus` JSON argument survive `ssh`. Provenance:
`t20b-vehicle.manifest.json`, `t20b-pit.manifest.json`. Both ends sampled,
2101 and 2100 samples, `pit_offset_s` −0.114 s against `--merge`'s 0.5 s
tolerance.

### The link, at last measured

Load window (n=2015), radio via `ubus`/`iwinfo` on both routers:

| | vehicle (`wlan1`) | pit (`wlan0`) |
| --- | ---: | ---: |
| signal | −13.4 dBm | −15.2 dBm |
| tx bitrate | 32.5 Mbit/s | 32.5 Mbit/s |
| rx bitrate | 32.5 Mbit/s | 32.5 Mbit/s |
| tx retries, whole run | +851 | +1014 |
| tx failed, whole run | +0 | +0 |

The MCS never moved off 32.5 Mbit/s in either direction, retries are a
rounding error against 128,364 transmitted packets, and nothing failed. This
is a link under no stress whatsoever.

### Airtime efficiency is still not measured, and this run cannot measure it

`link_probe` computes an `airtime_efficiency` series and it reads **0.016**.
**Do not cite that against §5's assumed 0.5.** It is wire goodput ÷ PHY
rate at the offered load, which is link *utilisation*; §5's 0.5 is the
fraction of PHY rate obtainable as goodput at **saturation**. This run
offers 469.4 kbit/s to a 32.5 Mbit/s link and never approaches saturation,
so it measures how little of the radio the telemetry uses, not how much of
the radio is usable.

What it does establish, and this is worth having: **about sixty times more
PHY rate than offered load, at −13 dBm with zero failed transmissions.**
That bounds the headroom question for the bench. It does not transfer to a
track — −13 dBm is two radios in one room, and §5's assumption governs
behaviour at the range where MCS actually drops.

### Framing overhead contradicts the 10 ms run

Vehicle-side `nft` named counters on `:7422`, `wire_source: nft` for all
2101 samples with an empty `reasons` field — the same instrumentation path
that produced §2's first measurement above.

```
forward   wire 118,243,713 B  ÷  leaf 130,698,113 B  =  0.905
reverse   wire   8,667,096 B  ÷  leaf   4,383,828 B  =  1.977
```

**The forward ratio is below 1, which is not physically possible** for a
like-for-like comparison: wire bytes carry the application bytes plus TCP,
IP and Ethernet headers, so the ratio has a floor above 1. Something between
the two counts is not what it appears to be, and until it is identified
**neither this run's 0.905 nor the 1.082 in "Framing overhead" above should
be quoted as the framing overhead of this link.**

Ruled out: `deploy/nats/vehicle.conf` and `pit.conf` configure no
compression, and the direction mapping in `deploy/nft/bench-vehicle.nft` is
correct (`sport 7422` → `openlaps_leaf_out`), which the reverse ratio's
plausible 1.977 independently supports — small ack packets, header
dominated, exactly as expected.

Leading hypothesis, **unconfirmed**: nats-server negotiates S2 compression
on leafnodes whether or not the config mentions it. Reading `/leafz` on the
pit after the run reports `"compression": "s2_uncompressed"` at an idle RTT
of 9.9 ms — and `s2_auto` selects its mode from RTT. The vehicle reported
`leaf_rtt_ms` of 82.0 for the entire run. If the connection sat in a
compressed mode while loaded, compressible protobuf batches would put wire
below application bytes in the forward direction and leave the
incompressible reverse direction alone, which is exactly the shape of the
two ratios. **The check is to read `/leafz` during a loaded run, not after
one**, and nobody has done that.

If it holds, framing overhead is not a constant of this link at all — it is
a function of whichever S2 mode the leafnode happens to be in, and §2 needs
rewriting around that rather than around a single number.

### An instrument reading that is not a measurement

`vehicle_leaf_rtt_ms` is **81.996 for all 2015 load-window samples**, to
three decimal places, while the pit's own `leaf_rtt_ms` varied across
8.6–12.0 over the same period. A value that constant across 35 minutes is a
cached figure, not a measurement. It is load-bearing for the compression
hypothesis above, so it needs resolving before that hypothesis is tested on
it.

### Pit health, and the MQTT branch at 20 ms

| series | mean | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: |
| `ingest_rows_per_s` | 3075.3 | 3105.0 | 3171.7 | 4106.1 |
| `ingest_lag_ms` | 22.3 | 22.5 | 29.2 | 50.9 |
| `consumer_num_pending` | 0.31 | 0.00 | 3.00 | 49.00 |
| `live_publish_rate` | 93.9 | 92.9 | 98.9 | 103.9 |

286,139 batches by both the vehicle's and the pit's `stream_last_seq` delta
— identical, so nothing was lost across the link — and ~6,194,929 rows by
integrating `ingest_rows_per_s` over the run. Zero on `unknown_seq_batches`,
`bad_version_batches`, `dropped_flushes`, `mqtt_drops`, `aggregate_sheds`,
`slow_consumers` at both ends, and `db_errors`.

**This is the first 20 ms run in which live-decoder published anything** —
it was broken for the original t20 and fixed before t10 — so the MQTT branch
of the pit is now exercised at both ticks.

Two things moved that had not before: `ntrip_reconnects` reached **4**
during the run, where earlier runs held at 0; and `ingest_wall_lag_ms` sits
at −29.4 ms, negative, which is the unresolved inter-host clock offset
showing through the writer's own figure rather than a lag that ran backwards.

### Offered load, and how much of the tick result is noise

| run | load-window mean | p50 | n | vs modelled 544.4 |
| --- | ---: | ---: | ---: | ---: |
| tick 20 ms (t20) | 529.0 kbit/s | 529.7 | 2015 | −2.8% |
| tick 20 ms (t20b) | 518.9 kbit/s | 519.9 | 2015 | **−4.7%** |
| tick 10 ms | 570.2 kbit/s | 570.9 | 2015 | +3.1% |

**Two nominally identical 20 ms runs differ by 1.9%.** That is the first
estimate this bench has of its own repeatability, and it is the number to
read the tick comparison against: the measured 20 → 10 ms change of +7.8% is
about four times the run-to-run spread, so "the model understates the
sensitivity" survives — but it survives as a factor of four, not as a
precise fivefold. Any future claim resting on a difference smaller than
about 2% at one tick is inside the noise.

## What P4.3 still does not answer

- **Airtime efficiency** (`wire goodput ÷ PHY rate` at saturation),
  replacing §5's assumed 0.5. The radio is now sampled at both ends — t20b
  did what this bullet asked for — and the answer is that **sampling the
  radio was never the hard part.** The bench offers 469 kbit/s to a
  32.5 Mbit/s link at −13 dBm, so it measures utilisation, not efficiency;
  §5's 0.5 describes goodput at capacity and needs a load that approaches
  capacity or a range that drops the MCS. Neither exists on this bench as
  built. See "Airtime efficiency is still not measured" above.
- **Absolute source-to-row latency** against the under-500 ms target. P4.8's
  chain is installed and working, but the UM980 held no fix in either run
  (RMC status `V`, one satellite at 22 dB-Hz), so the fix-gated PPS was silent
  and the hosts sat 51.9 ms apart. `ingest_lag_ms` above is the writer's own
  lag, not source-to-row.
- **Any pit-side wire figure at all** — the pit's stack runs inside Docker
  Desktop's VM, so its leafnode bytes never cross the netfilter hooks of the
  host we can instrument. t20b passes `--nft-command ""` explicitly to make
  that a stated choice rather than a silent fallback.
- **What the vehicle's `nft` counters are actually counting.** t20b supplies
  wire bytes at 20 ms, which this bullet used to ask for, and they came back
  *below* the application bytes they contain. Until that is explained,
  §2's framing-overhead figure has one measurement supporting it and one
  contradicting it, and the section should say so.
