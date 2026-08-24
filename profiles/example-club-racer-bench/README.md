# Bench profile — the example car, driven from synthetic sources

Phase 4 measures whether full-rate telemetry fits over a HaLow link
(`docs/LINK_BUDGET.md`). Doing that needs the real vehicle agent reading
real interfaces, and a bench has neither a running engine nor a sky view.
This profile is the example profile with its two live inputs repointed at
the bench's synthetic ones, so the load is injected *below* the agent, at
the socketCAN and serial boundaries — `docs/plan/PHASE4.md`, locked
decision 4.

`catalog.yaml`, `dbcs/` and `tracks/` are **byte-identical copies** of
`profiles/example-club-racer/`'s and must stay that way: the bench measures
the real channel mix or it measures nothing.
`tests/test_bench_profile.py` enforces the byte-identity, the resulting
identical runtime catalog, and that no third file appears here without
someone declaring it. Only `vehicle.yaml` differs, in exactly two places.

## The two deltas

**1. The bus keeps `name: can0` and takes `interface: vcan0`.**
`BusConfig.name` and `BusConfig.interface` are independent fields
(`src/core/config.py`): the name is what every catalog `from: "can0:…"`
reference resolves against, the interface is the socketCAN device actually
opened. So all 123 catalog entries stay valid with no catalog edit at all.

**2. The serial source points at the pty `tools/bench_gps.py` publishes,
and its `um980` driver takes `configure_on_start: false`.** The driver
block is *kept*, not dropped, for two reasons:

- Serial source references resolve `<serial>:<driver>` when a driver is
  attached and `<serial>:<decoder>` when one is not
  (`src/core/config.py`, `_validate_source_refs`). Removing the block would
  turn every `from: "serial0:um980.RMC.*"` entry into a validation error and
  force a catalog edit — the one thing that must not happen here.
- An attached driver is what makes `SerialCollector` accept RTCM
  write-back from the pit's ntrip-client. Removing it would delete
  pit→vehicle correction traffic from the measured reverse channel, which
  is the direction `LINK_BUDGET.md` §8 has never measured and P4.3 exists to
  measure.

`configure_on_start: false` is what keeps the driver from trying to
configure a receiver that is not there: `UM980Driver.configure` returns
immediately, and the retained `rate_hz`/`sentences` values document the
real receiver's settings without ever being sent.

## The vehicle id is unchanged, and that has a consequence

The id stays `example-club-racer`. The bench is measuring the deployed
configuration; changing the id would change every subject and every
pit-side setting along with it.

**Read this before the first bench run, because otherwise it looks broken.**
The registry generation counter lives in `.registry-state.json` *beside the
profile*, so this directory has its own generation sequence, independent of
the example profile's. A pit that has already seen the real profile's
generations will reject bench batches with `unknown_seq_batches` until it
rescans the catalog subject. The writer recovers on its own; P4.2's runbook
starts each bench series from a clean `TELE`/`TELE_VEHICLE` anyway.
`tests/test_bench_profile.py::test_registry_generation_is_tracked_per_profile`
pins the behaviour so this note stays true.

## CAN load: two modes, and one trap

`can-utils` is installed on the SBC; `canplayer` replays the checked-in
captures. P4.2's runbook is the operator document — this is the shape of the
choice.

- **Physical `can0`** — highest fidelity: real arbitration against the live
  FDI IMU, the real driver and USB path, and socketCAN loops transmitted
  frames back to local sockets so the agent sees them as it would on the
  car. Use the *example* profile for this mode, or a local copy of this one
  with `interface: can0` — and record which, and its content hash, in the
  run manifest.

  **The trap that otherwise costs an afternoon:** classic CAN needs at least
  one other node to assert the ACK slot. With nothing else on the bus the
  adapter goes error-passive and then bus-off, which presents as a driver or
  permissions fault and is neither. Power the IMU.

- **`vcan0`** — what this profile ships with. No hardware, no ACK
  requirement, and the only option when the IMU is off the bench. Loses real
  arbitration and the USB adapter from the measured path.

Both captures are needed on `vcan0`, because they are different parts of
the bus and `canplayer` takes one file per invocation:

| Fixture | Frames | Span | Frames/s | Mapped samples/s |
| --- | ---: | ---: | ---: | ---: |
| `candump-sample.log` (ECU + PD16A + WB1) | 45,861 | 30.00 s | 1,528.9 | 2,012.1 |
| `candump-imu-sample.log` (FDI DETA10A) | 1,255 | 4.99 s | 251.5 | 752.5 |

`canplayer -l i` loops a file. The loop seam is a timestamp discontinuity:
harmless for offered load, but per-signal rates must not be read across one.

With `vcan0` present, `tests/test_can_collector.py::test_vcan_roundtrip_emits_samples`
stops skipping and exercises the real socketCAN path. CI has no vcan and
will keep skipping it — that is fine, because the value of that test is on
the bench host, which is where it now runs.

## Check the mix before trusting a number

```
uv run tools/bench_check.py --predict   # no hardware needed
uv run tools/bench_check.py             # 30 s against the live bench
```

`tools/bench_check.py` drives the real collectors, catalog, pipeline and
timing app against whatever the bench is currently offering and gates the
measured rate per source class against what these fixtures predict. Run it
before every series: a bench that has silently lost GPS or the IMU still
produces a perfectly plausible bandwidth figure, of the wrong signal set.

It also prints the bench's predicted mix against `LINK_BUDGET.md` §2's
modelled 4,262 samples/s, and **the two do not agree**:

| Class | Predicted here | §2 modelled | Delta |
| --- | ---: | ---: | ---: |
| CAN (ECU + PD16A + WB1) | 2,012.1/s | 2,962.2/s | −32.1% |
| IMU | 752.5/s | 1,000.0/s | −24.8% |
| GPS | 250.0/s | 300.0/s | −16.7% |
| Host (`sys.host.clock_*`, 5 s poll) | 0.8/s | — | n/a |
| **Total** | **3,015.4/s** | **4,262.2/s** | **−29.3%** |

That gap is not a bench fault and no wiring change closes it. §2 counts
every signal in each DBC-known CAN message where the catalog maps a subset
of them; it models GPS at 6 doubles per fix where the catalog has 5
`position.*` channels; and it models the IMU at a flat 100 Hz × 10 where the
recorded frame rates are 100.2 / 100.2 / 50.1 / 1.0 Hz across four messages.
The bench also offers ~49 samples/s of `lap.*`/`timing.*` derived channels
and ~9/s of `sys.agent.*`/`sys.host.*` health that §2 does not model at all (measured over the 2026-08-21 run; see docs/bench/2026-08-21-steady-state/).

`bench_check` therefore gates on measured-vs-predicted and *reports*
predicted-vs-modelled (`--model-tolerance` makes the latter fatal for anyone
who wants it to be). Reconciling the model with the truth is P4.3's
signal-mix ground truth; this table is where that starts, and until it is
done a bench bandwidth figure should be read as measuring ~71% of the
offered load `LINK_BUDGET.md` §3 predicts.

## Not here

`session-roster.json` is not copied: it is a pit-side file named by
`OPENLAPS_ROSTER_FILE`, not part of a vehicle profile, and the bench pit
stack can keep pointing at the example profile's copy.
