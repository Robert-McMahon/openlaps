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

- **No radio in the path.** §1's topology assumes HaLow units either side.
  The vehicle reaches the pit over wired `enp2s0`; the pit reaches back via
  `172.29.128.1` on `eth0` — it is a WSL2 guest, and that address is a NAT.
  `halow-vehicle` and `halow-pit` resolve on neither host. Nothing in this run
  crossed a radio.
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
