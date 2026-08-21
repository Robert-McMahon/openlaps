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

## Default pins

All signals are 3.3 V TTL. Connect grounds before signals; do not put RS-232
levels into the RP2040.

| Signal | RP2040 GPIO | X4 v1.110 header | Older X4 header | Direction |
| --- | ---: | ---: | ---: | --- |
| UM980 PPS, active-high | GPIO2 | pin 38 | pin 22 | input |
| UM980 COM2 TX (`ZDA` + `GGA`) | GPIO5 / UART1 RX | pin 11 | pin 6 | input |
| Ground | GND | any GND | any GND | shared |

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
