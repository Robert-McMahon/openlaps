# P4.3 steady state — 2026-08-21

Two 35-minute runs on the bench profile with replayed CAN and synthetic GPS:
one at `OPENLAPS_TICK_MS=20`, one at `10`, as the brief requires. Both ran
registry generation 4 (163 channels, catalog `3abb3d5a…`), so they are
directly comparable.

Provenance: `t20-vehicle.manifest.json`, `t20-pit.manifest.json`,
`t10-vehicle.manifest.json`. **There is no `t10-pit.manifest.json`** — the pit
probe failed to start for the 10 ms run (see `notes.md`), so that run is
vehicle-side only. Both headline results below are vehicle-side measurements
and are unaffected.

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

## What P4.3 still does not answer

- **Airtime efficiency** (`wire goodput ÷ PHY rate`), replacing §5's assumed
  0.5. Not yet measured, but measurable: the radio *is* in the path. The
  vehicle is wired to the vehicle router (192.168.12.1) and the pit is wired
  to an access point (192.168.12.201); those two bridge to each other over
  HaLow. The bridge is transparent, which is why neither host's routing table
  shows it. Both devices answer ping with ssh open, so `link_probe`'s
  `--radio-host` / `--radio-adapter` path should reach them for PHY rate and
  station statistics.
- **Absolute source-to-row latency** against the under-500 ms target. P4.8's
  chain is installed and working, but the UM980 held no fix in either run
  (RMC status `V`, one satellite at 22 dB-Hz), so the fix-gated PPS was silent
  and the hosts sat 51.9 ms apart. `ingest_lag_ms` above is the writer's own
  lag, not source-to-row.
- **Wire bytes at 20 ms**, and any pit-side wire figure at all — the pit's
  stack runs inside Docker Desktop's VM, so its leafnode bytes never cross the
  netfilter hooks of the host we can instrument.
