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
| 6 | `EN` | pin 2/4 (with `VCC`) | high-level enable; **tie it to VCC** — see below |

**COM1, not COM2.** The X4 uses COM2, and that is the one thing not to copy
across: the driver's timing sentences are addressed to `COM2` explicitly
(`GPZDA COM2 1`, `GPGGA COM2 1`, `CONFIG COM2 ...`), so sitting on COM2 would
have it reconfigure the link it is talking over. The profile's
`timing_output: COM2` is vestigial here — nothing reads COM2 on this board —
and is left alone because the profile is shared with the X4.

**A floating `EN` looks like a dead module.** One WTRTK-980 ran for months
with `EN` unconnected; its replacement (2026-11-10) did nothing at all that
way — no LED, cold, no USB enumeration, `rx:0` on the UART — and came up at
once, drawing ~100 mA at 5 V, with `EN` tied to `VCC`. Tie it.

**The carrier joins USB 5 V to `VCC`.** With a bench supply on `VCC`, plugging
in USB took the supply's current to zero. Do not have the USB port and the
header's 5 V connected at the same time. The USB port is a CH340 serial bridge
(`1a86:7523`) to the receiver at 115200, which is how firmware is flashed
(Unicore UPrecise, Windows).

**A new receiver needs two things this repo does not do for it:**

- **Firmware R4.10 Build17548** (check with `VERSIONA`). The replacement shipped
  on Build13504.
- **`CONFIG SIGNALGROUP 8`**, once; it persists and the receiver reboots. Out of
  the box it is in group 1 and refuses the profile's 50 Hz output — the agent
  logs `UM980 command 'GPRMC 0.02' failed: PARSING FAILED FIELD OUT OF RANGE,
  Period 0.02 is not supported for this log message!` on every retry. The
  driver does not send this command.

