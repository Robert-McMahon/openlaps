# Envelope monitoring (P7.5)

`openlaps-watch` reads `TELE_VEHICLE`, resolves each registry generation,
resamples fresh values at one-second capture-time intervals, and writes one
score per monitor to Timescale. MQTT publishes
`openlaps/<vehicle>/watch.<monitor>.score` with `value`, `time` (milliseconds),
`status`, and `source: pit`. The table/view contract is in `PIT_SCHEMA.md`.
No dependency was added.

Configure `profiles/<car>/watch.yaml`. The nine example monitors cover oil
pressure, fuel pressure, coolant temperature, five PDM circuits and battery
voltage. Bin widths and `min_scale` are physical catalog units. Haltech
coolant/oil/ambient temperatures are **K**, not °C. Explanations carry units
from the stream's registry for the target and every conditioning channel;
a changed unit refuses comparison against the old baseline.

The PDM on state is output load/duty greater than zero, independent of the
measured current. `PIN_STATE` is a fault enum, not an on/off bit. Three
previous catalog references did not exist in the DBC: the steering pump,
thermo fan 1 and fuel pump now use their high-side current signals.

## Learning and scoring

An active session in `sessions`, an explicit `lap.event` with
`pit_status: track`, fresh inputs, and RPM above `baseline_rpm_min` are
required both to learn and to score. No timing event means no opinion.
These are observable gates, not a claim that the car is mechanically healthy:
the crew must confirm the first window is clean. A session baseline learns
180 **eligible seconds**, pausing in the pits, while stopped or during data
loss, then freezes. `min_bin_samples: 50` means 50 uniformly spaced
one-second observations, not 50 irregular RBE deliveries. Bins with fewer
observations remain unknown after freezing. Widen bins or extend the clean
window if too few become populated.

The model uses median and MAD per bin; residual is
`(observed - median) / max(min_scale, 1.4826 * MAD)`. The explicit physical-unit
floor prevents quantisation/constant signals producing infinite residuals.
The EWMA of `abs(residual) > residual_sigma` uses a 30-second time constant.
It opens above 0.8 and closes at or below 0.8. A continuous fault starting
from score zero therefore needs about 49 eligible seconds. These are
change detectors, alongside the faster threshold alarms.

Gated, stale, learning, insufficient-bin and unit-mismatch outputs have a
NULL score. They never report a healthy zero or close an existing finding.
The summary records target, expected/observed values, MAD, sample count,
baseline source session,
conditioning bins and their units at the peak score. On/off bins also record
the raw load and the `active_above` threshold.

The live service allows two seconds for independent source batches to arrive;
late/backfilled samples cannot rewrite an evaluated tick. Each input expires
after `max_age_seconds` (10 by default); set this above the longest configured
RBE heartbeat. A missing evaluation tick is not filled with newly arrived
data. This is a live monitor, not an accelerated replay processor; use
wall-clock replay (`--rate 1`) against the deployed service.

## Restart, stored models and session changes

The model, partial learning samples, EWMA and finding identity are checkpointed
in `watch_baselines` with every score transaction. Restart reloads the active
session's checkpoint, including an unfinished learning window. It never
silently re-fits after a configuration change: a mismatched checkpoint reports
a failed tick in `/health` and the logs. Session-wide baselines use
`stint_number=0`; per-stint policies are reserved for P7.6. A new session gets a
fresh model and closes the prior session's findings with an explicit reason.

Fit from a **completed, known-clean** session:

```bash
uv run tools/watch_fit.py --session SESSION_ID --output /tmp/watch-models
```

Review and copy the JSON files into the profile. For each corresponding
monitor set `baseline: stored` and `stored_file: <monitor>.json`; the fitter
exports exactly that configuration. All other parameters must match. Stored
models become session-scoped checkpoints as soon as the service starts.
Do this before opening the session; editing a live session's baseline config
is deliberately rejected. No re-learn UI ships in this package.

## Firing and clean replay checks

`tests/test_watch.py` replays 300 seconds of deterministic on-track readings
through all nine monitors: every model freezes, every populated bin reaches
`ready`, and no finding opens. The same replay with oil pressure multiplied
by 0.7 after 180 seconds opens only `oil_pressure_envelope`; its explanation
contains 100 kPa expected, 70 kPa observed and the RPM/temperature bin.
Tests also run the service against NATS, Timescale and MQTT, reload the
checkpoint after restart, and query both views as `grafana_ro`.

The checked-in engine-start CAN fixture is only 30 seconds, mostly idle,
and has no on-track timing events. The real `tools/replay.py` pipeline is
also tested against it: it cannot train these on-track models or open a
finding. Looping it does not turn idle operation into a representative
on-track baseline. A longer recorded clean on-track session is required for
the real-car acceptance drill; the synthetic replay is not a claim of a
measured false-positive rate.

For that drill, open a test session, start the pit stack including `watch`,
and replay a clean on-track CAN/GPS recording at rate 1. Verify populated
scores in `v_watch_scores`, no open `v_watch_findings`, and healthy dependencies
at `http://localhost:8090/health`. Repeat into a new test session with
`tools/replay.py --oil-pressure-fault-after 180` (plus the same input options).
The injector changes only replayed oil pressure, before mapping/RBE, by 0.7;
it never edits the recording. It applies per replay cycle.

`watch-critical` and `watch-warning` in `alarms.yaml` select open findings by
exact severity, avoiding duplicate warning notifications for a critical fault.
The generator preserves its existing `severity_at_least` option for callers
that want aggregation. Verify the corresponding Grafana rule fires and
resolves after healthy scoring closes the finding. Delivery uses the existing
Grafana contact point; P7.2/P7.3 supply acknowledgement and phone delivery.
Stop/restart `watch` during the fault: the expected value and finding id must
remain unchanged. If input goes missing, the score becomes NULL and the
finding stays open until healthy evidence or a session change closes it.

## Implementation verification

Validated from the isolated P7.5 branch based on `9324136`:

- 16 watch tests pass, including the real NATS/Timescale/MQTT service test,
  stored-file loading, restart persistence and Grafana view grants.
- 37 alert-generator, alert-contract and catalog tests pass.
- 20 bench-check tests pass with the unrelated live serial test deselected.
  The mapped replay rate is now approximately 2067.7 CAN samples/s, reflecting
  the five added load channels and three repaired current mappings.
- The full regression run completed with 816 passed, 23 failed and 3 skipped.
  Its two old eight-rule assumptions and three outdated rate assertions were
  corrected and the affected tests rerun as above. Other failures concern
  the existing serial collector's uninitialised `_raw_log_dir`/`_raw_writer`
  and firewall tests that reject the existing video counter rules. Both
  failure causes were reproduced from an untouched archive of `9324136`.
- Formatting passes repository-wide. Lint passes for the changed files;
  repository-wide lint still reports the existing unused imports/import order
  in `src/collectors/serial/transport.py` and `tests/test_serial_collector.py`.

These are automated replay/integration results, not an on-car false-positive
measurement. The recorded on-track drill above remains an event prerequisite.
