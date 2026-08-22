# Operator notes — 2026-08-21 steady state

Verbatim, per §10. Everything that went wrong, including the run that was
thrown away.

## A first run was discarded

An earlier 35-minute run on 2026-08-19 completed and was discarded. Its
vehicle probe was killed 55 s early by the run orchestration itself: the stop
step used `pkill -9 -f canplayer` / `-f bench_gps`, and the probe's own
argument list contained `--signal-source "canplayer x2 on vcan0 + bench_gps
50 Hz"`, so the pattern matched the probe. It died three seconds *before* the
load rather than after it and never wrote `…-veh.manifest.json`, so by §5 it
could not be cited. Its numbers, for the record: forward rate mean
521.7 kbit/s, −4.2% against §3.

That run also had its two probes started out of phase, leaving `pit_offset_s`
at −0.383 s mean with a worst case of −0.492 s against `--merge`'s 0.5 s
tolerance — 8 ms of margin. Both faults are fixed in this run: the load is
stopped by recorded PID, and both probes sleep until a shared epoch before
starting. `pit_offset_s` for this run is −0.094 s.

## Conditions that were not as intended

- **The radio IS in the path** — corrected after both runs. The vehicle is
  wired to the vehicle router (192.168.12.1); the pit is wired via USB
  Ethernet to an access point (192.168.12.201); the two bridge to each other
  over HaLow. The bridge is transparent at layer 2, so neither host's routing
  table shows it, and both manifests written during these runs carry the
  incorrect note "No radio in path". That claim was inferred from the routing
  table and is wrong; the manifests are left as written, since they are the
  record of what was believed at run time, and this file is the correction.
  `halow-vehicle` and `halow-pit` still resolve on neither host, which is why
  the probe ran with `--radio-adapter none` and collected no radio series.
- **No `nft` counters.** `sudo -n nft -j list counters` is refused for the
  operator's user on both hosts, in every path form; an
  `/etc/sudoers.d/openlaps-bench` drop-in exists but does not grant it.
  §2's one-time host preparation is therefore incomplete. Ran with
  `--nft-command ""` and `--interface`, which is the tool's supported
  degradation and records `wire_source=procnetdev`.
- **No GNSS discipline.** P4.8's chain is installed and working end to end —
  `deploy/chrony/vehicle.conf` in place, `timing_head_shim` running, the
  RP2040 emitting `TH1` at 1 Hz with the valid flag set — but the UM980 held
  no fix for the whole run. RMC status `V`, one satellite (PRN 18) at
  22 dB-Hz with no elevation or azimuth, i.e. noise-floor pickup rather than
  tracking. The PPS is fix-gated by design, so no fix means no pulse, and
  chrony's GPS sample went stale. The vehicle ran on GPS holdover plus NTP.
  Suspected antenna: cable, active-antenna bias, or sky view.
- **Inter-host clock offset 51.9 ms ±11.2**, measured directly against the
  two NATS monitoring endpoints rather than taken from `chronyc`. Note that
  `chronyc tracking` on the pit reports itself accurate to tens of
  microseconds while being 51.9 ms from the vehicle: it is a WSL2 guest whose
  clock the Windows host overrides, so chrony measures itself against the
  thing that is wrong. This is down from 200 ms before `deploy/chrony/pit.conf`
  was installed, but the residual is real and the pit's own instrumentation
  will not show it.

## Things that had to be fixed to get here

- `vcan0` did not survive a host reboot and had to be recreated; a first
  attempt at this run started without it, produced a GPS-only load, and was
  aborted 45 s in. Making the interface persistent is still outstanding.
- The timing-head shim was pointed at `/dev/ttyACM0` while the RP2040 had the
  **UART** firmware, which presents at `/dev/ttyS4`
  (`firmware/timing-head/README.md`). Corrected in
  `/etc/openlaps/timing-head.env`.
