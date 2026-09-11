# 0010: Vehicle targets, and a host-hardware overlay on the profile

## Status

Accepted, 2026-09-08.

## Context

A vehicle profile is meant to describe *the car*: which DBCs decode which
bus, which receiver sits on which source, what the 123 channels are called
(ADR 0004). Two of its fields describe something else entirely.
`buses[].interface` is a socketCAN device name and `serial[].port` is a path
under `/dev`, and both belong to the SBC rather than to the car. Move the
same car from a Radxa X4 to another board and every DBC, every channel and
every RBE policy is unchanged — those two strings are not.

The codebase had already worked around this three times, in three different
ways, without naming the problem:

- **`profiles/example-club-racer-bench/`** is a byte-for-byte copy of the
  example profile whose only deltas are `interface: vcan0` and a different
  serial port. `tests/test_bench_profile.py` exists to enforce that the rest
  of it stays identical — a test whose entire job is to police a copy.
- **`deploy/vehicle-compose.yaml`** mapped the host's real GNSS device to
  `/dev/ttyUSB0` inside the container, with a comment saying this was "so
  the profile does not encode the host's wiring". A device-path rewrite
  standing in for configuration.
- **The car's own checkout** carries a local edit to `vehicle.yaml` that can
  never be committed, because committing it would break every other host.

The bitrate had the same shape of problem in the other direction. It *is* a
property of the car's bus and it *was* written out by hand in
`deploy/systemd/openlaps-agent.service`, in `deploy/README.md`, and in
whatever the operator typed at the track — three copies, of which the profile
was the only one anybody updated.

Adding a second supported board (Luckfox Omni3576: on-SoC CAN, a real UART
instead of a USB relay, Rockchip MPP instead of VAAPI, no PPS client in the
kernel) turned all of this from untidy into blocking.

## Decision

**A target is one SBC the vehicle stack runs on, described by a directory
under `deploy/targets/`, and a profile is never edited to move between
targets.**

The agent's slice of that directory is `hardware.yaml`, a *host-wiring
overlay* applied on top of the loaded profile:

```yaml
target: luckfox-omni3576
buses:
  can0: { interface: can0, bitrate: 1000000 }
serial:
  serial0: { port: /dev/ttyS4, baud: 115200 }
```

- It is selected by `OPENLAPS_HARDWARE` (or `openlaps-agent --hardware`), and
  is optional: without one the profile is used as written, which is the
  pre-0010 behaviour.
- It addresses transports by the profile's own `name` — the same stable
  identifier the catalog's `from:` references already resolve against — so
  remapping a port cannot invalidate a channel.
- It may set only `interface`, `bitrate`, `port` and `baud`. Everything else
  in a profile is about the car.
- A serial source may additionally carry `driver: { configure_on_start: ... }`.
  That one driver setting is not about the receiver: it answers "is a real
  receiver on the other end of this port?", which the *rig* decides. Nothing
  else in a driver block is overridable — `rate_hz`, `sentences`, `pps` and
  `timing_output` describe the receiver the car carries and stay in
  `vehicle.yaml`, inert but readable, since `UM980Driver.configure` returns
  immediately when this is false.
- A bus may additionally carry a `link:` block — `fd`, `dbitrate` — which is
  **not** a profile override: those are arguments `tools/can_up.py` hands to
  `ip link`, and neither the profile nor the collector ever sees them. They
  exist because the Luckfox's `rk3576_canfd` controller cannot be brought up
  in classic mode at all (`incorrect/missing data bit-timing`), on a car
  whose bus is nonetheless ordinary classic CAN. That is a property of the
  silicon, and putting it in `vehicle.yaml` would say something false about
  the car.
- Naming a transport the profile does not define is a **load error**, not an
  ignored key. A typo must not leave the agent opening the profile's default
  port while the operator believes otherwise.
- It is re-validated through `VehicleConfig`, so an overlay is held to
  exactly the constraints `vehicle.yaml` is.

The rest of the directory is consumed by other things, one file each:
`target.env` by compose, `go2rtc.yaml` by go2rtc, an optional `compose.yaml`
by compose for device nodes the base stack does not map. `deploy/targets/`
selects between them with one variable, `OPENLAPS_TARGET`.

`tools/can_up.py` reads the same resolved configuration and brings each bus
up at its own bitrate, replacing the three hand-copied `1000000`s.

## Consequences

**The container no longer rewrites the device path.** `vehicle-compose.yaml`
maps the GNSS device at the same path inside and out, because the overlay can
only do its job if both ends agree on what the port is called. Existing
deployments keep working: the compose defaults are the X4's values, and the
X4's overlay names the device the X4 already had.

**Video is deliberately not in `hardware.yaml`.** go2rtc reads its own config
and the agent never touches a camera, so a `video:` block there would be
configuration nothing consumes. The encoder pipeline lives in the target's
`go2rtc.yaml` — which is genuinely the one file that cannot be shared
between boards, since `hevc_vaapi` does not exist on a Rockchip and
`h264_rkmpp` does not exist on an Intel.

**Two things are still written twice, and one test holds them together.**
Compose has to know the device node to map it and cannot read a YAML file to
find out, so `OPENLAPS_SERIAL_DEVICE` in `target.env` duplicates
`serial0.port` in `hardware.yaml`. `tests/test_hardware.py` asserts they
agree for every shipped target. The alternative — teaching compose to read
the overlay — is a generator step, and a generated compose file is worse than
a tested duplicate.

**The Phase 4 bench profile is deleted by this ADR**, and was the change that
justified the `driver.configure_on_start` field above. `example-club-racer-bench/`
was a copy of the example profile — `catalog.yaml`, four DBCs, two track
files — differing in three lines: `interface: vcan0`, the bench GPS pty, and
`configure_on_start: false`. It is `tools/bench-hardware.yaml` now, an overlay
on the example profile. It does not live under `deploy/targets/` because it is
not a board; it is a test fixture, and it sits with the tools that stand that
fixture up.

Two things fell out of that which are worth recording. Most of
`tests/test_bench_profile.py` was a copy-policing exercise — `filecmp` over
eight shared files plus an assertion that no ninth had appeared — and is now
unnecessary rather than merely passing: an overlay reads the same files off
disk, so there is no copy to drift. And the footgun that profile's README had
to warn about is gone: the registry generation counter keys on catalog content
in a state file beside the profile, so two profile *directories* meant two
independent sequences and a pit that had seen the car's generations rejected
bench batches with `unknown_seq_batches` until it rescanned. One directory
means one sequence, and alternating bench and real runs is now a no-op.

Bench manifests recorded before 2026-09-09 (`docs/bench/2026-08-21-steady-state/`)
name the deleted path. They are left exactly as written — they record what
actually ran — and their registry content hash, which is what a later run
should be compared against, is unchanged because the catalog never differed.

**Adding a board is a directory, not a code change.** No registry, no plugin
discovery, no entry point — the same discipline
`src/collectors/serial/drivers.py` states for receivers. CI walks
`deploy/targets/` and validates whatever it finds there against the example
profile, so a target that names a transport nobody has fails in CI rather
than on the car.
