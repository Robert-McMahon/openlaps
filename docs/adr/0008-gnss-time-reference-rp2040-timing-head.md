# 0008: GNSS time reference via an RP2040 timing head

## Status

Proposed, 2026-08-20

## Context

`docs/ARCHITECTURE.md` → "Link dropout and recovery" is explicit that
ingest-writer's `lag_ms` is *relative*: the vehicle and pit monotonic clocks
share no epoch, so anything wanting absolute source-to-row latency "has to
measure it end to end with a shared time reference". `docs/plan/PHASE4.md`
→ P4.3 asks for exactly that number. There is currently no such reference.

What the vehicle has instead is a free-running system clock. The one
mechanism that could have corrected it — `SteeredClock` in
`src/agent/clock.py` — is dormant and always has been: it steers only when
the canonical channel `position.time_unix_ms` is mapped
(`src/agent/pipeline.py`), the NMEA decoder emits no time field
(`src/collectors/serial/nmea.py`), and the example profile maps five
`position.*` channels with no time among them. `docs/BENCH_RUNBOOK.md` §3
already records this and draws the right conclusion for the bench: host
time discipline is the entire story.

That same section offers a "preferred" configuration — `refclock SHM 0` fed
by `gpsd` on the SBC — which cannot be built as written. The agent's
`SerialCollector` opens the receiver's port exclusively, and ADR 0006 puts
RTCM write-back on that same port. `gpsd` cannot have it.

ADR 0006 also settled that **the vehicle has no reliable internet** — that
is why the NTRIP client runs at the pit. A vehicle clock disciplined only by
internet NTP is therefore not a reference at a track; it is a reference in
the garage.

The deployed SBC is a Radxa X4. Its 40-pin header is an I/O expansion off
the onboard RP2040, not off the N100; the N100 exposes no GPIO that a PPS
edge can raise an interrupt on. The RP2040 reaches the host over an internal
USB 2.0 bus and over a UART. **Any PPS signal on this board must therefore
cross a link before it reaches the kernel**, and the properties of that link
— not the receiver, and not GPS — set the achievable accuracy.

## Decision

Build a **GNSS timing head** on the RP2040 and discipline the host clock
from it with `chrony`.

- The UM980's **PPS output** goes to an RP2040 GPIO, captured in hardware.
- A **spare UM980 COM port** feeds 1 Hz `ZDA` into an RP2040 UART, so the
  timing head can name the second the edge belongs to without depending on
  the host, the agent, or the internet.
- Firmware emits one message per second carrying the UTC second, the
  captured edge, and **its own edge→transmit interval**, so the host
  subtracts the part of the delay that is known rather than estimating all
  of it.
- A host shim feeds those samples to `chrony` as a **SOCK refclock**, which
  carries a full offset sample where SHM would carry less.
- `chrony` also holds internet NTP sources. **Its ordinary source selection
  is the primary/fallback behaviour** — the timing head is `prefer`red, and
  NTP takes over when it is absent. No custom failover code exists.
- The system clock becomes the vehicle's single time authority.
  `SteeredClock`'s GNSS steering is **retired rather than repaired**.
- Clock health is published as telemetry (`host:clock_*` through the host
  collector), on the same argument that makes drop counters channels.

**The accuracy this buys is set by the RP2040→host link, and is recorded as
a measured figure rather than assumed.** Indicative, to be replaced by P4.8's
measurement: USB CDC is full-speed only, so its 1 ms frame quantisation is a
floor (~1 ms absolute, ~0.5 ms jitter); the UART path is roughly one
character time (~90 µs at 115200, ~50 µs jitter). Kernel PPS on real GPIO
would be sub-µs and is not available on this hardware.

## Alternatives considered

- **`gpsd` + SHM refclock on the receiver's serial port** (what
  `BENCH_RUNBOOK.md` §3 currently prescribes). Rejected because it cannot be
  built: the port is exclusively owned by the collector, and ADR 0006 needs
  it for RTCM write-back. Making `gpsd` the agent's position source instead
  would put a daemon's own reporting cadence between the receiver and the
  timing engine — the exact class of accidental quantisation that
  `docs/bench/timing-parity.md` documents costing four orders of magnitude.

- **NMEA time only, no PPS** — add `ZDA` to the stream the agent already
  reads, teach the decoder a time field, and wake `SteeredClock` up.
  Rejected as the primary reference: serial sentence delivery jitters by
  tens of milliseconds and varies with host load. That is a coarse
  correction, not a reference. It remains available as a cheap secondary.

- **PPS into a USB-serial adapter's DCD line with `pps_ldisc`.** Rejected:
  modem-status changes on a USB-serial device are polled on an interrupt
  endpoint at the descriptor's interval, so this inherits the same USB frame
  quantisation as the RP2040 path while giving up the ability to timestamp
  at the source and to subtract a known delay.

- **Internet NTP only.** Rejected on ADR 0006's finding: the vehicle does
  not reliably have internet, which is the whole reason NTRIP moved to the
  pit. This remains the *fallback*, which is a different claim.

- **An external GPS-disciplined NTP appliance on the vehicle network.**
  Rejected: another box, another power draw and another thing to mount in a
  car, bought for accuracy that nothing in this system needs.

- **Different SBC with host-accessible GPIO.** Out of scope. The X4 is the
  deployed hardware and this decision is about getting a defensible
  reference out of it.

## Consequences

**Positive**

- The vehicle gains a bounded absolute time reference that is available at a
  track with no internet and is correct from a cold start with no RTC.
- The time path and the data path stop competing for the receiver's serial
  port. ADR 0006's RTCM write-back is untouched.
- There is exactly one thing steering time — `chrony` — instead of a host
  daemon and an in-process `SteeredClock` correcting toward the same source
  by different rules on different cadences.
- Primary/fallback is `chrony`'s source selection, so the failover path is
  one that is widely deployed and observable via `chronyc` rather than one
  this project wrote and has to test.
- P4.3's absolute source-to-row latency becomes reportable, and
  `BENCH_RUNBOOK.md` §3 gains a preferred configuration that exists.

**Negative**

- **The accuracy ceiling is a link, not GPS.** "GPS-disciplined" invites the
  reader to assume microseconds; on this board it is hundreds of
  microseconds at best and about a millisecond over USB. The measured figure
  goes in the manifest, and any document quoting it says which transport it
  came from.
- The repository acquires **firmware** — a pico-sdk toolchain, a build, a
  `.uf2` artefact and a flashing procedure — in a codebase that has so far
  been Python and configuration. That is a new maintenance surface and a new
  thing that can be out of date relative to the host shim it talks to.
- **Reflashing the RP2040 replaces Radxa's stock GPIO firmware.** Nothing in
  this stack currently uses the 40-pin header, but that ceases to be free:
  anything added to it later must be served by our firmware.
- The UM980's spare COM port must exist, be exposed on the carrier board,
  and be configured for 1 Hz `ZDA`. That configuration is vehicle startup
  state which the `um980` driver does not currently manage — today it
  configures one port's rate and sentence set.
- **The pit clock is unimproved and still dominates.** The pit disciplines
  over HaLow at millisecond class. This decision makes the vehicle's
  *stamp* good, which is what makes the stored data true; it does not make
  the pit's receive-side timestamps better, and P4.3's latency uncertainty
  will still be set by the pit end.
- One more wire, one more connector and one more silent failure mode in a
  car: a detached PPS lead degrades to NTP without complaint, which is
  correct behaviour and is exactly why the clock source has to be a
  telemetry channel rather than something discovered later in a log.
