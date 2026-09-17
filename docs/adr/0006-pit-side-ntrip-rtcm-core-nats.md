# 0006: Pit-side NTRIP client, RTCM over core NATS

## Status

Accepted, 2026-07-26

## Context

The current system's vehicle-side process holds NTRIP credentials directly
and fetches RTK correction data from an NTRIP caster over the internet from
the vehicle itself. This requires the vehicle to have independent internet
reachability, which it does not reliably have; the pit does, via its own
backhaul. The credentials involved are environment-baked and present in the
the legacy implementation's history, which is one of the concrete
secrets-hygiene problems the rewrite (and ADR 0001's clean-history decision) exists to
address — the leaked NTRIP password specifically needs rotation regardless
of what the new system does.

Separately, the GNSS receiver (a UM980) needs a startup configuration
routine (applying the target fix rate and sentence set) that only makes
sense running on the vehicle, next to the physical receiver.

## Decision

Run the NTRIP client at the **pit**, which has the internet backhaul, as a
`ntrip-client` pit service. RTCM correction bytes are shipped vehicle-ward
over **core NATS** (not JetStream) on subject `rtcm.<vehicle>`, which gives
**at-most-once** delivery: correction bytes are not stored, retried, or
replayed after a gap. NTRIP credentials therefore live only in the pit
`ntrip-client` service's environment and never touch the vehicle. The UM980
startup configuration routine (rate/sentence setup on boot) and RTCM
write-back to the receiver's serial port remain vehicle-side, implemented as
the serial collector's `um980` device driver.

## Alternatives considered

- **Keep the NTRIP client on the vehicle (status quo).** Rejected on two
  independent grounds: it requires internet connectivity on the vehicle,
  which the vehicle doesn't reliably have while the pit does; and it
  requires NTRIP credentials to live on vehicle-side storage, which is
  exactly the class of problem (credentials baked into environment/config
  that ends up in the repo or on the device) that this rewrite's
  secrets-hygiene work is meant to close off.

## Consequences

**Positive**

- NTRIP credentials exist in exactly one place — the pit `ntrip-client`
  environment — never on vehicle-side storage or in any vehicle-side config
  that could be lost or exposed along with the car.
- Using core NATS rather than JetStream for RTCM correctly matches the
  semantics of the data: stale RTK corrections are worthless, so there is no
  value in the redelivery/replay behaviour JetStream would otherwise
  provide, and it would be actively wrong to apply an old correction after a
  reconnect.
- The UM980 driver's surface stays small and isolated — startup
  configuration plus RTCM pass-through only — making it the one
  intentionally device-specific piece of code in the collector layer, kept
  narrow so that supporting a different receiver in future is a new driver,
  not an architecture change.

**Negative**

- RTCM delivery is at-most-once by design: during a link dropout the
  vehicle simply receives no corrections until the leafnode reconnects,
  with no buffering or catch-up for this subject. This is a real, if
  usually brief, accuracy hit to RTK fix quality — and therefore to
  GPS-derived timing precision — during any dropout.
- GNSS correction quality now depends on the pit link even in situations
  where, previously, a vehicle with its own internet access would have been
  self-sufficient. If the pit's own backhaul goes down, RTCM corrections
  stop entirely regardless of whether the vehicle-pit link itself is fine —
  a new failure mode that did not exist when the vehicle sourced NTRIP
  directly.
