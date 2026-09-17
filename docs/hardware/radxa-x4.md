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
`/dev/ttyACM0`; `timing_head_shim.py` opens `/dev/ttyS4`. Do not combine them.

## Video

`go2rtc.yaml` defines H.265 `car` and H.264 `car_h264`. The dashboard defaults
to H.264 because common Linux Chromium builds cannot decode HEVC. Consume one
stream at a time because both require the same camera.

## Timing head

The RP2040 firmware replaces the board's stock GPIO firmware. Follow the
[RP2040 timing-head guide](timing-head.md) for wiring, build, flash and chrony
integration.
