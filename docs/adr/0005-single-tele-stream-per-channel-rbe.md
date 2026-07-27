# 0005: Single TELE stream with per-channel RBE, no live/batch split

## Status

Accepted, 2026-07-26

## Context

Many telemetry systems split "live" (low-latency, often decimated) traffic
from "historical" (full-rate, batched) traffic into separate paths or
streams. This project's owner instead wants full-rate 100 Hz IMU data live
whenever the link supports it — not a live view that is always decimated for
transport reasons, with full rate reserved for post-session analysis.

At the same time, not every high-rate channel is valuable at full rate on
the link: in the current system, the two 100 Hz PD16 analog/switched input
voltage broadcasts alone account for a large share of total CAN bandwidth,
while being of limited value live in the pit at that rate.

## Decision

Use a **single JetStream stream, `TELE`, for everything**, including 100 Hz
IMU data (on the order of 10 kB/s in protobuf, per the wire-format sizing
model). Data is published as protobuf `SampleBatch` messages on a 10–20 ms
tick; there is no separate live-only or batch-only stream — the ticks
published to `TELE` *are* the live feed, and the same stream, read from an
earlier sequence, *is* the historical feed.

Per-channel **report-by-exception (RBE)** policy in the catalog — deadband,
minimum interval, and a maximum-interval heartbeat — is the relief valve for
channels that don't need full rate on the link (e.g. PD16 input voltages),
applied individually per channel rather than as a link-wide mode. RBE
applies only to the **link/store path**; the on-vehicle timing engine taps
channel data **pre-RBE**, so lap/sector timing never sees decimated position
samples regardless of what link policy does to what eventually reaches the
pit. On a bad-RF day, the accepted failure mode is that the stream falls
behind and catches up once the vehicle returns to pit wifi — not a fallback
to some degraded live-only mode.

## Alternatives considered

- **A second, local-only stream harvested post-session**, separate from the
  live feed, for full-rate data (particularly IMU). Rejected: the owner
  explicitly wants full-rate IMU live whenever the link allows it, not
  deferred to a post-session harvest step. Maintaining two streams — their
  retention policies, their consumer offsets, and deciding at the stream
  level which channels go where — is more moving parts than one stream with
  per-channel RBE, for a link-constrained-live-view problem that the RBE
  relief valve already addresses at the channel level.

## Consequences

**Positive**

- One stream to provision, retain, and monitor, instead of two with
  separate lifecycles.
- "Live" and "historical" are definitionally the same data on the same
  stream — there is no possibility of live and historical views drifting
  or needing reconciliation with each other.
- RBE is scoped per channel, in the catalog, as an ordinary configuration
  change — not an architectural live/batch fork that requires code changes
  to adjust.
- Lap/sector timing correctness is protected by construction (the pre-RBE
  tap), independent of whatever link policy is applied to the stored/pit
  side of the same data.

**Negative**

- On a bad-RF day, all channels lag together — there is no independent,
  always-current, low-bandwidth "vital signs only" live path for something
  like a dashboard warning light while the bulk stream is catching up.
- The catalog's RBE configuration becomes load-bearing for whether the
  stream fits the link at all; a new noisy channel added without an RBE
  policy has no other safety net besides someone noticing and editing the
  catalog by hand — the plan's own stated "emergency relief valve" is a
  manual one.
- `max_interval` heartbeats must correctly bound how far TimescaleDB's
  fill-forward reads can drift from reality; a bug in heartbeat emission
  would let historical queries show a stale value as if it were current,
  silently.
