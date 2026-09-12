# Luckfox Omni3576

Rockchip RK3576, 8 cores, 8 GB, Debian 12, kernel 6.1.75 (Rockchip BSP).

Three things differ from the Radxa X4, and each has a section below: the CAN
controller only comes up in FD mode, video is H.264 only, and the stock image
exposes no usable UART until you flash a new device tree.

The target files here assume that flash has been done — `serial0.port` is
`/dev/ttyS2`. On a stock board, use a USB-serial adapter and `/dev/ttyUSB0`.

## Wiring

40-pin header, Raspberry-Pi-compatible, 3.3 V TTL. TX and RX cross.

Use the receiver's **COM1** — on a WTRTK-980 that is the *first* connector,
which carries PPS, power, ground and the UART together:

| WTRTK-980 pin | | Header pin | |
| --- | --- | --- | --- |
| 1 | `PPS` → | **11** | GPIO3_A2 |
| 2 | `VCC` ← | **2** or **4** | 5 V; see Power below |
| 3 | `RXD` ← | **8** | board TX (`uart2m1`) |
| 4 | `TXD` → | **10** | board RX (`uart2m1`) |
| 5 | `GND` ↔ | **6** | mandatory — PPS and the UART levels are both referenced to it |
| 6 | `EN` | — | high-level enable; see Power below |

**COM1, not COM2** — the X4 uses COM2, and that is the one thing not to copy
across. `_startup_commands` in `src/collectors/serial/um980.py` issues the
data sentences with no port argument, so they configure whichever port the
agent is speaking over, while the timing sentences are addressed to `COM2`
explicitly (`GPZDA COM2 1`, `GPGGA COM2 1`, `CONFIG COM2 ...`). Sitting on
COM1 keeps those two apart; sitting on COM2 would have the driver reconfigure
the link it is talking over and log 1 Hz ZDA/GGA into the agent's own stream.

The profile's `timing_output: COM2` is vestigial on this board — it still
configures COM2 to emit ZDA and GGA, and with no RP2040 here nothing reads
them. Harmless, and not worth editing out: the profile is shared with the X4,
which needs it.

Pins 8 and 10 need the device-tree change below. **Until then** the receiver's
COM1 goes to a USB-serial adapter and `serial0.port` stays `/dev/ttyUSB0`; PPS
on pin 11 works either way.

### Power

Which pin depends on whether the receiver is a bare UM980 or a carrier board
with its own regulator, and the two answers are opposites.

| | Supply | Draw | From |
| --- | --- | --- | --- |
| WitMotion **WTRTK-980** (the module in use) | 5 V (3.6–6 V in) | 220 mA typ per its manual; 0.15–0.19 A measured on the X4 | header **pin 2** or **4** |
| A **bare UM980** | 3.0–3.6 V, **3.6 V absolute max** | 180 mA max + up to 50 mA antenna | **its own LDO** — not header pin 1 |

The carrier board is the easy case: it regulates internally, so the UM980's
50 mV VCC ripple ceiling is its problem rather than the header's, and the
header's 5 V pins are the board's input rail rather than a regulated one.
~1.1 W on top of an 8-core SoC and an NVMe — check the supply has the
headroom, and note the receiver draws inrush at power-on.

