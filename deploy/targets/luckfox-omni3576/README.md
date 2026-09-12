# Luckfox Omni3576

Rockchip RK3576, 8 cores, 8 GB, Debian 12, kernel 6.1.75 (Rockchip BSP).

Three things differ from the Radxa X4: the CAN controller only comes up in FD
mode, video is H.264 only, and the stock image exposes no usable UART or PPS,
so the board runs a **kernel built from this directory** (`openlaps.dtsi` plus
a config fragment). Everything below assumes that kernel is flashed;
"Kernel" is how.

The stack is three pieces, none of them compose: the shipped Docker 20.10
has no `compose` subcommand (`docker compose ...` prints the Docker help
text and exits 0). The NATS server and go2rtc are plain `docker run`
containers, and the agent is a systemd unit running from `/opt/openlaps`.

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
| 6 | `EN` | — | high-level enable; tie to VCC if the module does not start with it floating |

**COM1, not COM2.** The X4 uses COM2, and that is the one thing not to copy
across: the driver's timing sentences are addressed to `COM2` explicitly
(`GPZDA COM2 1`, `GPGGA COM2 1`, `CONFIG COM2 ...`), so sitting on COM2 would
have it reconfigure the link it is talking over. The profile's
`timing_output: COM2` is vestigial here — nothing reads COM2 on this board —
and is left alone because the profile is shared with the X4.

**A dead receive line looks like a receiver fault.** The symptom is
`receiver configuration failed: UM980 command 'CONFIG CMDFORMAT 1' failed:
timeout` every 30 s while PPS works. Check `/proc/tty/driver/serial` line 2:
`tx` climbing with `rx:0` means not one byte has arrived on pin 10 — not even
garbage, which a baud mismatch would give — and the fault is the `TXD` lead.

### Power

| | Supply | Draw | From |
| --- | --- | --- | --- |
| WitMotion **WTRTK-980** (the module in use) | 5 V (3.6–6 V in) | 220 mA typ; 0.15–0.19 A measured | header **pin 2** or **4** |
| A **bare UM980** | 3.0–3.6 V, **3.6 V absolute max** | 180 mA max + up to 50 mA antenna | **its own LDO** — never the header |

