# 0001: Fresh public repo with clean history, big-bang cutover

## Status

Accepted, 2026-07-26

## Context

The current telemetry stack lives in a private repository (`/mnt/data/logger`)
that is going to be replaced end-to-end by a generic, open-source platform
(`openlaps`) in which any specific car is just a configuration profile.
Two properties of the existing repo make simply continuing it in the open
unattractive:

- **Secrets are hardcoded, and not only in the current tree.** NTRIP
  credentials, InfluxDB tokens, and admin passwords appear in compose files,
  Telegraf configs, and provisioning files — and because those files have
  been edited in place across many commits, the same secrets are also
  present in the repo's git history, not just its HEAD.
- **The car-specific hardcoding is woven through history, not just code.**
  Commit messages, config diffs, and design docs reference this specific
  vehicle, its DBCs, and its track/session details throughout the project's
  life, not only in a few isolated files. There is no single commit after
  which the repo becomes "generic platform, car is a config profile" — that
  framing does not exist anywhere in the current history.

The rewrite also touches every major subsystem at once: transport (MQTT →
NATS JetStream), storage (dual InfluxDB → single TimescaleDB), and the
collector model (domain-specific collectors → generic transports + a channel
catalog). These changes are interlocking rather than independent — for
example, deleting the diff-based `data-sync-service` depends on JetStream's
resumable-consumer semantics, which in turn depends on collectors publishing
into that stream in the first place.

## Decision

Start a fresh public repository (`openlaps`) with clean history from an empty
initial commit. The old repository stays private and unpublished; it is not
scrubbed, rewritten, or merged in any form. It remains bootable as an
operational rollback until the new stack has been validated on the car.

Sequencing is **big-bang, not incremental**: build the complete new system
(collectors, catalog, agent, pit services, storage, dashboards) to feature
parity, validate it primarily by replaying previously recorded real
telemetry (the June 2025 Wanneroo event) through the full pipeline, and then
switch the car to the new stack in one step — with the old stack retained
and ready as a rollback for the first on-track sessions on the new system.

## Alternatives considered

- **Scrub history with `git-filter-repo` / BFG and publish the same repo.**
  Rejected: history-scrubbing is error-prone at the scale of "every commit
  since a config format existed" — a single missed occurrence of a token
  format re-exposes it. Even a successfully scrubbed history still leaves a
  repo whose commit messages, issues, and design discussion are about this
  specific car; it does not produce the "generic platform, car is an example
  profile" framing the project wants, and it does nothing to address the
  architectural debt (three mosquitto brokers, dual InfluxDB, a diff-based
  sync service) that the rewrite exists to remove.
- **Incremental in-place refactor of the current repo.** Rejected: the
  planned changes are simultaneous and interlocking rather than swappable
  one at a time. Doing this piecemeal means every intermediate state must be
  a hybrid old/new system that still has to run the actual car, which
  doubles the number of things that need to work correctly at any given
  moment, and delays open-sourcing anything until the very last piece lands.
- **Strangler-pattern, service-by-service cutover on the existing car.**
  Considered and rejected by the project owner in favour of building the
  complete system first. Timing/telemetry correctness is judged by
  comparing the new system's output against itself on known-good recorded
  data; a system that is partly old-stack and partly new-stack mid-migration
  is harder to validate this way than a single system verified by full
  replay. Full retention of the old stack as rollback also removes most of
  the risk the strangler pattern is normally chosen to mitigate.

## Consequences

**Positive**

- The public repo's history is clean by construction — no secret has ever
  existed in it, so there is no "rotate and hope nobody already cloned the
  old history" exposure for the new repo.
- The project reads, from commit 1, as a generic platform with an example
  profile, not as one team's car with the serial numbers filed off.
- There is exactly one cutover event, gated on replay parity plus a garage
  HaLow bench test, rather than N partial migrations each needing their own
  sign-off and each risking a mixed-state failure on the car.
- The old stack is a genuine operational rollback (not just a git tag or a
  design note) during the highest-risk period: the first real track outings
  on the new system.

**Negative**

- The old repository's secrets (NTRIP password, InfluxDB tokens, Grafana/
  admin passwords) still exist in that history and still must be rotated —
  this decision does not substitute for that work, it only prevents the
  *new* repo from inheriting the problem.
- Two codebases must be reasoned about during the transition window: the old
  one running the car, and the new one being built and validated. Porting
  pure modules (`timing_core`, `distance_model`, `reference_lap`) unchanged
  mitigates this for the timing engine, but the collectors, agent, and pit
  services are net-new and duplicate effort against the old system's
  equivalents until cutover.
- Big-bang sequencing means there is no incremental production feedback
  until the full system, the garage bench test, and the track shakedown —
  a defect touching several components at once is more likely to surface
  late. Replay validation mitigates this for logic bugs but cannot exercise
  real RF link conditions.
- Keeping the old repository bootable as a rollback is itself a small
  ongoing maintenance burden for the duration of the transition.

## Amendment (2026-08-22)

Two changes to this decision's foundations, recorded together because the
second subsumes most of the first. The original text above is unchanged.

**Cutover is not gated on a verified rollback** (Phase 5, locked decision
5). The earlier plan carried a package that booted the predecessor stack to
confirm this ADR's claim that it "remains bootable as an operational
rollback". That package was dropped and the claim is not being relied upon.
This matters to the original reasoning: the strangler pattern was rejected
partly *because* "full retention of the old stack as rollback removes most
of the risk the strangler pattern is normally chosen to mitigate" —
removing the rollback removes that mitigation, and the rejection now rests
on one fewer leg than it did. What carries the risk instead is the
vehicle's JetStream file store (ADR 0002): the car keeps recording locally
and durably even if every pit service is broken, so the failure mode of a
bad first outing is a pit wall with no live view, not a lost session. The
residual exposure is a race weekend run without live telemetry, and
`docs/CUTOVER_RUNBOOK.md` states that plainly rather than implying a
fallback nobody has tested.

**There is no cutover event** (owner direction, 2026-08-22). The new stack
is treated as a fresh installation, validated end-to-end on the bench by
replaying recorded data, and then simply used. The old stack plays no part
in any procedure — it is not installed alongside, not kept warm, and not a
fallback; per the previous paragraph its bootability was already not being
relied upon. What survives of "big-bang" is only the sequencing claim that
mattered: the car runs the complete new system from its first session on
it, never a hybrid. The gates the original decision named — replay parity
plus the garage bench test — still gate that first session, and
`docs/CUTOVER_RUNBOOK.md` records their state with provenance.