- The bench profile's `catalog.yaml` had drifted from the example profile's
  again (PR #18); the pit was running two uncommitted local fixes that were
  not on `main` (PR #17). Both merged before this run.
- The pit's `git fetch` fails on GitHub authentication, so that host cannot
  pull. Its checkout is behind `main`; the changes it needed were carried over
  as a patch by hand.

## Still unexplained

- **live-decoder published nothing**, across this run and the discarded one.
  Correct `OPENLAPS_LIVE_STREAM` and `OPENLAPS_VEHICLE_ID`, health endpoint
  responding, no errors in its log, and no consumer on `TELE_VEHICLE` at all.
  The MQTT branch of the pit is untested by both runs.

---

# Operator notes — the 10 ms run

## The pit probe never started

Its `--note` was passed inline through `ssh … sh -c '…'` and contained the
string `Docker Desktop's VM`. The apostrophe terminated the single-quoted
block and the remote shell reported:

```
VM,: 1: Syntax error: Unterminated quoted string
```

So the 10 ms run has no pit CSV, no merge, and no pit-side time series —
no `ingest_lag_ms` distribution, no `pit_offset_s`, no per-second
live-decoder rate. The vehicle probe was unaffected and wrote its manifest.
The run was accepted rather than repeated: both of P4.3's 10 ms deliverables
(offered load, framing overhead) are vehicle-side, and the pit's own counters
evidence its health even without the series. Pass the note through a file
rather than inline next time; no shell quoting, no failure mode.

## What was fixed before this run

- **`nft` counters.** The grant now exists on both hosts and the vehicle's
  ruleset is loaded, so the vehicle probe recorded `wire_source: nft` with an
  empty `reasons` field for all 2101 samples. This is what makes §2's framing
  measurement possible.
- **The pit cannot use them at all, and this is structural.** Its counters
  read exactly 0 over 30 s while the leafnode was passing traffic, because
  the pit stack runs in **Docker Desktop's VM**, not the WSL distro we ssh
  into: `docker info` reports `Name: docker-desktop`, and `192.168.12.118` is
  the Windows host's address while the distro is `172.29.140.9` behind a NAT,
  with `docker0` and the `br-*` bridges both DOWN. Loading `bench-pit.nft`
  there counts a link that is not present. It also retro-explains the 20 ms
  run's `pit_fwd_wire_kbit_s` of 0.9 kbit/s while 521 kbit/s crossed — the
  `/proc/net/dev` fallback was reading the wrong path entirely, not merely a
  coarser one. The pit probe now passes `--nft-command ""` explicitly so this
  is a stated choice rather than a silent fallback.
- **live-decoder.** Fixed between the two runs; it published at ~100/s
  throughout the 10 ms run with no drops.

## A false alarm worth recording

The agent came up reporting 163 channels at `registry_seq 4` while the pit's
`channel_map` held a generation with 161, which would have made the tick
comparison meaningless. It resolved as P4.8's change — `sys.agent.clock_*`
retired, four `host:clock_*` added — in a generation that **predates both
runs**. Old generations accumulate in `channel_map` because §8 step 4
deliberately leaves `channel_registry` alone. The t20 manifest's
`registry_hash` is byte-identical to the state file's `catalog_hash` at seq 4,
which is what confirms both runs share a generation.

Reading a channel count out of `channel_map` without checking which
generation belongs to your run is a trap; it nearly cost a good run.

## Unchanged from the 20 ms run

No GNSS fix, so no GNSS traceability — the UM980 still reports RMC status `V`
with one satellite at 22 dB-Hz, suspected antenna. Inter-host clock offset
51.9 ms. The radio-in-path correction above applies to this run too: it
crossed HaLow, but with `--radio-adapter none`, so no radio series exist for
either tick.

---

# Operator notes — t20b, the second 20 ms run

2026-08-21 23:26 → 2026-08-22 00:01 UTC. Added specifically to collect the
radio series the two runs above could not. The operator's `--note` is
carried verbatim in both manifests; this file records what surrounds it.

## It ran clean

No aborts, no discarded attempt, nothing killed early. Both probes started
from a shared epoch, 2101 vehicle samples and 2100 pit samples,
`pit_offset_s` −0.114 s against `--merge`'s 0.5 s tolerance — better than
the −0.383 s of the discarded 19 Aug run and comparable to the t20 run's
−0.094 s. The pit probe's log is two lines and both are the expected ones.
This is the first run in the series with full coverage at both ends and no
caveat about how it was executed.

## The two ends were at different commits

The vehicle probe records `git_sha f5af3d0`, the pit records `f5aa02d`, and
both record `git_dirty: true`. That is not a mistake to correct later, so it
is written down now:

- The vehicle ran from `feat/p5.1-grafana-in-the-pit-stack`, which at that
  commit was `main` plus P5.1's docs and deploy changes. It touches neither
  `src/` nor `profiles/`, so the agent and the bench profile are byte-identical
  to `main`.
- The pit could not fetch from GitHub at all (see the 20 ms run's notes), so
  its checkout sat at `f5aa02d` with the changes it needed applied by hand as
  uncommitted local edits — which is what `git_dirty` is reporting. One of
  those edits *was* the ubus shell-quoting fix that makes this run possible.
- Registry generation 4 on both, `registry_hash 5218f635…`, catalog
  `3abb3d5a…` — identical to t20 and t10, which is what makes the three runs
  comparable regardless of the SHA difference.

## The radio adapter had to be pinned to ubus

`iw` misreports this link as **5805 MHz / 160 MHz**, which is not what a
924 MHz HaLow radio is doing. The probe was therefore run with
`--radio-adapter ubus` explicitly rather than letting it auto-select, and
peers were pinned by MAC at both ends (`94:83:C4:67:4B:E4` from the vehicle,
`94:83:C4:67:42:40` from the pit) because several stations are associated.
Anyone repeating this and trusting auto-selection will get plausible numbers
off the wrong radio.

## Pit wire counters were disabled on purpose

`--nft-command ""`, not a fallback. The pit stack runs in the Docker Desktop
VM and its leafnode bytes never cross the netfilter hooks of the host being
sampled — established during the 10 ms run and unchanged. Stating it as a
flag makes the resulting empty column a decision in the record rather than
something to re-diagnose.

## Things noticed in the data afterwards

Recorded here because they were not visible during the run and they are what
the next operator needs:

- **Forward wire bytes came back below application bytes** (ratio 0.905),
  which cannot be true as stated. `summary.md` has the full argument, the
  ruled-out causes, and the S2-compression hypothesis. The check nobody has
  run is to read `/leafz` *during* load rather than after it.
- **`vehicle_leaf_rtt_ms` was 81.996 for every sample**, to three decimals,
  for 35 minutes, while the pit's varied normally. Treat the vehicle's leaf
  RTT as unreliable until someone works out where it comes from.
- **`ntrip_reconnects` reached 4**, where both earlier runs held at 0. No
  effect on `ntrip_bytes_per_s` that shows in the distribution, and nothing
  else moved, but it is the first time that counter has been non-zero.
- **`ingest_wall_lag_ms` is −29.4 ms**, negative throughout. That is the
  inter-host clock offset appearing in the writer's own figure, not a lag
  that ran backwards; it is another face of the 51.9 ms problem above.

## Still unfixed, and it is the same list

No GNSS fix — the UM980 still holds none, so this run is not GNSS-traceable
either and the clock question is now three runs old. `vcan0` still does not
survive a host reboot. The pit still could not fetch from GitHub at run
time. None of these blocked the run; all three are the same entries the
20 ms run's notes opened with.