The carrier board regulates internally, so the header's 5 V input rail is
fine for it; ~1.1 W on top of an 8-core SoC and an NVMe, with inrush at
power-on, so check the supply's headroom. A bare UM980 is destroyed by 5 V
and needs a dedicated 3.3 V LDO rated about twice its draw
([UM980 manual](https://docs.sparkfun.com/SparkFun_UM980_Triband_GNSS_RTK_Breakout/assets/component_documentation/UM980_Datasheet.pdf)).

**Signal levels are 3.3 V TTL**, so `TXD` and `PPS` go straight to pins 10
and 11 with no level shifting — the same receiver drives RP2040 GPIOs
directly on the X4 (`firmware/timing-head/README.md`, which also has the
WTRTK-980's connector pinout). Put bulk decoupling at the connector; the run
to the receiver is not short.

### Everything else

| | Where | |
| --- | --- | --- |
| CAN | `can0`, on-SoC | `can0m2` is GPIO4_A4/A6 — its own connector, not the 40-pin header |
| Camera | USB UVC (Insta360 X3 in webcam mode) | `/dev/video45` — the MIPI pipeline holds `video0`–`video44`. Always address it by `/dev/v4l/by-id/` |
| Audio | `card 0`, ES8388 | captured over PipeWire's Pulse socket, source named explicitly — see Audio |
| Encode | `/dev/mpp_service` + `/dev/rga` | H.264 only |
| Storage | eMMC 58 GB (`mmcblk2`) + 1 TB NVMe at `/srv/openlaps` | Docker's data-root and the JetStream store both live on the NVMe |
| RTC | hym8563 | needs its battery — see Clock |

## Kernel

`/dev/ttyS4` is the Bluetooth HCI link and every other UART is disabled in
the stock device tree; PPS and NVMe temperature need drivers the stock config
leaves out. One build covers all three:

- `openlaps.dtsi` beside this file: `uart2` on `uart2m1` (pins 8/10, becomes
  `/dev/ttyS2`) and a `pps-gpio` node on GPIO3_A2 (pin 11, becomes
  `/dev/pps0`).
- A config fragment, passed to the SDK as `RK_KERNEL_CFG_FRAGMENTS` (the
  `docker.config` in the build wrapper):

  ```
  CONFIG_PPS_CLIENT_GPIO=y
  CONFIG_NVME_HWMON=y
  ```

  Not `.config` directly: the SDK regenerates that from the board defconfig on
  every build, so a hand edit is discarded.

### 1. Back up the boot partition

The DTB lives in a FIT image in the 64 MB `boot` partition. U-Boot
(`mmcblk2p1`) is never written by this procedure.

```bash
sudo dd if=/dev/mmcblk2p3 of=~/boot-stock.img bs=1M count=64 status=progress
```

### 2. Build

From the SDK root:

```bash
cp <openlaps>/deploy/targets/luckfox-omni3576/openlaps.dtsi \
  kernel-6.1/arch/arm64/boot/dts/rockchip/
printf '\n#include "openlaps.dtsi"\n' \
  >> kernel-6.1/arch/arm64/boot/dts/rockchip/luckfox-omni3576.dts
./build.sh kernel
```

**At the end of the `.dts`, not the top** — the fragment refers to `&uart2`
and to `RK_PA2` / `GPIO_ACTIVE_HIGH`, which only exist after the board `.dts`
has included `rk3576.dtsi` and the `dt-bindings` headers. Re-applying after
an SDK update is the same two commands.

The result is `output/firmware/boot.img` (a symlink to `kernel-6.1/boot.img`,
~47 MB). Copy it to the board.

### 3. Write it, and check it took

```bash
sudo dd if=boot.img of=/dev/mmcblk2p3 bs=1M conv=fsync status=progress
sync && sudo reboot
```

Then, **before believing it**:

```bash
uname -v                                   # build number and date must be the new ones
zcat /proc/config.gz | grep -E 'NVME_HWMON|PPS_CLIENT_GPIO'
ls /dev/ttyS2 /dev/pps0
```

A `boot.img` copied to the board but not written boots the old kernel with
no complaint, and the agent's only hint is a one-line warning that
`nvme.composite` is not exposed.

### If it does not boot

U-Boot is untouched, so loader mode still works: `sudo reboot loader` from a
booted system, the recovery button from one that will not boot. Then write
`boot-stock.img` back with `rkdeveloptool wl`, or `dd` it from any working
boot. Maskrom is only needed if `uboot` itself is damaged, which this
procedure never touches.

## Storage

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

Docker's data-root goes there via `/etc/docker/daemon.json`:

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

## Install

Two trees. `~/openlaps` is the checkout, with `deploy/.env` and the TLS
material in `~/openlaps-secrets/tls` beside it (`deploy/README.md` →
Secrets). `/opt/openlaps` is the installed copy the services run from, owned
by the `openlaps` system user with its own venv. Both containers mount their
config from `/opt/openlaps`, so an update is one copy plus restarts (see
Operating).

### 1. Checkout, env, venv

```bash
git clone <this repo> ~/openlaps && cd ~/openlaps     # or tar a checkout over
cat deploy/targets/luckfox-omni3576/target.env >> deploy/.env
uv python install 3.13 && uv sync                     # Debian ships 3.11; pyproject needs 3.13
```

`.env` is read only by the NATS container here (`--env-file`); the go2rtc
device path is given on its `docker run` line and the agent reads
`/etc/openlaps/agent.env`. Set `OPENLAPS_VIDEO_DEVICE` to the camera's
`/dev/v4l/by-id/` path anyway, so the file states the truth.

### 2. The installed tree, the agent and CAN

```bash
getent group openlaps >/dev/null || sudo groupadd --system openlaps
id -u openlaps >/dev/null 2>&1 || \
  sudo useradd --system --gid openlaps --home-dir /opt/openlaps --shell /usr/sbin/nologin openlaps
sudo install -d -o openlaps -g openlaps /opt/openlaps
tar cf - --exclude=.venv --exclude=.git . | sudo -u openlaps tar xf - -C /opt/openlaps
(cd /opt/openlaps && sudo -u openlaps uv python install 3.13 && sudo -u openlaps uv sync)

sudo install -d /etc/openlaps
sudo tee /etc/openlaps/agent.env >/dev/null <<'EOF'
OPENLAPS_PROFILE=/opt/openlaps/profiles/example-club-racer
OPENLAPS_HARDWARE=/opt/openlaps/deploy/targets/luckfox-omni3576/hardware.yaml
OPENLAPS_VEHICLE_ID=christine
OPENLAPS_NATS_URL=nats://127.0.0.1:4222
OPENLAPS_STATE_DIR=/var/lib/openlaps
OPENLAPS_TICK_MS=20
EOF
sudo install -m 0644 deploy/systemd/openlaps-can.service deploy/systemd/openlaps-agent.service \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openlaps-can openlaps-agent
```

`openlaps-can` runs `tools/can_up.py` as root before the agent, with the
`link: {fd: true, dbitrate: 2000000}` from `hardware.yaml` — see CAN below
for why that is not optional.

### 3. Clock: PPS into chrony

`deploy/chrony/vehicle-pps.conf` is `vehicle.conf` with
`refclock PPS /dev/pps0` in place of the SOCK refclock the X4's timing head
needs.

```bash
sudo apt-get install chrony pps-tools
sudo install -m 0644 deploy/chrony/vehicle-pps.conf /etc/chrony/chrony.conf
sudo install -m 0644 deploy/udev/99-openlaps-pps.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=pps
sudo systemctl restart chrony
```

**The udev rule is not optional.** `pps-gpio` creates `/dev/pps0` as
`crw------- root root` and chronyd drops to `_chrony`; without the rule the
refclock sits at `Reach 0` forever with nothing in the journal, while
`sudo ppstest /dev/pps0` happily shows pulses. The wire itself is proven by
`cat /sys/class/pps/pps0/assert`: a count after `#` that rises once a second.

### 4. NATS

```bash
docker run -d --name openlaps-vehicle-nats --network host --restart unless-stopped \
  --env-file ~/openlaps/deploy/.env \
  -v /opt/openlaps/deploy/nats/vehicle.conf:/etc/nats/nats.conf:ro \
  -v ~/openlaps-secrets/tls:/etc/nats/tls:ro \
  -v /srv/openlaps/nats:/data \
  nats:2.12-alpine -c /etc/nats/nats.conf
```

### 5. go2rtc

The camera's microphone is captured through PipeWire's Pulse socket under
`/run/user/1000`, which exists only while `luckfox` has a session. Linger
gives it one from boot, with nobody logged in:

```bash
sudo loginctl enable-linger luckfox
```

Docker's containers must not start before that session exists, or the
socket's bind mount has no source:

```bash
sudo tee /etc/systemd/system/docker.service.d/openlaps-pulse.conf >/dev/null <<'EOF'
[Unit]
After=user@1000.service
Wants=user@1000.service
EOF
sudo systemctl daemon-reload
```

Then the container — `vehicle-compose.yaml`'s `go2rtc` service plus this
directory's `compose.yaml` overlay, flattened:

```bash
CAM=/dev/v4l/by-id/usb-Amba_Insta360_X3-video-index0   # v4l2-ctl --list-devices
docker run -d --name openlaps-vehicle-go2rtc --network host --restart unless-stopped \
  --device $CAM:/dev/video0 --device /dev/dri:/dev/dri --device /dev/snd:/dev/snd \
  --device /dev/mpp_service:/dev/mpp_service --device /dev/rga:/dev/rga \
  -v /opt/openlaps/deploy/targets/luckfox-omni3576/go2rtc.yaml:/config/go2rtc.yaml:ro \
  -v /run/user/1000/pulse/native:/run/pulse/native -e PULSE_SERVER=unix:/run/pulse/native \
  alexxit/go2rtc:1.9.14-rockchip
```

Docker **refuses** to start a container whose `--device` is absent, and a
refusal is not retried by the restart policy — so a camera missing at boot
leaves go2rtc down until someone runs `docker start`. A udev rule does that
the moment a USB camera enumerates, via a oneshot unit that is a no-op when
the container is already up:

```bash
sudo install -m 0644 deploy/udev/99-openlaps-camera.rules /etc/udev/rules.d/
sudo install -m 0644 deploy/systemd/openlaps-go2rtc.service /etc/systemd/system/
sudo udevadm control --reload && sudo systemctl daemon-reload
```

An Insta360 X3 does go missing: across a reboot it can drop out of webcam
mode, and then `dmesg` shows `usb 1-1.4: device descriptor read/64, error
-110` repeating with no `/dev/v4l/by-id/` at all. That is the camera, not the
board — put it back in webcam mode and the rule starts the container.

### 6. Verify

```bash
systemctl is-active openlaps-can openlaps-agent chrony
docker ps --format '{{.Names}} {{.Status}}'
chronyc sources                          # '#* GPS' selected, stratum 1
ip -d link show can0 | grep -o 'fd on'
sudo journalctl -u openlaps-agent -b | grep -E 'starting vehicle|WARNING|ERROR'
curl -s -m 10 "http://127.0.0.1:1984/api/stream.mp4?src=car_h264" -o /dev/null
curl -s "http://127.0.0.1:1984/api/streams?src=car_h264"
```

The agent's only expected log lines are `starting vehicle christine` and
the `no catalog mapping for host:...` notes for raw per-device refs. The
go2rtc producer must list `H264` and `OPUS/48000/2` medias with byte counts
on both; a producer with no medias and no ffmpeg process is the pipeline
having hung at an input and been reaped without a word — run its `exec:` line
by hand with `docker exec ... -v info -t 5 -f null -`, one input at a time.

Then at the pit: `deploy/README.md` → "Verify each hop", and set the Video
dashboard's `camera` variable to `http://192.168.12.222:1984`.

## Operating

```bash
# status
systemctl status openlaps-can openlaps-agent chrony
docker ps
sudo journalctl -u openlaps-agent -f
docker logs -f openlaps-vehicle-go2rtc

# stop everything (data kept)
sudo systemctl stop openlaps-agent
docker stop openlaps-vehicle-go2rtc openlaps-vehicle-nats

# start
docker start openlaps-vehicle-nats openlaps-vehicle-go2rtc
sudo systemctl start openlaps-agent
```

All of it comes back on its own after a power cycle: the units are enabled,
the containers are `--restart unless-stopped`, and linger brings up the
Pulse socket. `docker stop` is remembered across reboots; `docker start`
undoes it.

**Update** a changed checkout in `~/openlaps` (or files tarred over from the
pit) into the installed tree, then restart what reads them:

```bash
cd ~/openlaps && tar cf - --exclude=.venv --exclude=.git . | sudo -u openlaps tar xf - -C /opt/openlaps
sudo systemctl restart openlaps-agent          # profile, hardware.yaml, src
docker restart openlaps-vehicle-go2rtc         # go2rtc.yaml
docker restart openlaps-vehicle-nats           # nats/vehicle.conf
```

Re-run `uv sync` in `/opt/openlaps` if `pyproject.toml` changed.

**Tear down** (destructive — the JetStream store and the agent's registry
and session state go with it; pit history is unaffected, see
`deploy/README.md` → Tear-down):

```bash
sudo systemctl disable --now openlaps-agent openlaps-can
docker stop openlaps-vehicle-go2rtc openlaps-vehicle-nats   # stop, then rm: `rm -f`
docker rm openlaps-vehicle-go2rtc openlaps-vehicle-nats     # SIGKILLs nats mid-write
sudo rm -rf /srv/openlaps/nats/* /var/lib/openlaps
```

## Board notes

### CAN: FD mode is mandatory

A classic bring-up is refused, with the bitrate already committed so
`ip -d link show can0` then shows a bitrate on an interface that is not
running:

```
# ip link set can0 up type can bitrate 1000000
rk3576_canfd 2ac00000.can can0: incorrect/missing data bit-timing
# ip link set can0 up type can bitrate 1000000 dbitrate 2000000
RTNETLINK answers: Operation not supported
```

Both together work, which is why `hardware.yaml` carries
`link: {fd: true, dbitrate: 2000000}`. The car's bus is unaffected: an
FD-mode controller receives classic frames unchanged, the data bitrate only
governs BRS frames a classic bus never sends, and the agent transmits nothing.

### Video: H.264 only

`hevc_rkmpp` fails at `Failed to init MPP context: -1`; `h264_rkmpp` encodes
640x360 at ~7× real time. So `go2rtc.yaml` defines `car_h264` and no `car`.
The `video` dashboard defaults to `car_h264`; selecting `car` gets "stream
not found". Cost: ~800 kbit/s for the quality the X4 gets from ~500 at
H.265 — inside `docs/LINK_BUDGET.md` §5's ~1.4 Mbit/s, with less headroom.

### Audio: Pulse, with the source named

The `-rockchip` go2rtc image's ffmpeg has no ALSA input device, so capture
goes through PipeWire's Pulse socket. The pipeline names the ES8388's source
rather than `default`: a USB camera with a microphone (the Insta360 X3 has
one) becomes WirePlumber's default source the moment it is plugged in, and
that mic delivers nothing through Pulse — audio-only ffmpeg received no
packets until killed, and the full pipeline hung at input probing.
`ffmpeg -sources pulse` inside the container lists the names; `wpctl status`
on the host shows which is default.

### Temperatures

The RK3576's TSADC exposes six on-die zones, each an hwmon chip with one
unlabelled reading, so psutil names them `<zone>.0`: `soc_thermal` (the
governor's zone — throttling is decided on this one), `bigcore_thermal`,
`little_core_thermal`, `ddr_thermal`, `npu_thermal`, `gpu_thermal`. All read
within a degree of each other; `crit` is 115 °C on each. `hardware.yaml`
maps `cpu: soc_thermal.0` and `nvme: nvme.composite` (the `nvme` hwmon chip
the rebuilt kernel provides). There is **no board sensor**: the only other
hwmon chip is the USB-PD controller, which reports volts and amps, so
`board` is deliberately unmapped rather than passing off an on-die reading.

Throttling comes from the cooling devices `cpufreq-cpu0` (A53 cluster),
`cpufreq-cpu4` (A72 cluster), `devfreq-dmc` (DDR), `devfreq-*.gpu` and
`devfreq-*.npu`, normalised to percentages as `sys.host.throttle_*`. The
cpufreq policies are `policy0` (cores 0–3, up to 2016 MHz) and `policy4`
(cores 4–7, up to 2208 MHz), so `sys.host.cpu_freq_max_mhz` / `_min_mhz` are
the big and little cluster; psutil's single figure averages them into a clock
neither runs at.

### Clock

Measured on this board with the PPS selected: offset a few microseconds,
**~9 µs standard deviation** (`chronyc sourcestats`), against the ±100 µs the
X4's RP2040 path gets. Re-read it under real load before believing it at an
event; the kernel is `PREEMPT_VOLUNTARY`, not `PREEMPT_RT`.

**Without an RTC battery, a cold boot leaves the PPS unselected for about
half an hour.** The hym8563 comes up invalid, the clock starts in 2021, and
chrony's first act is a step of months. A PPS refclock cannot number its own
pulses, so each pulse inherits the local clock's error estimate as its
dispersion, and that estimate is left in the tens of thousands of seconds by
the step: `chronyc sources` shows `#? GPS ... +/- 58318s` decaying about a
quarter per minute, and the dashboard's stratum reads 4 from the NTP pool.
`sudo systemctl restart chrony` once the clock is right shortcuts it to
stratum 1 within a minute. A warm reboot keeps the RTC ticking and does not
trigger it; the battery makes the cold boot a step of seconds and ends it.

`tools/pps_gpio_shim.py` is the userspace alternative for a board that has
not been reflashed; it is not used here, and cannot coexist with `pps-gpio`,
which holds the same GPIO.
