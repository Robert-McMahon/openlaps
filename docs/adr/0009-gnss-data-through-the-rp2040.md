# 0009: GNSS data through the RP2040, sharing one receiver port

## Status

Accepted, 2026-08-31. Extends [0008](0008-rp2040-gnss-receiver-interface.md).

## Context

ADR 0008 put the timing path on the RP2040 and left the data path where it
was: UM980 COM1 → an external USB-serial cable → a CH340 → `/dev/ttyUSB0`.
The receiver therefore reached the host twice, over two unrelated links.

The cable is the half that fails. On 2026-08-29 the CH340 re-enumerated 34
seconds after the agent started — `ch341-uart converter now disconnected`
followed immediately by `now attached`, keeping the same `ttyUSB0` name — and
the collector logged 181 errors over roughly 30 minutes before recovering on
its own. It did recover, so this is not a correctness bug; it is a connector
in a car that vibrates, in the same class as the antenna pigtail whose shield
failed twice in the same week.

**Moving the data path to a native host UART is not possible on this board.**
The X4's N100 exposes no GPIO, the 40-pin header is RP2040 I/O, and the only
UART the kernel probes — `ttyS4` at MMIO `0xfe040000` — is the internal
RP2040↔N100 link that 0008 already uses. There is no external host-UART wire
to move to. The choice is the cable or the RP2040.

## Decision

Route the GNSS **data** path through the RP2040 as well, and let time and data
share one receiver port.

- The receiver connects on **COM2 only**: `TX2` → GPIO5 (`uart1` RX), `RX2` ←
  GPIO4 (`uart1` TX), PPS → GPIO2. One connector, one wire pair plus PPS.
- The RP2040 relays that byte stream to the host over its **internal USB CDC**
  (`/dev/ttyACM0`), and relays RTCM corrections and startup commands back the
  other way. Bytes pass unmodified; the agent's transport does its own framing.
- **Timing keeps its own transport** — `TH1` lines on `uart0` → `ttyS4`,
  unchanged shim, unchanged chrony config, so it retains the UART path's
  latency rather than inheriting USB's.
- The external USB-serial cable and the CH340 are removed entirely.

**This reverses 0008's consequence that "the time path and the data path stop
competing for the receiver's serial port".** They now share COM2 deliberately.
That is safe because the contention 0008 was avoiding was *exclusive port
ownership* — `gpsd` and the collector could not both hold the port — and that
problem does not arise here: one reader, the RP2040, owns the port and fans
out. Bandwidth was the other worry and it is not close: 50 Hz RMC is ~40 kbps
plus ~1.5 kbps of ZDA/GGA, against 115200.

Nothing in the host command set changes. With the relay in place the host's
commands arrive *on* COM2, so `GPRMC 0.02` configures that port with no
explicit port argument.

## Alternatives considered

- **Harden the USB path instead** — a udev rule pinning the receiver by
  physical port, an FTDI adapter with a real serial number, strain relief.
  Cheaper and lower risk, and it was the recommendation until the cost of the
  alternative turned out to be low: both RP2040 hardware UARTs were only
  half-used, so the two directions needed were free and no PIO soft-UART was
  required. Rejected because it leaves the connector in place.

- **NMEA over `ttyS4` alongside `TH1`, eliminating USB entirely.** Rejected:
  two host consumers cannot share one tty, so it needs a splitter daemon; it
  depends on GPIO1's host→RP2040 direction being wired, which the firmware has
  never configured and which no jumper can change; and it puts data and timing
  on one 115200 link. The internal CDC is an on-PCB bus, not the connector
  that failed.

- **A different SBC with host-accessible GPIO.** Out of scope for the same
  reason 0008 gave: the X4 is the deployed hardware.

## Consequences

**Positive**

- The external USB connector — the observed failure — is gone. The receiver
  reaches the host once, over links that are all either on-PCB or short
  point-to-point wiring inside the enclosure.
- One connector to the receiver instead of two, which is one fewer thing to
  wire, strain-relieve and get wrong.
- The RP2040 can now *write* to the receiver, which makes a hardware reset via
  the `EN` pin possible where previously the only recovery was physical.

**Negative**

- **The RP2040 becomes a single point of failure for position and time.**
  Previously a cable fault lost position and an RP2040 fault lost time,
  independently. This is the real cost, and it is why timing keeps a separate
  transport and codepath rather than being multiplexed into the relay.
- **Firmware now has a real-time obligation it did not have.** Emitting `TH1`
  blocks ~5 ms at 115200, which at 50 Hz spans ~20 bytes against a 32-byte
  FIFO, so receiver bytes must be drained by interrupt. Interrupt priorities
  are now explicit — PPS above UART above USB — where the RP2040 default of
  equal priority silently let a UART handler delay the PPS timestamp.
- **The receiver's 5 V no longer comes from the USB cable.** It must be fed
  from the board's rail; measured draw is 0.15–0.19 A at 5 V on a partial sky
  view, against a 1.1 W manufacturer figure.
- **The receiver's port configuration is applied by the agent**, so a receiver
  that power-cycles while the agent is down emits nothing on COM2 — and since
  ZDA/GGA gate the GNSS receiver interface, chrony loses its refclock too. Time would then
  depend on the telemetry agent having run, which 0008 did not intend. Found
  the hard way on 2026-08-31: the receiver had never actually lost power, so
  the agent had been silently re-applying the configuration on every start.

  Resolved by persisting the log set on the receiver with `SAVECONFIG`, once,
  by hand. The profile stays authoritative — `configure_on_start` still
  re-applies it on every agent start — and the saved copy is only the power-on
  default for when the agent is not there. **`SAVECONFIG` is deliberately not
  in the driver's startup commands**: it writes the receiver's NVM, and an
  agent in a crash loop would then wear that flash out. The cost is that a
  profile change makes the saved default stale until someone re-saves, which
  is the right trade for a value that only matters when the agent is absent.
- Reflashing resets the firmware's sequence counter, which the shim reads as
  stale; the shim must be restarted as part of flashing.