Once the agent has configured the receiver (`CONFIG CMDFORMAT 1`), it ignores
plain-text commands in silence. To talk to it by hand, stop the agent and send
`$<command>*<XX>`, `XX` being the XOR of the command's bytes (`$CONFIG*0A`).

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
directly on the X4 (`firmware/gnss-receiver-interface/README.md`, which also has the
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
leaves out; and the stock CAN driver wedges under a busy bus. One build
covers all four:

- `openlaps.dtsi` beside this file: `uart2` on `uart2m1` (pins 8/10, becomes
  `/dev/ttyS2`) and a `pps-gpio` node on GPIO3_A2 (pin 11, becomes
  `/dev/pps0`).
- A config fragment, passed to the SDK as `RK_KERNEL_CFG_FRAGMENTS` (the
  `docker.config` in the build wrapper):

  ```
  CONFIG_PPS_CLIENT_GPIO=y
  CONFIG_NVME_HWMON=y
  CONFIG_TCP_CONG_ADVANCED=y
  CONFIG_TCP_CONG_BBR=y
  CONFIG_NET_SCH_FQ=y
  CONFIG_USBIP_CORE=y
  CONFIG_USBIP_HOST=y
  ```

  The three TCP lines compile in BBR congestion control and the `fq` pacing
  qdisc for the vehicle-to-pit TCP link (the NATS leafnode, over HaLow or
  Starlink). They are inert until selected: the kernel default stays
  `cubic`, and `systemd/90-openlaps-bbr.conf` (installed to
  `/etc/sysctl.d/`) is what switches the board over.

  The two USBIP lines are only for remote ECU tuning
  (`docs/operations/remote-ecu.md`): they let the board export the ECU's USB
  port to the pit.

  All five are `=y` rather than `=m` because this procedure writes only
  `boot.img` and never installs a matching module tree, so a module would
  have nothing to load from.

  Not `.config` directly: the SDK regenerates that from the board defconfig on
  every build, so a hand edit is discarded.
- `patches/0001-can-rk3576_canfd-never-leave-NAPI-scheduled.patch`: the
  receive-path fix for the vendor CAN driver (see *CAN: the controller can
  wedge* below for what it fixes). The patch header carries the full
  analysis.

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
patch -p1 -d kernel-6.1 \
  < <openlaps>/deploy/targets/luckfox-omni3576/patches/0001-can-rk3576_canfd-never-leave-NAPI-scheduled.patch
./build.sh kernel
```

`patch` refuses an already-applied patch (answer `n` to "reversed?"), so
re-running after an SDK update is safe; a rejected hunk means the vendor
driver moved and the patch needs rebasing before the build is trusted.

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
zcat /proc/config.gz | grep -E 'NVME_HWMON|PPS_CLIENT_GPIO|TCP_CONG_BBR|NET_SCH_FQ|USBIP_HOST'
ls /dev/ttyS2 /dev/pps0
cat /proc/sys/net/ipv4/tcp_available_congestion_control   # reno cubic bbr
ls -d /sys/bus/usb/drivers/usbip-host      # remote ECU tuning only
```

Then switch the board to BBR, which the kernel only makes available:

```bash
sudo install -m 0644 deploy/targets/luckfox-omni3576/systemd/90-openlaps-bbr.conf /etc/sysctl.d/
sudo sysctl --system
sysctl net.ipv4.tcp_congestion_control    # bbr
```

The vehicle nats-server runs with host networking, so it picks this up on
its next connection; restart the `nats` container to apply it to the live
leafnode link.

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

No checkout on the board. The git checkout stays on the pit; the board holds
`/opt/openlaps`, a plain copy of the tree the services run from, owned by the
`openlaps` system user with its own venv (`tools/deploy_vehicle.sh` refreshes
it, see Operating). Secrets live outside that tree in `~/openlaps-secrets`:
`vehicle.env` (the NATS container's `--env-file`) and `tls/` (the leaf's
certificates, `docs/operations/nats-security.md`). Both containers mount their
config from `/opt/openlaps`, so an update is one copy plus restarts.

### 1. Secrets

On the pit, build the env file from `example.env` as
`docs/operations/vehicle.md` describes, append this target's overrides, and
copy it over with the TLS material:

```bash
cat deploy/targets/luckfox-omni3576/target.env >> deploy/.env
ssh luckfox 'install -d -m 0700 ~/openlaps-secrets/tls'
scp deploy/.env luckfox:openlaps-secrets/vehicle.env
scp <ca.pem server-cert.pem server-key.pem> luckfox:openlaps-secrets/tls/
```

`vehicle.env` is read only by the NATS container here; the go2rtc device
path is given on its `docker run` line and the agent reads
`/etc/openlaps/agent.env`. Set `OPENLAPS_VIDEO_DEVICE` to the camera's
`/dev/v4l/by-id/` path anyway, so the file states the truth.

### 2. The installed tree, the agent and CAN

```bash
getent group openlaps >/dev/null || sudo groupadd --system openlaps
id -u openlaps >/dev/null 2>&1 || \
  sudo useradd --system --gid openlaps --home-dir /opt/openlaps --shell /usr/sbin/nologin openlaps
sudo install -d -o openlaps -g openlaps /opt/openlaps
# from the pit checkout:  git archive HEAD | ssh luckfox 'sudo -u openlaps tar xf - -C /opt/openlaps'
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
  deploy/systemd/openlaps-can-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openlaps-can openlaps-agent openlaps-can-watchdog
```

`openlaps-can` runs `tools/can_up.py` as root before the agent, with the
`link: {fd: true, dbitrate: 2000000}` from `hardware.yaml` — see CAN below
for why that is not optional. `openlaps-can-watchdog` runs
`tools/can_watchdog.py`, also as root, and reboots the board when the
controller wedges (CAN below again). It starts in `log` mode; add
`OPENLAPS_CAN_WATCHDOG_ACTION=reboot` to `agent.env` before a race weekend,
and `sudo systemctl restart openlaps-can-watchdog`.

### 3. Clock: PPS into chrony

`deploy/chrony/vehicle-pps.conf` is `vehicle.conf` with
`refclock PPS /dev/pps0` in place of the SOCK refclock the X4's GNSS receiver interface
needs.

```bash
sudo apt-get install chrony pps-tools
sudo install -m 0644 deploy/chrony/vehicle-pps.conf /etc/chrony/chrony.conf
sudo install -m 0644 deploy/udev/99-openlaps-pps.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=pps
sudo systemctl restart chrony
```

**Offline, that is not enough: pair the PPS with the receiver's time.** A
PPS edge says when a second starts, not which one, and `vehicle-pps.conf`
takes the whole second from the internet pool. With no internet it comes from
the RTC instead: on 2026-11-10 this board ran a whole 12 s fast with PPS
locked to microseconds, and the dashboard showed the vehicle at stratum 10
(chrony gives an unpaired PPS "local stratum − 1" when it is first selected
under the `local` fallback, and keeps it). `vehicle-pps-nmea.conf` locks the
PPS to a second source the agent feeds from the top-of-second RMC:

```bash
sudo install -m 0644 deploy/chrony/vehicle-pps-nmea.conf /etc/chrony/chrony.conf
sudo install -D -m 0644 deploy/systemd/chrony-openlaps-nmea.conf \
    /etc/systemd/system/chrony.service.d/openlaps-nmea.conf
echo 'OPENLAPS_CHRONY_NMEA_SOCKET=/run/openlaps-chrony/nmea.sock' | sudo tee -a /etc/openlaps/agent.env
sudo systemctl daemon-reload
sudo systemctl restart chrony openlaps-agent
```

The agent logs `gnss time: feeding chrony at ...` on its first sample.
`chronyc sources` should show `NMEA` with a `?` (it is `noselect`) and a
climbing reach, and `GPS` selected at stratum 0. The `offset 0.029` on the
`refclock SOCK` line is this board's measured serial latency; the NMEA row's
offset should sit near zero, and if it does not, add it (sign included) to
that figure. With this file the PPS
gives no samples while the agent is stopped or the receiver has no fix; the
pool, then the drift file, carry the clock until it returns.

**The udev rule is not optional.** `pps-gpio` creates `/dev/pps0` as
`crw------- root root` and chronyd drops to `_chrony`; without the rule the
refclock sits at `Reach 0` forever with nothing in the journal, while
`sudo ppstest /dev/pps0` happily shows pulses. The wire itself is proven by
`cat /sys/class/pps/pps0/assert`: a count after `#` that rises once a second.

### 4. NATS

```bash
docker run -d --name openlaps-vehicle-nats --network host --restart unless-stopped \
  --env-file ~/openlaps-secrets/vehicle.env \
  -v /opt/openlaps/deploy/nats/vehicle.conf:/etc/nats/nats.conf:ro \
  -v ~/openlaps-secrets/tls:/etc/nats/tls:ro \
  -v /srv/openlaps/nats:/data \
  nats:2.12-alpine -c /etc/nats/nats.conf
```

### 4b. Raw CAN capture (optional, for fixtures)

`docs/RAW_CAPTURE.md` describes the capture; the shipped unit assumes
`/mnt/data/captures`, which on this board is `/srv/openlaps/captures`, and
the link has to be brought up in FD mode. The drop-in under
`systemd/openlaps-canlog@.service.d/` beside this file carries both and its
header has the four install commands. About 220 MB/hour on this bus; the
NVMe has room for weeks.

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
systemctl status openlaps-can openlaps-agent openlaps-can-watchdog chrony
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

**Update** from the pit, where the git checkout lives (the board has no
`.git` — `/opt/openlaps` is a plain tree, and `cat /opt/openlaps/.deployed-rev`
says which commit it holds):

```bash
tools/deploy_vehicle.sh                        # HEAD → /opt/openlaps, uv sync if the lock changed, restart agent
```

It ships committed files only, needs no internet unless `uv.lock` changed,
and restarts only the agent. Restart the containers by hand when their
config is what changed:

```bash
docker restart openlaps-vehicle-go2rtc         # go2rtc.yaml
docker restart openlaps-vehicle-nats           # nats/vehicle.conf
```

The same thing without the script, from the pit checkout:

```bash
git archive HEAD | ssh luckfox 'sudo -u openlaps tar xf - -C /opt/openlaps'
ssh luckfox 'cd /opt/openlaps && sudo -u openlaps uv sync --frozen'   # only if pyproject.toml / uv.lock changed
ssh luckfox 'sudo systemctl restart openlaps-agent'                  # profile, hardware.yaml, src
```

**Tear down** (destructive — the JetStream store and the agent's registry
and session state go with it; pit history is unaffected, see
`deploy/README.md` → Tear-down):

```bash
sudo systemctl disable --now openlaps-agent openlaps-can openlaps-can-watchdog
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

### CAN: the controller can wedge, and the link must not be bounced

Three times on 2026-11-08, always with the engine running, `can0` stopped
delivering frames for good: the kernel counted ~100 "frames"/s on the link
and matched none of them to any socket (`/proc/net/can/stats` current match
ratio 0 % while the RX counter climbed), candump saw nothing even with the
full error mask, and the link could read ERROR-ACTIVE with zero error
counters. `ip link set can0 down` then hung inside `rk3576_canfd` holding
the rtnl lock, which blocked every new SSH login.

The cause is in the vendor driver's DMA receive path, not on the bus. It
masks the stream interrupts, DMAs the records waiting in the controller's
14-record SRAM, and relies on the NAPI poll to unmask them; the poll only
completed NAPI when it drained *fewer* than 14 records. Drain a full SRAM
once (the host ~9 ms late at the Haltech's ~1500 frames/s) and NAPI stays
scheduled forever: the interrupts stay masked, every later schedule is a
no-op, and each further frame overflows the SRAM into an error frame with no
class bits that no socket can receive. `napi_disable()` in the close path
spins on that same bit, hence the hang. The definitive check, harmless to
run, is the controller's interrupt mask at `0x2ac00010` via `/dev/mem`:
`0x00028001` is wedged, `0x00000001` is healthy.

`patches/0001-can-rk3576_canfd-never-leave-NAPI-scheduled.patch` fixes it
(the poll always completes and chains another DMA while records are
waiting; the mask can no longer be left set with nothing to clear it;
overflow is reported as `CAN_ERR_CRTL_RX_OVERFLOW`). Build it in per
*Kernel* above and confirm with `uname -v` after flashing.

Until the patched kernel is on the board: **never take `can0` down by
hand.** Only a reset of the controller clears it, and a reboot while the bus
is already busy can wedge again during boot, which is why on 2026-11-08 a
plain reboot with the engine running came back stalled while an ECU cycle
followed by a reboot worked. So: ignition/ECU off, reboot, ECU on.
`openlaps-can-watchdog` (`tools/can_watchdog.py`) watches for the signature
on a socket of its own and, in `reboot` mode, reboots the board after 10 s
of it, with `systemctl reboot` first and `/proc/sysrq-trigger` as the
fallback (three reboots an hour, then it stops). The pit sees the same
condition as `sys.agent.stall_s.can0` and the `can-bus-stalled` alarm. One
gap remains on this kernel: no `/dev/watchdog` is exposed (the RK3576 has
one; the 6.1.75 BSP does not enable it), so a full kernel hang has no
safety net.

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
