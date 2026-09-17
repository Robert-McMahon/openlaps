# Luckfox Omni3576

The Luckfox Omni3576 target uses an RK3576, the on-SoC CAN controller, a direct
UM980 UART and GPIO PPS. Its current deployment is systemd for the agent and
CAN setup, plus plain `docker run` for NATS and go2rtc. The board's shipped
Docker 20.10 has no working Compose plugin.

## Required kernel support

The stock image does not expose the required UART or PPS path. The target's
`openlaps.dtsi` enables UART2 on header pins 8/10 and `pps-gpio` on pin 11.
Build the Rockchip kernel with:

```text
CONFIG_PPS_CLIENT_GPIO=y
CONFIG_NVME_HWMON=y
```

Back up the boot partition before writing a rebuilt `boot.img`. After reboot,
verify the running build and the required devices:

```bash
uname -v
zcat /proc/config.gz | grep -E 'NVME_HWMON|PPS_CLIENT_GPIO'
ls /dev/ttyS2 /dev/pps0
```

## Connections

| Function | Path or header |
| --- | --- |
| UM980 PPS | header pin 11, GPIO3_A2 |
| Board TX to UM980 RX | header pin 8 |
| UM980 TX to board RX | header pin 10 |
| CAN | `can0`, on-SoC controller |
| Camera | stable `/dev/v4l/by-id/` path |
| PPS | `/dev/pps0` |

Use the WTRTK-980 carrier's regulated 5 V input. A bare UM980 is a 3.3 V device
and must not be connected to 5 V.

## Runtime differences

- CAN must be brought up in FD mode with a 2 Mbit/s data bitrate even when the
  car sends classic frames; `hardware.yaml` contains this requirement.
- Install `vehicle-pps.conf` and the PPS udev rule so chrony can read
  `/dev/pps0` after dropping privileges.
- Video is H.264 only through Rockchip MPP.
- Audio is captured through the host PulseAudio/PipeWire socket.
- Put the Docker data root and NATS store on the NVMe, and use
  `RequiresMountsFor=/srv/openlaps` so a missing disk cannot silently redirect
  writes to eMMC.

The full board bring-up commands remain beside the target configuration until
they are converted into an installation script.
