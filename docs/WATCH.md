# Watch: envelope, drift, ratio, counter and whole-car monitors (P7.5, P7.6)

`openlaps-watch` reads `TELE_VEHICLE`, resolves each registry generation,
resamples fresh values at one-second capture-time intervals, and writes one
score per monitor to Timescale. MQTT publishes
`openlaps/<vehicle>/watch.<monitor>.score` with `value`, `time` (milliseconds),
`status`, and `source: pit`. The table/view contract is in `PIT_SCHEMA.md`.
P7.5 added no dependency; P7.6 added `numpy` for the whole-car model.

Configure `profiles/<car>/watch.yaml`. The nine example envelopes cover oil
pressure, fuel pressure, coolant temperature, five PDM circuits and battery
voltage; the P7.6 kinds below add nine drift monitors, five ratios, one
counter and the whole-car model. Bin widths and `min_scale` are physical catalog units. Haltech
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

## The P7.6 kinds

Four more kinds share the envelope's interface, file and checkpoint. Every
finding summary carries `expected`, `observed` and `baseline`, so the
findings table renders them all the same way; `kind` says which produced
it. Scores stay in `[0, 1]` in `watch_scores` for every kind: 1.0 means "at
or beyond the threshold", and `residual` carries the kind's own unit of
evidence. `numpy` was added for the whole-car model (ADR 0011 decision 4).

**`drift`** is per lap, not per sample. While `when` holds (oil pressure
with RPM in the cruise band; a PDM current while its load is on) the lap's
one-second observations of `target` are averaged; on each `lap_completed`
event that the timing engine marks valid -- so in-laps and out-laps are
excluded, as is any lap during which the car was in the pits -- a lap with
at least `min_lap_samples` observations counts. The first `baseline_laps`
clean laps set the expectation (their mean) and the unit of residual (their
standard deviation, floored by `min_scale`); after that each lap's residual
feeds a CUSUM with allowance `cusum_k` and threshold `cusum_h`, in the
direction(s) configured. The score is the CUSUM as a fraction of `cusum_h`.
The finding opens at the lap boundary that crosses the threshold and its
summary carries the per-lap series -- lap number, mean, residual, running
sum -- which is what makes a slow decline believable. It closes after three
consecutive laps back within one baseline standard deviation, at which
point the sum restarts from zero. Between laps the score row repeats the
last lap's verdict; a gated second still reports a finding that opened or
closed at the boundary.

**`ratio`** watches a physical relationship that should be a constant:
`numerator / denominator` (a list is averaged) per group, where the group
is the integer value of `per` (the gear) or "all". Learning is per group
with median and MAD, as the envelope, for `baseline_seconds` eligible
seconds; groups with fewer than `min_group_samples` observations have no
opinion. `when` gates the evaluation -- the wheel-speed monitors ask for
small longitudinal g, more than 60 km/h and the brake off, so a braking
zone or a pit stop scores nothing rather than scoring wrong. The
per-wheel tyre-circumference difference is therefore a learned baseline,
not assumed to be zero. The summary says which group, whether the ratio is
`low` or `high`, and what the profile says that means (`low_means`,
`high_means`): for the driveline, engine faster than the wheels is clutch
slip; for a wheel, low is a puncture or pressure loss, high a dragging
brake or a bearing.

**`counter`** learns nothing: a monotonic count should not move. Over the
last `score_window` seconds of eligible observations the rise is turned
into a rate per minute, the score is that rate as a fraction of
`open_finding_above` (counts per minute), and the finding is the rate. It
needs ten seconds of history before it has an opinion (`warming`).

