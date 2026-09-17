# 0004: Generic collectors and a channel catalog

## Status

Accepted, 2026-07-26

## Context

The legacy implementation's GPS processor is the concrete counter-example
this decision is written against: a single 592-line class that mixes serial I/O,
an NTRIP client, NMEA parsing, calls into the timing engine, and MQTT
publishing. Adding a new GPS receiver, or moving GPS to a different
transport entirely (for example, a GPS module that reports over CAN instead
of a serial NMEA stream), means touching this file and its assumptions
directly. The old CAN ingestion path similarly bakes in a fixed, specific
DBC list. A separate module, `imu_combiner`, exists purely to reassemble
multi-frame IMU CAN data into named channels alongside GPS — a second,
smaller instance of the same domain-specific-collector pattern.

The new platform's stated goal is that any car is just a configuration
profile on top of a generic core, which requires that adding hardware (a
bus, a DBC, a serial device, a new sensor arriving over an existing
transport) be a configuration change, not a new class.

## Decision

Collectors are **generic transports only**, carrying no domain meaning:

- `can` — N instances (`can0..n`), each with its own SocketCAN interface and
  its own list of DBCs, each DBC mounted under a configured **device
  alias** (e.g. `haltech`, `imu`, `pd16`, all potentially on `can0`).
- `serial` — N instances (`serial0..n`): a port, a baud rate, a **decoder**
  (initially `nmea`), and an optional **device driver** for startup
  configuration and write-back (initially `um980`: applies rate/sentence
  configuration on start, and writes inbound RTCM bytes to the port).
- `host` — psutil-based system metrics.

A YAML **channel catalog** maps source refs, in the namespace
`bus:device.MESSAGE.SIGNAL` (e.g. `can0:haltech.ENGINE1.ENGINE_SPEED`), to
canonical channel names (`engine.rpm`) with units, datatype, and per-channel
link policy. Consumers — the lap-timing engine, dashboards, exports —
reference canonical names only, never source refs. Channels are assigned
integer IDs via a `ChannelRegistry` message, published Sparkplug-BIRTH-style
at agent start and whenever the catalog changes, so wire payloads carry
channel IDs, never name strings.

`imu_combiner` is deleted outright, not ported: under this model IMU frames
are simply DBC-decoded CAN signals like any other, reaching canonical names
through the same catalog mapping as everything else. There is no special
IMU code path anywhere in the new system.

## Alternatives considered

- **Per-domain collectors** (a GPS collector, an IMU collector, a CAN
  collector, each understanding its own domain) — this is what the old
  repo already does, and `gps_processor.py` is the counter-example cited
  above: mixing serial I/O, NTRIP, NMEA parsing, timing calls, and MQTT
  publishing in one class. Rejected because it requires a new collector
  class for every new sensor or domain, and hardcodes the transport
  assumption (e.g. "GPS is serial") into domain logic — so "GPS arrives
  over CAN instead" becomes a rewrite of the GPS collector rather than a
  one-line catalog edit.

## Consequences

**Positive**

- Adding a bus, a DBC, or a serial device is a configuration change
  (`vehicle.yaml` + `catalog.yaml`), not new code.
- New hardware for an existing domain — e.g. GPS arriving over CAN instead
  of serial — is a catalog mapping edit, not a change to any consumer
  (timing engine, dashboards, exports).
- `imu_combiner`'s entire reason to exist disappears; there is one fewer
  bespoke module and one fewer domain-specific code path to maintain.
- `ChannelRegistry` integer IDs mean wire payloads never repeat name
  strings, directly supporting the bandwidth reduction argued in ADR 0002.

**Negative**

- Adds a layer of indirection (source ref → catalog entry → canonical name
  → integer ID) that must stay internally consistent; a missing or wrong
  catalog mapping produces a silent "unmapped source ref" gap rather than an
  obvious code-level error.
- The catalog file itself becomes a single point of configuration
  correctness — achieving 1:1 coverage of every signal the old system
  currently publishes is an explicit acceptance requirement for the example
  profile, not a given.
- Registry lifecycle (publish on start, republish on catalog change, a
  sequence number carried in every batch header) is new protocol surface
  that must be implemented correctly for late-joining or reconnecting pit
  consumers to decode historical data with the right catalog version.

## Amendment (2026-07-27)

The example catalog originally grouped canonical channel names under fixed
top-level domains (`engine.*`, `chassis.*`, `wheels.*`, `fuel.*`,
`electrics.*`), documented in `docs/CATALOG.md`. In practice this
taxonomy invited exactly the kind of pointless categorization debate this
ADR's collector design was meant to avoid at the transport layer: does
throttle position belong to `engine.*` or `chassis.*`? Is ECU-calculated
vehicle speed `wheels.*` or `engine.*`? The old convention's own
"judgement calls worth a second look" section already listed several such
calls with no clean answer.

The owner decided to flatten every on-vehicle sensor/actuator reading into
a single `car.*` namespace (one level, snake_case), dropping the domain
sub-taxonomy entirely. `position.*` (GNSS), `sys.*` (host + agent health),
and the derived `lap.*` / `timing.*` namespaces are unaffected — they were
never part of the domain taxonomy this amendment removes. Where a bare,
flattened name would be ambiguous, the former domain word is folded into
the name itself instead of used as a prefix (e.g. `engine.demand` ->
`car.engine_demand`, `engine.limiting_active` ->
`car.engine_limiting_active`).

Alongside the flattening, the owner introduced a second catalog
principle: **name the measurement, not the wire.** Some source signals —
notably the PD16A power-distribution module's generic analog inputs
(`AVI1-4`, `SPI3-4`) and its per-output current/status/load telemetry
(`HBO1-2`, `HCO25_1-4`, `HCO8_1-10`) — have no fixed physical meaning; they
report whatever happens to be wired to that pin on a given car. Mapping
these to canonical names in the *example* profile would document a
wiring choice, not a measurement, and would mislead anyone copying the
example catalog for their own car. These entries were removed from
`profiles/example-club-racer/catalog.yaml` and replaced with a commented
template showing how to map one once its real-world meaning is known.
PD16A device-health diagnostics (its own temperature, battery voltage,
total current, per-driver-pair thermal status) are unaffected by this
change — they describe the module itself, not its wiring, and remain
mapped as `car.pd16_*` channels, alongside the CAN keypad's `car.keypad_*`
channels (a generic device, not wiring-dependent).

This amendment does not change anything else in this ADR's decision:
collectors remain generic transports, the catalog still maps source refs
to canonical names with units/type/link policy, and `ChannelRegistry`
integer IDs still carry those canonical names over the wire.
