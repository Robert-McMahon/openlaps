# Radxa X4

Intel N100, x86-64. The board the vehicle stack was written and measured on,
which is why the defaults compiled into `deploy/vehicle-compose.yaml` are
this target's: an X4 works with no `target.env` sourced at all.

## Wiring

| | Where | Notes |
| --- | --- | --- |
| CAN | `can0` | USB/CAN adapter |
| GNSS | `/dev/ttyACM0` | UM980 data relayed by the onboard RP2040 (ADR 0009), `openlaps-timing-head-uart-relay.uf2` |
| Camera | USB UVC on `/dev/video0` | 4:2:2 MJPEG, with the cabin mic on the same device |
| Audio | ALSA `plughw:0,0` | the camera's mic — and it only delivers samples while its video stream is running, which is why `go2rtc.yaml` keeps both in one ffmpeg process |
| Encode | Intel iGPU, VAAPI on `/dev/dri/renderD128` | H.265 (`car`) and H.264 (`car_h264`) |

## Two serial paths, and only one of them is the agent's

The relay firmware splits them on purpose (ADR 0009):

- **`/dev/ttyACM0`** — GNSS *data*, RP2040 USB CDC. This is `serial0.port`
  in `hardware.yaml` and `OPENLAPS_SERIAL_DEVICE` in `.env`.
- **`/dev/ttyS4`** — the timing head's `TH1` lines over the internal
  N100↔RP2040 UART, read by `tools/timing_head_shim.py` and handed to chrony
  as a SOCK refclock. The agent never opens it, and nothing in this
  directory configures it; `deploy/systemd/timing-head-shim.service` and
  `/etc/openlaps/timing-head.env` do.

Do not merge them to save a device node. Timing keeps the UART path
deliberately, to keep its latency rather than inherit USB's.

External wiring for the timing head itself (UM980 PPS and COM2 into RP2040
GPIOs) is in `deploy/README.md` → "GNSS timing head and clock discipline" and
`firmware/timing-head/README.md`; it is a property of the timing head rather
than of this board's target files.

## Video

`go2rtc.yaml` defines both `car` (H.265) and `car_h264`. Measured on real
camera footage, 500k HEVC matches 800k H.264 (SSIM 0.9804 vs 0.9800) — worth
a ~40% cut in the link budget's video share, since the encode is hardware
either way.

The pit dashboard nonetheless defaults to `car_h264`: Chromium on Linux has
no platform HEVC decoder, so the pit itself is a viewer that cannot play
`car`. Do not consume both at once — one process holds `/dev/video0` and the
loser's viewer sits on "loading" forever with no error.