**`whole_car`** is one monitor, a channel list and a baseline policy. During
the baseline window it accumulates only the count, the sums and the
second-moment matrix of the channels -- so the per-tick checkpoint is a
p x p table whatever the window length -- and at the freeze it
standardises every channel and fits, per channel, a ridge regression
predicting it from all the others out of the correlation matrix (`numpy`
least squares; no iteration, no hyperparameter a crew would have to
understand). The residual covariance follows analytically and its inverse
gives each sample's Mahalanobis distance, reported as a root-mean-square
standardised residual so that "4 sigma" means the residual vector sits four
baseline residual standard deviations off per channel on average. That
distance is smoothed over `score_window` and compared with
`open_finding_above` (in sigma); the stored score is the fraction of that
threshold and `residual` is the unsmoothed distance. The summary ranks the
channels by standardised residual with expected and observed in catalog
units, the direction, the percentage, and the channels that predict it
most strongly, so it reads "oil pressure 29 % below expected from car.rpm,
car.oil_temp". A channel with no variation in the baseline is floored at
one percent of its mean so a constant does not become an infinite residual.

It will fire on legitimate change -- nightfall, rain, a driver with a
different throttle habit -- which is why `baseline: stint_start` is the
default and why the finding names its baseline (`baseline_session`,
`baseline_stint`). Its false-positive budget (P7.7) is the one most likely
to need a recorded decision rather than a zero.

### Stint baselines

`baseline: stint_start` re-learns at every driver change. The service asks
`stints` for the open stint at each tick; when the number changes, every
`stint_start` monitor is replaced by a fresh one (restored from that stint's
checkpoint if a restart made one), its previous finding is closed with
`closed_reason: stint_changed`, and every other monitor carries on. The
checkpoint row's `stint_number` is the stint for `stint_start` monitors and
0 for the rest, which is the slot P7.5 reserved.

## Restart, stored models and session changes

The model, partial learning samples, EWMA and finding identity are checkpointed
in `watch_baselines` with every score transaction. Restart reloads the active
session's checkpoint, including an unfinished learning window. It never
silently re-fits after a configuration change: a mismatched checkpoint reports
a failed tick in `/health` and the logs. Session-wide baselines use
`stint_number=0`; `stint_start` policies use the stint number (see above). A new session gets a
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
through all nine envelopes: every model freezes, every populated bin reaches
`ready`, and no finding opens. The same replay with oil pressure multiplied
by 0.7 after 180 seconds opens `oil_pressure_envelope` -- its explanation
contains 100 kPa expected, 70 kPa observed and the RPM/temperature bin --
and `whole_car`, which was configured with nothing but a channel list and
ranks `car.oil_pressure` first. `tests/test_watch_monitors.py` drives a
synthetic car with laps, gears, a braking zone and pit stops through each
P7.6 kind: the clean run opens nothing; a half-kPa-per-lap oil decline, an
8 % third-gear slip, a 2 % slow puncture, a dragging brake, a trigger
counter climbing six a minute and an unconfigured oil-pressure fault each
open exactly their finding with expected and observed stated. The wheel
monitor scores nothing in the braking zone or the pits.
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

`/health` also carries the consumer's own counters. `backfill_skipped` is
batches whose epoch is older than 30 s, skipped on the batch header before
any registry lookup: a source catch-up after a pit outage arrives as new
messages, at thousands per second, and is the ingest-writer's to archive, not
the watch's to learn. `slow_consumer_drops` is messages the NATS client
discarded because the service fell behind; it is logged at most once a
minute, and a value that keeps rising means the watch cannot keep up with the
stream. `dropped` counts individual samples outside the live window inside an
otherwise live batch; `unknown_registry`, `bad_version` and `malformed` are
the decode refusals from `docs/WIRE_FORMAT.md`.

`watch-critical` and `watch-warning` in `alarms.yaml` select open findings by
exact severity, avoiding duplicate warning notifications for a critical fault.
The generator preserves its existing `severity_at_least` option for callers
that want aggregation. Verify the corresponding Grafana rule fires and
resolves after healthy scoring closes the finding. Delivery uses the existing
Grafana contact point; P7.2/P7.3 supply acknowledgement and phone delivery.
Stop/restart `watch` during the fault: the expected value and finding id must
remain unchanged. If input goes missing, the score becomes NULL and the
finding stays open until healthy evidence or a session change closes it.

## Validation status

Automated unit, replay and service tests cover monitor behaviour, persistence
and database integration. On-car false-positive measurement remains an event
prerequisite; see the current [project status](status.md) and the bench runbook
rather than relying on historical branch test counts.
