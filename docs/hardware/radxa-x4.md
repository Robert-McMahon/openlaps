# Radxa X4

The Radxa X4 is the default vehicle target. Its values are the defaults in
`deploy/vehicle-compose.yaml`, so it needs no Compose overlay.

## Connections

| Function | Path | Notes |
| --- | --- | --- |
| CAN | `can0` | USB/CAN adapter |
| GNSS data | `/dev/ttyACM0` | UM980 relayed through the onboard RP2040 |
| Timing | `/dev/ttyS4` | RP2040 `TH1` output to the chrony shim |
| Camera | `/dev/video0` | USB UVC |
| Encoder | `/dev/dri/renderD128` | Intel VAAPI |

GNSS data and timing deliberately use separate paths. The agent opens
`/dev/ttyACM0`; `gnss_receiver_interface_shim.py` opens `/dev/ttyS4`. Do not
combine them.

## RP2040 GNSS receiver interface

The onboard RP2040 pairs each UM980 PPS edge with the following ZDA sentence
and emits a `TH1` timing record. The deployed UART-relay firmware also carries
GNSS data and receiver commands over USB CDC. It replaces the board's stock
GPIO firmware.

### Deployed artifact

`firmware/gnss-receiver-interface/openlaps-gnss-receiver-interface-uart-relay.uf2`
is the deployed build. Timing appears at `/dev/ttyS4`; GNSS data appears at
`/dev/ttyACM0`. There is no external host-UART wire: `/dev/ttyS4` is the X4's
internal RP2040-to-N100 UART.

### Wiring

All signals are 3.3 V TTL and require a shared ground.

| Signal | RP2040 pin |
| --- | --- |
| UM980 PPS | GPIO2 |
| UM980 COM2 RX into RP2040 | GPIO5 / UART1 RX |
| RP2040 TX to UM980 COM2 | GPIO4 / UART1 TX |

Power the WTRTK-980 carrier from the board's 5 V rail. Do not power a bare
UM980 from 5 V.

### Build

Requires Pico SDK 2.2.0, CMake and `arm-none-eabi-gcc`.

```bash
cmake -S firmware/gnss-receiver-interface \
  -B build/gnss-receiver-interface-uart \
  -DPICO_SDK_PATH="$HOME/pico-sdk" \
  -DPICO_BOARD=pico \
  -DTIMING_TRANSPORT=UART
cmake --build build/gnss-receiver-interface-uart -j
```

For the older USB timing-only build, explicitly disable the relay:

```bash
cmake -S firmware/gnss-receiver-interface \
  -B build/gnss-receiver-interface-usb \
  -DPICO_SDK_PATH="$HOME/pico-sdk" \
  -DPICO_BOARD=pico \
  -DTIMING_TRANSPORT=USB \
  -DGNSS_DATA_RELAY=OFF
cmake --build build/gnss-receiver-interface-usb -j
```

### Flash and verify

Flashing replaces the X4's stock RP2040 firmware. Stop consumers, enter
BOOTSEL, copy the UF2, then restart the shim:

```bash
sudo systemctl restart gnss-receiver-interface-shim.service
chronyc sources -v
```

Restarting is mandatory because a reflash resets the firmware sequence
counter; a running shim otherwise rejects new records as stale until the
counter catches up.

### Install the host shim

Install the receiver-interface shim and its shared chrony SOCK encoder
together:

```bash
sudo install -D -o root -g root -m 0755 \
  tools/gnss_receiver_interface_shim.py \
  /usr/local/libexec/openlaps/gnss_receiver_interface_shim.py
sudo install -D -o root -g root -m 0644 tools/chrony_sock.py \
  /usr/local/libexec/openlaps/chrony_sock.py
sudo install -m 0644 \
  deploy/systemd/gnss-receiver-interface-shim.service \
  /etc/systemd/system/
sudo install -d /etc/systemd/system/chrony.service.d
sudo install -m 0644 deploy/systemd/chrony-openlaps-sock.conf \
  /etc/systemd/system/chrony.service.d/openlaps-sock.conf
```

Set `GNSS_RECEIVER_INTERFACE_ARGS=--device /dev/ttyS4 --chrony-socket
/run/chrony/openlaps-timing.sock` in
`/etc/openlaps/gnss-receiver-interface.env`, then restart chrony and enable
the shim.

## Video

`go2rtc.yaml` defines H.265 `car` and H.264 `car_h264`. The dashboard defaults
to H.264 because common Linux Chromium builds cannot decode HEVC. Consume one
stream at a time because both require the same camera.
