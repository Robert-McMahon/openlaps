# 0007: Private companion repo for proprietary-format exports and live vehicle profile

## Status

Accepted, 2026-07-26

## Context

Two categories of content need to stay out of the public repository
entirely, rather than merely being sanitized within it:

- The current system includes export tooling that writes session data into
  a proprietary third-party analysis-tool format, for use with that tool's
  free-tier analysis application. Using generated files of that format with
  that application in a public, redistributable open-source project raises
  usage-rights concerns under that tool's end-user license terms for
  third-party-generated data. Including the export code, or documentation
  of the format's internals, in the public tree carries that concern with
  it even if the tool itself is never bundled or invoked from CI.
- Separately, the team's actual day-to-day deployment — its live channel
  catalog, real operational credentials, and the specific hardware roster in
  current use — is a different thing from the example profile meant to
  document how the platform is configured. Conflating them risks a live
  credential, or an operational detail that shouldn't be public, leaking
  into the repository via what's supposed to be a documentation example.

## Decision

The public `openlaps` repository contains only the generic platform plus one
example profile (`profiles/example-club-racer/` — DBCs, catalog, and track
files from the initial deployment, checked in purely as documentation of how
the platform is configured, with no live credentials and no operationally
sensitive data). Anything encumbered lives instead in a **private companion
repository**: any proprietary-format analysis-tool export tooling, real
operational credentials, and the team's actual, currently-in-use channel
catalog. The private repository reads from the public platform's
TimescaleDB output as a downstream consumer; it does not duplicate platform
code, and the public repository contains no code, documentation, or even
passing references naming the third-party export format or the tool it
targets.

## Alternatives considered

- **Keep everything in one repository and exclude only the sensitive parts**
  (export tooling gated behind a flag, real credentials via `.env` only).
  Rejected: a single repository cannot cleanly separate "safe to have ever
  existed in this history" from "not," which is exactly the property ADR
  0001 establishes for the platform as a whole. Keeping the proprietary
  export code path present in the public source tree — even inert, never
  invoked in CI, gated behind a flag — still means the public repository
  distributes code whose purpose is to interoperate with a specific
  third-party tool under license terms that raised the concern in the first
  place; excluding it from the tree entirely is the only way to avoid that.

## Consequences

**Positive**

- The public repository can be shared, cloned, and reviewed by anyone with
  no usage-rights ambiguity about any tooling it contains.
- The "the car is just an example profile" framing from ADR 0001 is
  reinforced structurally, not just by convention: the actual, live,
  in-use profile is literally not present in the public repository, so
  there is no path for a live credential or an operational detail to leak
  out through what is meant to be a documentation example.
- Third-party contributors to the public platform never need access to
  anything private in order to work on the platform itself.

**Negative**

- Two repositories must be kept conceptually compatible: schema or format
  changes on the public platform side (e.g. a TimescaleDB migration) must
  remain readable by whatever the private repository's export tooling
  expects, with no automated check spanning both repositories.
- The private repository's export functionality is not available to other
  users of the public platform — anyone else wanting equivalent export
  capability for their own deployment has to build it themselves against
  the public schema; the public project provides the platform, not this
  particular export path.
- There is some risk of the private repository copying rather than cleanly
  depending on public platform code for convenience, which would need
  periodic reconciliation to avoid drifting out of sync.