A bare UM980 is the opposite: 5 V destroys it, and a 50 mV ripple ceiling on
a rail shared with SoC I/O during H.264 encode is not a rail to share. Give
that one a dedicated 3.3 V LDO, rated about twice its draw as the
[UM980 manual](https://docs.sparkfun.com/SparkFun_UM980_Triband_GNSS_RTK_Breakout/assets/component_documentation/UM980_Datasheet.pdf)
advises.

**Signal levels are 3.3 V TTL**, so `TXD` and `PPS` go straight to pins 10
and 11 with no level shifting. That is settled by the X4 rather than assumed:
`firmware/timing-head/README.md` wires the same receiver's PPS and COM2 TX
directly into RP2040 GPIOs, which are not 5 V tolerant, and has been running.
It also carries the WTRTK-980's connector pinout and the rest of the
receiver-side wiring — read it alongside this file rather than re-deriving it.

Put bulk decoupling at the connector; the 5 V comes off the board rail and
the run to the receiver is not short.

`EN` is a high-level enable. If the module does not start with it floating,
tie it to VCC.

Everything else:

| | Where | |
| --- | --- | --- |
| CAN | `can0`, on-SoC | `can0m2` is GPIO4_A4/A6 — its own connector, not the 40-pin header |
| Camera | USB UVC, the next free `/dev/videoN` | **not `/dev/video0`** — the MIPI paths hold `video0`–`video44`. `v4l2-ctl --list-devices`, then use `/dev/v4l/by-id/...` |
| Audio | `card 0`, ES8388 | captured over PulseAudio, not ALSA |
| Encode | `/dev/mpp_service` + `/dev/dri/renderD128` | H.264 only |

## Temperatures

The dashboard's three host temperatures come from `host.temperatures` in
`hardware.yaml` here (ADR 0010), and this board can fill in one of them.

The RK3576's TSADC exposes six zones, each a separate hwmon chip with one
unlabelled reading, so psutil names them `<zone>.0`:

| Zone | hwmon name | What it is |
| --- | --- | --- |
| `thermal_zone0` | `soc_thermal` | the governor's zone: throttling is decided on this one |
| `thermal_zone1` | `bigcore_thermal` | the four A72s |
| `thermal_zone2` | `little_core_thermal` | the four A53s |
| `thermal_zone3` | `ddr_thermal` | the DDR controller, on the SoC die |
| `thermal_zone4` | `npu_thermal` | |
| `thermal_zone5` | `gpu_thermal` | |

All six are on the die (they read within a degree of each other, idle at
about 28 °C), and `crit` is 115 °C on each. `cpu: soc_thermal.0` is the
mapping; the raw `host:temp.<zone>.0` refs are all emitted as well.

**Throttling and clocks.** The governor's cooling devices are `cpufreq-cpu0`
(the A53 cluster, 8 steps), `cpufreq-cpu4` (the A72 cluster, 9 steps),
`devfreq-dmc` (DDR, 3 steps), `devfreq-27800000.gpu` and
`devfreq-27700000.npu`; the host collector normalises each to a percentage
and rolls them up as `sys.host.throttle_*`. The two cpufreq policies are
`policy0` (cores 0–3, 408–2016 MHz) and `policy4` (cores 4–7, up to
2208 MHz); `sys.host.cpu_freq_max_mhz` / `_min_mhz` are the big and little
cluster respectively, since psutil's single figure averages the two into a
clock neither cluster runs at. The first critical trip on every zone is
115 °C; expect the cooling devices to start stepping well before that.

**No board sensor.** The only other hwmon chip is the USB-PD controller
(`tcpm_source_psy_2_004e`), which reports voltage and current, not
temperature. `board` is deliberately left unmapped rather than pointing at
`ddr_thermal` and calling an on-die reading a board temperature.

**NVMe: needs a kernel rebuild.** The drive (`ZHITAI TiPlus7100s`) reports
its temperature over SMART like any NVMe, and the kernel's `nvme` driver
would register it as an hwmon chip named `nvme` with a `Composite` reading --
the `nvme.composite` the mapping already names. But the BSP config has

```
# CONFIG_NVME_HWMON is not set
```

so no chip appears, and the agent logs once at start:

```
host: temperature 'nvme' maps to sensor 'nvme.composite', which this host does not expose (it exposes: bigcore_thermal.0, ddr_thermal.0, ...)
```

The fix goes in the same kernel config fragment that carries
`CONFIG_PPS_CLIENT_GPIO` (see "Enabling the hardware UART" -- it is
`docker.config`, passed as `RK_KERNEL_CFG_FRAGMENTS`):

```
CONFIG_NVME_HWMON=y
```

then `./build.sh kernel` and write `boot.img` to `mmcblk2p3` exactly as
for the UART. It is a config-only change to a driver already built in, and
the same reflash procedure and the same recovery apply. Until then the
`nvme` channel is simply absent on this board; `nvme-cli` (`nvme smart-log
/dev/nvme0`) reads the same value by hand, but it needs root and the agent
runs as `openlaps`, so it is not a substitute the collector can use.

## Bring-up

Get the repository onto the board first — everything below is run from its
root, and the relative paths assume that:

```bash
git clone <this repo> ~/openlaps && cd ~/openlaps
```

The agent and `tools/can_up.py` need this project's dependencies, so they run
through the venv. (`tools/pps_gpio_shim.py` does not — it is stdlib-only and
runs on Debian's `python3`.)

```bash
uv python install 3.13 && uv sync          # Debian ships 3.11; pyproject needs 3.13
cat deploy/targets/luckfox-omni3576/target.env >> deploy/.env
# edit .env: OPENLAPS_VIDEO_DEVICE is almost certainly not /dev/video0
sudo ./.venv/bin/python tools/can_up.py --profile profiles/<yours> \
  --hardware deploy/targets/luckfox-omni3576/hardware.yaml
docker compose -f deploy/vehicle-compose.yaml \
  -f deploy/targets/luckfox-omni3576/compose.yaml --profile video up -d
```

`deploy/README.md` → "Bring-up" is the rest, unchanged by the board, with one
exception: **the shipped image has Docker 20.10, which has no `compose`
subcommand** and no `docker-compose` either. `docker compose ...` there prints
the Docker help text and exits 0, so it looks like it worked. Either install
the compose plugin, or run the pieces with `docker run` — the vehicle's
nats-server needs only its config, the TLS directory and a store:

```bash
docker run -d --name openlaps-vehicle-nats --network host \
  --env-file deploy/.env \
  -v $PWD/deploy/nats/vehicle.conf:/etc/nats/nats.conf:ro \
  -v ~/openlaps-secrets/tls:/etc/nats/tls:ro \
  -v /var/lib/openlaps/nats:/data \
  nats:2.12-alpine -c /etc/nats/nats.conf
```

The agent can then run natively (`deploy/systemd/openlaps-agent.service`),
which also avoids building the image for aarch64.

go2rtc the same way. This is `vehicle-compose.yaml`'s `go2rtc` service plus
this directory's `compose.yaml` overlay, flattened -- every device and mount
below corresponds to a line in one of those two files, and the camera is the
`/dev/v4l/by-id/` path rather than a `/dev/videoN` that changes with
enumeration order (the MIPI pipeline holds `video0`-`video44`; a USB camera
lands at `video45` or later):

```bash
CAM=/dev/v4l/by-id/usb-Amba_Insta360_X3-video-index0   # v4l2-ctl --list-devices
docker run -d --name openlaps-vehicle-go2rtc --network host --restart unless-stopped \
  --device $CAM:/dev/video0 --device /dev/dri:/dev/dri --device /dev/snd:/dev/snd \
  --device /dev/mpp_service:/dev/mpp_service --device /dev/rga:/dev/rga \
  -v /opt/openlaps/deploy/targets/luckfox-omni3576/go2rtc.yaml:/config/go2rtc.yaml:ro \
  -v /run/user/1000/pulse/native:/run/pulse/native -e PULSE_SERVER=unix:/run/pulse/native \
  alexxit/go2rtc:1.9.14-rockchip
```

Prove it from the board before involving the pit: pull the stream for ten
seconds, then ask go2rtc what the producer negotiated.

```bash
curl -s -m 10 "http://127.0.0.1:1984/api/stream.mp4?src=car_h264" -o /tmp/probe.mp4
curl -s "http://127.0.0.1:1984/api/streams?src=car_h264"
```

A working producer lists `H264` and `OPUS/48000/2` medias with byte counts
climbing on both. A producer with **no medias and no ffmpeg process** is the
pipeline having hung and been reaped without a word (`-v error` says nothing
about a blocked input) -- run the `exec:` line by hand with `docker exec`,
`-v info` and `-t 5 -f null -`, one input at a time. That is how the audio
source note in `go2rtc.yaml` was found.

### Storage

The eMMC is 58 GB and the M.2 slot takes an NVMe. Put both stores on it:

```bash
sudo parted -s /dev/nvme0n1 mklabel gpt
sudo parted -s -a optimal /dev/nvme0n1 mkpart openlaps-data ext4 1MiB 100%
sudo mkfs.ext4 -L openlaps-data -m 0 /dev/nvme0n1p1
# fstab, by UUID, nofail so a dead disk still boots the car
echo "UUID=$(sudo blkid -s UUID -o value /dev/nvme0n1p1) /srv/openlaps ext4 defaults,noatime,nofail 0 2" \
  | sudo tee -a /etc/fstab
sudo mkdir -p /srv/openlaps && sudo mount -a
```

Then `NATS_STORE_DIR=/srv/openlaps/nats` in `.env`, and Docker's data-root
via `/etc/docker/daemon.json`:

```json
{ "data-root": "/srv/openlaps/docker" }
```

**Pair that with a drop-in**, or `nofail` becomes a trap — a disk that fails
to mount lets Docker start anyway and silently build a fresh, empty data-root
on the eMMC:

```ini
# /etc/systemd/system/docker.service.d/openlaps-storage.conf
[Unit]
RequiresMountsFor=/srv/openlaps
```

## Enabling the hardware UART

`/dev/ttyS4` is the Bluetooth HCI link (`hciattach` holds it) and every other
`serial@` node is `status = "disabled"`, so a UART needs a new device tree.
The kernel already has `CONFIG_SERIAL_8250_DW=y` — this is a **DTB change, not
a kernel rebuild**.

`openlaps.dtsi` beside this file is the change. It enables `uart2` on
`uart2m1` (header pins 8/10, the position Luckfox's wiki documents) and lists
the other six header-reachable muxes if pins 8/10 are inconvenient.

### 1. Back up the current boot partition

The DTB lives in a FIT image in the 64 MB `boot` partition. Backing it up
first is what makes every later step recoverable.

```bash
sudo dd if=/dev/mmcblk2p3 of=~/boot-stock.img bs=1M count=64 status=progress
```

| | | |
| --- | --- | --- |
| `mmcblk2p1` | 4 MB | `uboot` — **never written by this procedure** |
| `mmcblk2p2` | 4 MB | `misc` |
| `mmcblk2p3` | 64 MB | `boot` — the FIT: kernel, DTB, resource |
| `mmcblk2p4` | 128 MB | `recovery` |
| `mmcblk2p5` | 32 MB | `backup` |
| `mmcblk2p6` | 58 GB | `rootfs` |

### 2. Build

`openlaps.dtsi` is a device-tree fragment, not a patch. Copy it into the SDK's
device-tree directory and `#include` it from the end of the board's `.dts`,
which is a textual paste done by the preprocessor — no Makefile change, and
re-applying it after an SDK update is the same two commands.

First confirm which `.dts` your build actually uses, since the SDK carries
several boards:

```bash
cd <sdk>/kernel-6.1/arch/arm64/boot/dts/rockchip
ls luckfox-omni3576*.dts
```

Then, from the SDK root:

```bash
cp <openlaps>/deploy/targets/luckfox-omni3576/openlaps.dtsi \
  kernel-6.1/arch/arm64/boot/dts/rockchip/
printf '\n#include "openlaps.dtsi"\n' \
  >> kernel-6.1/arch/arm64/boot/dts/rockchip/luckfox-omni3576.dts
./build.sh kernel
```

**At the end, not the top.** The fragment refers to `&uart2` and to the
`RK_PA2` / `GPIO_ACTIVE_HIGH` macros; both only exist after the board `.dts`
has included `rk3576.dtsi` and the `dt-bindings` headers.

The build produces `output/firmware/boot.img` (a symlink to
`kernel-6.1/boot.img`); a full run also writes
`output/update/Image/update.img`, which is not needed here. Copy `boot.img`
to the board and check it is under 64 MB.

If you build in a container that bind-mounts the SDK directory, nothing above
changes — the tree the container compiles is the one you just edited. Check
only that the wrapper does not re-run `repo sync`, which can revert local
modifications to tracked trees.

### 3. Write it

`boot.img` goes at offset 0 of the boot partition, which is exactly what the
flasher does — so this needs no USB cable and no button:

```bash
sudo dd if=boot.img of=/dev/mmcblk2p3 bs=1M conv=fsync status=progress
sync
sudo reboot
```

### 4. Point the profile at it

```yaml
# hardware.yaml
serial:
  serial0:
    port: /dev/ttyS2
```

and `OPENLAPS_SERIAL_DEVICE=/dev/ttyS2` in `target.env`.
`tests/test_hardware.py` fails the pair if they disagree.

### If it does not boot

U-Boot is untouched, so loader mode still works. This board declares
`mode-loader` under `/syscon@26024000/reboot-mode`, so from a booted system:

```bash
sudo reboot loader
```

and from a board that will not boot, the recovery button. Then write
`boot-stock.img` back with `rkdeveloptool wl`, or `dd` it back from any
working boot. Maskrom is only needed if `uboot` itself is damaged, which this
procedure never writes to.

## PPS → chrony

Two routes, and which one you are on depends on whether the reflash above
carried `CONFIG_PPS_CLIENT_GPIO` and the `pps-gpio` node.

```bash
ls /dev/pps0
```

Present → **kernel PPS**, below. Absent → **the userspace shim**, further
down. They are mutually exclusive by construction: `pps-gpio` claims
GPIO3_A2, so the shim's line request fails with `Device or resource busy`
while the driver is bound.

### Kernel PPS (`/dev/pps0`)

Proving the wire needs no openlaps code at all — the counter is in sysfs:

```bash
cat /sys/class/pps/pps0/assert
```

`1789123380.000820828#2032` is a timestamp, then `#`, then the number of
edges seen. A rising count is the receiver pulsing; `0` or a file that never
changes is no fix yet (`CONFIG PPS ENABLE` suppresses output until the
receiver has one), the wrong pin, or no shared ground. `sudo apt-get install
pps-tools` adds `ppstest /dev/pps0` for a live view.

Then install chrony with the PPS config — `deploy/chrony/vehicle-pps.conf` is
`vehicle.conf` with `refclock PPS /dev/pps0` in place of the SOCK refclock the
X4's RP2040 timing head needs:

```bash
sudo apt-get install chrony pps-tools
sudo install -m 0644 deploy/chrony/vehicle-pps.conf /etc/chrony/chrony.conf
sudo install -m 0644 deploy/udev/99-openlaps-pps.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=pps
sudo systemctl restart chrony
chronyc sources -v
```

**The udev rule is not optional.** `pps-gpio` creates `/dev/pps0` as
`crw------- root root` and Debian's chronyd drops to `_chrony`, so without it
the refclock sits at `Reach 0` and `#?` forever with nothing in the journal
saying why — while `sudo ppstest /dev/pps0` happily shows the pulses arriving.

No `lock NMEA`: nothing here feeds chrony NMEA, so the coarse second comes
from the NTP pool the config already carries. Same assumption the shim makes,
sound while NTP holds the clock inside ±0.5 s.

Measured on this board, idle, about two minutes after start:

```
#* GPS    0  4  377   9   +2337ns[+4543ns] +/- 7727ns
GPS       8  4  110   +0.002   0.516   +3ns   9451ns
```

Selected, offset a couple of microseconds, **9.4 µs standard deviation** —
against the ±100 µs the X4's RP2040 path gets. Re-read `chronyc sourcestats`
under real load before believing that number at an event.

**After a cold boot with no RTC battery, expect `#?` for half an hour.** The
hym8563 RTC comes up invalid, the clock starts in 2021 (systemd then bumps
it to its build epoch), and chrony's first act is a step of months. A PPS
refclock cannot number its own pulses, so each pulse inherits the local
clock's error estimate as its dispersion, and that estimate is left in the
tens of thousands of seconds by the step. Seen on 2026-09-12: samples
31 µs tight, yet `+/- 58318s`, decaying about a quarter per minute, with the
dashboard's `sys.host.clock_stratum` showing 4 from the pool. It converges
on its own in roughly 30 minutes. `sudo systemctl restart chrony` once the
clock is right shortcuts it: selected within a minute, stratum 1. The real
fix is the RTC battery, which turns the boot-time step into seconds.

### Userspace shim, if you did not reflash

`tools/pps_gpio_shim.py` reads the edges from `/dev/gpiochip3` and feeds
chrony's SOCK refclock. The kernel takes the timestamp in its hard IRQ handler
and hands userspace an already-stamped event, so the shim's own scheduling
latency does not enter it. It needs no NMEA: the coarse second comes from
rounding the edge's own timestamp, guarded by `--max-offset`.

It is **stdlib-only**, so it runs on Debian's `python3` — no venv, no `uv`,
and only two files need to be on the board. From a checkout:

```bash
cd ~/openlaps        # wherever the checkout is; the paths below are relative to it
sudo python3 tools/pps_gpio_shim.py \
  --chip /dev/gpiochip3 --line 2 --dry-run --max-offset 0.5
```

One line per second, each with the offset it would have sent. To check the
pin itself before the receiver is wired, leave that running and brush a
jumper from pin 1 (3V3) onto pin 11 — contact bounce gives a burst of edges.
The line is an input; touching it to 3V3 is safe.

Installing it:

```bash
sudo apt-get install chrony
getent group openlaps >/dev/null || sudo groupadd --system openlaps
sudo install -D -o root -g root -m 0755 tools/pps_gpio_shim.py \
  /usr/local/libexec/openlaps/pps_gpio_shim.py
sudo install -D -o root -g root -m 0644 tools/chrony_sock.py \
  /usr/local/libexec/openlaps/chrony_sock.py
sudo install -m 0644 deploy/udev/99-openlaps-gpio.rules /etc/udev/rules.d/
sudo install -m 0644 deploy/systemd/openlaps-pps-gpio.service /etc/systemd/system/
sudo install -m 0644 deploy/chrony/vehicle.conf /etc/chrony/chrony.conf
sudo install -d /etc/systemd/system/chrony.service.d
sudo install -m 0644 deploy/systemd/chrony-openlaps-sock.conf \
  /etc/systemd/system/chrony.service.d/openlaps-sock.conf
sudo install -d /etc/openlaps
printf '%s\n' 'PPS_GPIO_ARGS=--chip /dev/gpiochip3 --line 2' \
  | sudo tee /etc/openlaps/pps-gpio.env
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=gpio
sudo systemctl daemon-reload
sudo systemctl restart chrony
sudo systemctl enable --now openlaps-pps-gpio
chronyc sources -v
```

The unit `Conflicts=timing-head-shim.service`: both feed the same socket, and
two opinions about one second is not redundancy.

### Either way, measure it

The kernel is `PREEMPT_VOLUNTARY`, not `PREEMPT_RT`, so excursions are
expected and whether they matter is a number rather than a claim.
`docs/BENCH_RUNBOOK.md` is how to get it.

### Other PPS pins

If pin 11 is inconvenient — all free, all interrupt-capable:

| Header pin | GPIO | shim arguments |
| --- | --- | --- |
| 11 | GPIO3_A2 | `--chip /dev/gpiochip3 --line 2` |
| 13 | GPIO3_A3 | `--chip /dev/gpiochip3 --line 3` |
| 32 | GPIO3_A0 | `--chip /dev/gpiochip3 --line 0` |
| 33 | GPIO3_A1 | `--chip /dev/gpiochip3 --line 1` |
| 7 | GPIO2_A6 | `--chip /dev/gpiochip2 --line 6` |
| 29 | GPIO2_A7 | `--chip /dev/gpiochip2 --line 7` |

Chip *N* starts at global GPIO 32*N*, and the offset within it is
`8×(A=0,B=1,C=2,D=3) + digit`. Confirm a line is unclaimed before wiring:

```bash
sudo cat /sys/kernel/debug/gpio
```

## CAN: FD mode is mandatory

A classic bring-up is refused — with the bitrate already committed, so
`ip -d link show can0` then shows a bitrate on an interface that is not
running:

```
# ip link set can0 up type can bitrate 1000000
rk3576_canfd 2ac00000.can can0: incorrect/missing data bit-timing
# ip link set can0 up type can bitrate 1000000 dbitrate 2000000
RTNETLINK answers: Operation not supported
```

Both together work, which is why `hardware.yaml` carries
`link: {fd: true, dbitrate: 2000000}` for `tools/can_up.py` to pass to `ip`.
The car's bus is unaffected: an FD-mode controller receives classic frames
unchanged, the data bitrate only governs BRS frames a classic bus never sends,
and the agent transmits nothing. `mtu` becomes 72 rather than 16.

## Video: H.264 only

`hevc_rkmpp` fails at `Failed to init MPP context: -1` on this board;
`h264_rkmpp` encodes 640x360 at ~7× real time. So `go2rtc.yaml` defines
`car_h264` and no `car`. The `video` dashboard already defaults to
`car_h264`, so nothing at the pit changes; selecting `car` gets "stream not
found".

Cost: ~800 kbit/s for the quality the X4 gets from ~500 at H.265 — inside
`docs/LINK_BUDGET.md` §5's ~1.4 Mbit/s, with less headroom.

## Audio: PulseAudio, not ALSA

The `-rockchip` go2rtc image's ffmpeg has no ALSA input device, so capture
goes through PipeWire's Pulse socket — `compose.yaml` mounts
`/run/user/1000/pulse/native` and sets `PULSE_SERVER`.

That path contains the session user's uid; `OPENLAPS_PULSE_SOCKET` overrides
it. The session must exist for the socket to, so if the stack starts before
anyone logs in, `loginctl enable-linger <user>`.

The pipeline names the ES8388's source rather than `default`. A USB camera
with a microphone (the Insta360 X3 does) becomes WirePlumber's default source
the moment it is plugged in, and that mic delivered nothing through Pulse
here: audio-only ffmpeg received no packets until killed, and the full
pipeline hung at input probing. `ffmpeg -sources pulse` inside the container
lists the names; `wpctl status` on the host shows which one is default.
