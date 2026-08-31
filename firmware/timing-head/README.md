# RP2040 GNSS timing head

This firmware pairs the UM980 PPS edge with the `ZDA` sentence that follows
it, carries the firmware-known edge-to-transmit delay to the host, and emits
one line per valid UTC second. `GGA` fix quality gates output: no current fix,
no timing sample.

The committed `openlaps-timing-head-usb.uf2` is the USB CDC build (SHA-256
`19d311dedbaa34215d04cebce9f438df73e07ed7df5c21e0b8f07652a44e117a`).
Rebuilding with `TIMING_TRANSPORT=UART` selects the direct RP2040-to-N100
UART instead. Neither transport is preferred until the one-hour
characterisation in `docs/BENCH_RUNBOOK.md` has been run. Both builds use the
same line protocol:

```
TH1 <uint32-sequence> <UTC-unix-second> <edge-us> <edge-to-transmit-us> <0|1>
```

`tools/timing_head_shim.py` timestamps line arrival with `CLOCK_REALTIME`,
subtracts `edge-to-transmit-us`, and sends a full offset sample to chrony's
SOCK refclock. It rejects malformed, invalid, late and stale-sequence input.

## The data relay

`GNSS_DATA_RELAY` (default `ON`) additionally carries the receiver's NMEA to
the host and the host's RTCM corrections and startup commands back, over the
RP2040's **internal** USB CDC — an on-PCB bus, not the external cable that
used to run to a CH340. It exists so the receiver reaches the host once
instead of twice.

It is a second channel, not a second use of the first: `TH1` lines and NMEA
have different consumers on the host and cannot share one device node. So the
relay requires `TIMING_TRANSPORT=UART`, and both CMake and `main.c` refuse the
combination rather than let it be discovered at runtime. Timing goes to
`/dev/ttyS4`, data to `/dev/ttyACM0`.

Two properties the implementation is built around:

- **Timing wins.** The `TH1` write stays blocking and immediate, because its
  `edge-to-transmit-us` is measured immediately before transmission — queueing
  it would make the figure the host subtracts a lie. The relay is therefore
  the side that must never block: it moves a bounded number of bytes per pass
  and drops the oldest queued NMEA if the host stops reading.
- **The receiver's bytes survive that write.** Emitting `TH1` blocks for about
  5 ms at 115200, which at 50 Hz spans ~20 bytes against a 32-byte FIFO. So
  `UART1` RX is drained by interrupt into a ring and the main loop reads the
  ring, rather than racing the FIFO.

Bytes are relayed unmodified rather than reassembled into lines: the agent's
transport does its own framing, so passthrough means sentences this firmware
does not parse still reach it, as do the command acknowledgements the UM980
driver reads back.

Note that with the relay in place the host's commands arrive **on COM2**, so
`GPRMC 0.02` and friends configure that port without needing an explicit port
argument — the driver's existing command set is unchanged.

No relay `.uf2` is committed yet. The committed USB build predates the relay
and remains the BOOTSEL recovery artefact; build the relay variant yourself
until it has been verified against hardware, at which point commit it with its
SHA-256 the way the USB build records one.

## Default pins

All signals are 3.3 V TTL. Connect grounds before signals; do not put RS-232
levels into the RP2040.

| Signal | RP2040 GPIO | X4 v1.110 header | Older X4 header | Direction |
| --- | ---: | ---: | ---: | --- |
| UM980 PPS, active-high | GPIO2 | pin 38 | pin 22 | input |
| UM980 COM2 TX (`ZDA` + `GGA` + data) | GPIO5 / UART1 RX | pin 11 | pin 6 | input |
| UM980 COM2 RX (RTCM + commands) | GPIO4 / UART1 TX | check pinout | check pinout | output |
| Ground | GND | any GND | any GND | shared |

`GPIO4` is only wired when the data relay is built (see below). Its header
pin is deliberately not tabulated — GPIO5's neighbour on the connector is not
GPIO4 on either revision, so read it off the X4 pinout for the board in hand.

**The receiver's 5 V supply does not come from the RP2040.** Those are 3.3 V
I/O with a 12 mA ceiling. Take VCC from the header's 5 V pins, which come
from the board rail: budget ~200–250 mA for a UM980 plus an active antenna's
LNA bias, and put bulk decoupling at the connector. On the WTRTK-980 carrier
the second connector is **7** PPS, **8** VCC (5 V), **9** `RX2`, **10**
`TX2`, **11** GND, **12** EN.

There is **no external host-UART wire**. On the UART firmware build, the
RP2040 sends on its internally connected UART0 TX/GPIO0 to the N100, where it
appears as `/dev/ttyS4`. GPIO0 and GPIO1 are reserved for that onboard link.
The USB build instead appears as `/dev/ttyACM0`. Check the X4 revision before
using the physical header-pin numbers; the RP2040 GPIO numbers are unchanged.

The receiver startup driver applies the manual's PPS form (Reference Commands
Manual V2 EN R1.14, printed page 56):

```
CONFIG COM2 115200 8 N 1
CONFIG PPS ENABLE GPS POSITIVE 500000 1000 0 0
GPZDA COM2 1
GPGGA COM2 1
```

`ENABLE`, rather than `ENABLE2` or `ENABLE3`, prevents PPS before the receiver
has converged. GGA provides immediate explicit fix-loss gating during the
receiver's documented PPS holdover.

## Build

Requires pico-sdk 2.2.0, CMake, and `arm-none-eabi-gcc`:

```
git clone --depth 1 --branch 2.2.0 --recurse-submodules \
  https://github.com/raspberrypi/pico-sdk.git ~/pico-sdk
cmake -S firmware/timing-head -B build/timing-head-usb \
  -DPICO_SDK_PATH="$HOME/pico-sdk" -DPICO_BOARD=pico -DTIMING_TRANSPORT=USB
cmake --build build/timing-head-usb -j

cmake -S firmware/timing-head -B build/timing-head-uart \
  -DPICO_SDK_PATH="$HOME/pico-sdk" -DPICO_BOARD=pico -DTIMING_TRANSPORT=UART
cmake --build build/timing-head-uart -j
```

Each build produces `openlaps-timing-head.uf2`. The host-native association
test is also part of ordinary pytest; it proves that the following sentence
is paired, a preceding sentence is not, and delays over 900 ms are rejected.

## Flash

Flashing replaces the Radxa X4's stock RP2040 GPIO firmware.

1. Stop services using the RP2040 device.
2. Put the RP2040 into BOOTSEL mode per the Radxa X4 procedure so its mass
   storage device appears.
3. Copy `openlaps-timing-head-usb.uf2` onto that volume and wait for it to
   unmount/reboot.
4. Install the vehicle chrony configuration, its systemd socket-permission
   drop-in, and the shim service exactly as documented in `deploy/README.md`,
   then start chrony and the shim.
5. Verify `chronyc sources -v` selects `GPS` and pull/restore PPS to exercise
   fallback without using `chronyc makestep`.
