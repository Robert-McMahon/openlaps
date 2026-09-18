# Hardware targets

A vehicle profile describes the car. A target describes the computer attached
to it: device names, CAN link settings, host sensors and video encoding.

## Supported targets

| Target | Architecture | CAN | GNSS and clock | Video |
| --- | --- | --- | --- | --- |
| [Radxa X4](radxa-x4.md) | Intel N100, x86-64 | USB/CAN `can0` | UM980 through RP2040 GNSS receiver interface | VAAPI H.265 and H.264 |
| [Luckfox Omni3576](luckfox-omni3576.md) | Rockchip RK3576, aarch64 | on-SoC CAN FD controller | UM980 UART plus GPIO PPS | Rockchip MPP H.264 |

## Target directory

```text
deploy/targets/<target>/
  hardware.yaml  agent and CAN host overlay
  target.env     deployment variables
  go2rtc.yaml    board-specific encoder pipeline
  compose.yaml   optional extra device mappings
```

`hardware.yaml` is consumed by the agent and `tools/can_up.py`. Video remains in
go2rtc configuration because the agent does not own the camera.

## Add a target

Copy the closest existing target and change only board-specific values. Validate
the overlay without privileges:

```bash
uv run tools/can_up.py --profile profiles/example-club-racer \
  --hardware deploy/targets/<target>/hardware.yaml --dry-run
uv run pytest tests/test_hardware.py
```

Add a Compose overlay only when the base vehicle stack lacks required device
nodes or mounts.
