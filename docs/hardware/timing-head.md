# RP2040 GNSS timing head

The Radxa X4's RP2040 pairs each UM980 PPS edge with the following ZDA sentence
and emits a `TH1` timing record. The deployed UART-relay firmware also carries
GNSS data and receiver commands over USB CDC.

## Deployed artifact

`firmware/timing-head/openlaps-timing-head-uart-relay.uf2` is the deployed
build. Timing appears at `/dev/ttyS4`; GNSS data appears at `/dev/ttyACM0`.
There is no external host-UART wire: `/dev/ttyS4` is the X4's internal
RP2040-to-N100 UART.

## Wiring

All signals are 3.3 V TTL and require a shared ground.

| Signal | RP2040 pin |
| --- | --- |
| UM980 PPS | GPIO2 |
| UM980 COM2 RX into RP2040 | GPIO5 / UART1 RX |
| RP2040 TX to UM980 COM2 | GPIO4 / UART1 TX |

Power the WTRTK-980 carrier from the board's 5 V rail. Do not power a bare
UM980 from 5 V.

## Build

Requires Pico SDK 2.2.0, CMake and `arm-none-eabi-gcc`.

```bash
cmake -S firmware/timing-head -B build/timing-head-uart \
  -DPICO_SDK_PATH="$HOME/pico-sdk" \
  -DPICO_BOARD=pico \
  -DTIMING_TRANSPORT=UART
cmake --build build/timing-head-uart -j
```

For the older USB timing-only build, explicitly disable the relay:

```bash
cmake -S firmware/timing-head -B build/timing-head-usb \
  -DPICO_SDK_PATH="$HOME/pico-sdk" \
  -DPICO_BOARD=pico \
  -DTIMING_TRANSPORT=USB \
  -DGNSS_DATA_RELAY=OFF
cmake --build build/timing-head-usb -j
```

## Flash and verify

Flashing replaces the X4's stock RP2040 firmware. Stop consumers, enter BOOTSEL,
copy the UF2, then restart the shim:

```bash
sudo systemctl restart timing-head-shim.service
chronyc sources -v
```

Restarting is mandatory because a reflash resets the firmware sequence counter;
a running shim otherwise rejects new records as stale until the counter catches
up.

## Install the host shim

Install the timing shim and its shared chrony SOCK encoder together:

```bash
sudo install -D -o root -g root -m 0755 tools/timing_head_shim.py \
  /usr/local/libexec/openlaps/timing_head_shim.py
sudo install -D -o root -g root -m 0644 tools/chrony_sock.py \
  /usr/local/libexec/openlaps/chrony_sock.py
sudo install -m 0644 deploy/systemd/timing-head-shim.service \
  /etc/systemd/system/
sudo install -d /etc/systemd/system/chrony.service.d
sudo install -m 0644 deploy/systemd/chrony-openlaps-sock.conf \
  /etc/systemd/system/chrony.service.d/openlaps-sock.conf
```

Set `TIMING_HEAD_ARGS=--device /dev/ttyS4 --chrony-socket
/run/chrony/openlaps-timing.sock` in `/etc/openlaps/timing-head.env`, then
restart chrony and enable the shim.
