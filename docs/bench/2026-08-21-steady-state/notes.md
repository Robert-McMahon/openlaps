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
