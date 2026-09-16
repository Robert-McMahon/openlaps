# Vehicle targets

A **target** is one SBC the vehicle stack can run on. The car is described
once, in a profile (`profiles/<name>/`); the board it happens to be bolted to
is described here. Nothing in a profile changes when the car moves from one
board to another — the DBCs, the channel catalog and the receiver's settings
are properties of the car, and a socketCAN device name is not.

Two targets ship, and both are real hardware that has been measured, not
paper support:

| | `radxa-x4` | `luckfox-omni3576` |
| --- | --- | --- |
| SoC | Intel N100 (x86-64) | Rockchip RK3576 (aarch64) |
| CAN | USB/CAN HAT (`can0`) | on-SoC `rk3576_canfd` (`can0`) |
| GNSS | UM980 via the onboard RP2040 relay (ADR 0009) | UM980 on a SoC UART, or the same relay over USB |
| Video encode | Intel iGPU, VAAPI, H.265 + H.264 | Rockchip MPP, **H.264 only** |
| Cabin audio | ALSA `plughw:0,0` (camera mic) | PulseAudio/PipeWire (`ES8388` codec) |
| PPS discipline | RP2040 timing head → chrony SOCK | none in-tree; see its README |

## What a target directory holds

```
deploy/targets/<target>/
  README.md      what this board is, how it is wired, and what it cannot do
  target.env     the deploy-time variables the compose stack reads
  hardware.yaml  the agent's host wiring: which interface, which tty
  go2rtc.yaml    the encoder pipelines this board's silicon can actually run
  compose.yaml   optional: extra device nodes the base stack does not map
```

Each of the four is consumed by exactly one thing, which is why there are
four of them rather than one:

- **`hardware.yaml`** is read by the agent (`core.hardware`, ADR 0010) and by
  `tools/can_up.py`. It overlays `buses[].interface`, `buses[].bitrate`,
  `serial[].port` and `serial[].baud` onto the profile, addressing transports
  by the profile's own `name`, plus two host-only settings: a bus's `link:`
  block (`fd`, `dbitrate`, for a controller that needs them to come up at all)
  and a serial source's `driver.configure_on_start` ("is a real receiver on
  the other end of this port?"). It also carries `host.temperatures`, the
  board's answer to which thermal sensor stands behind each
  `host:temp.<alias>` the catalog maps -- a Luckfox has no `coretemp`, an X4
  no `soc_thermal`, and the channel names must not care. It carries no
  video: go2rtc reads its own config and the agent never touches a camera,
  so a `video:` block here would be configuration nothing consumes.

  The same file type serves a rig that is not a board at all —
  `tools/bench-hardware.yaml` points the Phase 4 bench at `vcan0` and a pty.
  It lives beside the bench tools rather than here, because a target is an
  SBC and that is a test fixture.
- **`target.env`** is read by `docker compose` — device paths to map, the
  go2rtc image variant, which `go2rtc.yaml` to mount.
- **`go2rtc.yaml`** is read by go2rtc, and is where the encoder choice lives.
  This is the file that genuinely cannot be shared between the two shipped
  targets: `hevc_vaapi` does not exist on a Rockchip and `h264_rkmpp` does not
  exist on an Intel.
- **`compose.yaml`** exists only where the base stack's device list is not
  enough. `radxa-x4` has none; `luckfox-omni3576` needs `/dev/mpp_service`
  and the PulseAudio socket.

## Using one

Append the target's variables to the deploy `.env`, once:

```bash
cat deploy/targets/luckfox-omni3576/target.env >> deploy/.env
```

Then bring the stack up with the target's compose overlay, if it has one:

```bash
docker compose -f deploy/vehicle-compose.yaml \
  -f deploy/targets/luckfox-omni3576/compose.yaml --profile video up -d
```

Running the agent directly instead of in a container (`deploy/systemd/`),
point it at the overlay and nothing else changes:

```bash
OPENLAPS_HARDWARE=/opt/openlaps/deploy/targets/luckfox-omni3576/hardware.yaml
```

`deploy/README.md` → "Bring-up" is the full sequence; this file is only the
board-shaped part of it.

## Adding a target

There is no registry to edit and no code to write. Copy the directory that is
closest to your board, change what differs, and check it:

```bash
uv run tools/can_up.py --profile profiles/<yours> \
  --hardware deploy/targets/<yours>/hardware.yaml --dry-run
```

(`--dry-run` needs no privileges. The real bring-up runs as root and is
`sudo ./.venv/bin/python tools/can_up.py ...` — see `deploy/README.md`.)

`tests/test_hardware.py` walks every directory under `deploy/targets/`, so a
new one is validated against the example profile by CI the moment it lands:
the overlay parses, and every transport it names is one the profile actually
defines. A hardware file that names `serial1` when the profile has only
`serial0` fails there rather than at the first port open on the car.

The defaults compiled into `deploy/vehicle-compose.yaml` are `radxa-x4`'s, so
that target needs no `target.env` sourced to work — it is the board the stack
was written on. Any other board sets the variables.
