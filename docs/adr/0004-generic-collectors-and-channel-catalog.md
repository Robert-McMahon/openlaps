# 0004: Generic collectors and a channel catalog

## Status

Accepted, 2026-07-26

## Context

The old repo's `gps_processor.py` is the concrete counter-example this
decision is written against: a single 592-line class that mixes serial I/O,
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
