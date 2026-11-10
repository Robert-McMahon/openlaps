# Remote ECU tuning

Tune the car's ECU (a Haltech Elite in the example profile) from the pit. The
ECU's USB port is exported by the vehicle SBC over USB/IP, crosses the HaLow
link, is attached on the pit host, and is passed into a Windows guest where
the vendor's tuning software (NSP) runs as if the cable were plugged in.

```text
ECU ──USB──▶ vehicle SBC              pit host (Omarchy)
             usbip-host ◀─ bind loop   vhci-hcd ◀─ attach loop
             usbipd :3240 ══ HaLow ══▶ /dev/bus/usb ◀─ redirect loop (usbredirect)
                                                        ║ TCP :4160
                                       Windows guest: QEMU usb-redir ──▶ NSP
```

| Piece | Where | File |
| --- | --- | --- |
| USB/IP server, off by default | vehicle | `deploy/systemd/openlaps-usbipd.service` |
| Bind loop (arms remote access) | vehicle | `deploy/systemd/openlaps-ecu-export.service` |
| Pit-only firewall on 3240 | vehicle | `deploy/nft/ecu-usbip.nft` |
| Attach loop | pit | `deploy/systemd/openlaps-ecu-attach.service` |
| Redirect loop (host → guest, usbredir) | pit | `deploy/systemd/openlaps-ecu-redirect.service` |
| Windows guest | pit | `ecu-windows` in `deploy/pit-compose.yaml`, profile `ecu` |
| Shared script | both | `tools/ecu_usbip.sh` |
| Shared settings | both | `deploy/ecu/openlaps-ecu.env` → `/etc/default/openlaps-ecu` |

## Limits

!!! danger "Never update ECU firmware over the link"
    A dropout during a firmware update can leave the ECU unable to boot. Do
    firmware updates on a cable at the car, with a stable supply.

- **Calibration changes only, car stationary.** USB/IP turns every USB
  transaction into a round trip over the radio. A write that stalls on a
  dropout is at best a timeout in NSP and at worst a half-applied change on a
  running engine. Arm remote access in the pit lane or the garage, not on
  track.
- **Check the category's regulations.** Many forbid changing engine settings
  from the pit while the car is on circuit. This setup does not stop anyone
  doing it; disarming the vehicle end does.
- **usbipd has no authentication.** Whoever connects first can attach the ECU
  and write to it. The export is off until armed, and the nft rule limits
  port 3240 to the pit's address. Keep both.
- **While armed, the vehicle cannot use the ECU's USB port itself.** CAN
  telemetry is unaffected.
- **Bandwidth is not the concern; latency is.** NSP's traffic is small
  against the ~1.4 Mbit/s of headroom in the [link budget](../LINK_BUDGET.md),
  but NSP may time out on a link whose round trip spikes. Prove it on the
  bench first (below).

## 1. Find the ECU's USB IDs

On the vehicle, with the ECU powered and its USB cable in:

```bash
lsusb
```

Note the `ID xxxx:yyyy` on the ECU's line. Those two 4-hex-digit values go
into both hosts' `/etc/default/openlaps-ecu` and the pit's `deploy/.env`, in
the same form, without `0x`.

## 2. Vehicle

The kernel needs `CONFIG_USBIP_HOST`. On the Luckfox that means the two
USBIP lines in the [target's kernel fragment](../hardware/luckfox-omni3576.md);
check with:

```bash
zcat /proc/config.gz | grep USBIP_HOST
```

Then:

```bash
sudo apt install usbip
sudo install -m 0755 tools/ecu_usbip.sh /usr/local/sbin/openlaps-ecu-usbip
sudo install -m 0644 deploy/ecu/openlaps-ecu.env /etc/default/openlaps-ecu
sudoedit /etc/default/openlaps-ecu          # the VID and PID from step 1
sudo install -m 0644 deploy/systemd/openlaps-usbipd.service \
     deploy/systemd/openlaps-ecu-export.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Firewall, persistent across reboots (edit `PIT_ADDR` first if the pit is not
`192.168.12.203`):

```bash
sudo install -D -m 0644 deploy/nft/ecu-usbip.nft /etc/nftables.d/ecu-usbip.nft
echo 'include "/etc/nftables.d/ecu-usbip.nft"' | sudo tee -a /etc/nftables.conf
sudo systemctl enable --now nftables
sudo nft list table inet openlaps_ecu
```

Do **not** enable the export unit. It is armed per session (step 5).

## 3. Pit host

```bash
sudo pacman -S usbip usbredir
sudo install -m 0755 tools/ecu_usbip.sh /usr/local/sbin/openlaps-ecu-usbip
sudo install -m 0644 deploy/ecu/openlaps-ecu.env /etc/default/openlaps-ecu
sudoedit /etc/default/openlaps-ecu          # same VID and PID; OPENLAPS_ECU_HOST is the vehicle;
                                            # OPENLAPS_ECU_VM is the guest's container (step 4)
sudo install -m 0644 deploy/systemd/openlaps-ecu-attach.service \
     deploy/systemd/openlaps-ecu-redirect.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openlaps-ecu-attach openlaps-ecu-redirect
```

Both loops are safe to leave enabled. The attach loop only attaches what the
vehicle is exporting; the redirect loop only streams what the attach loop
has put on this host, into a guest that is running. Each logs once and waits
while it has nothing to do.

### Why a redirect loop and not QEMU `usb-host`

`usb-host` matches a device by VID:PID and, on a bare host, follows it
through resets. Inside the dockur container it cannot: libusb's libudev
backend disables its hotplug monitor when no udevd is running and `/dev` is
not devtmpfs, so QEMU only ever knows the USB devices that existed when the
VM booted. Every ECU reset, re-arm or vehicle reboot therefore needed a
Windows restart (proven 2026-11-08 on the pit host: `info usbhost` in the
QEMU monitor never listed the reattached vhci device, and a fresh
`device_add usb-host` could not find it either).

`redirect` opens the ECU on the host with `usbredirect`, where udev works,
and streams it over TCP to a `usb-redir` device it adds to the guest's
`xhci.0` through the QEMU monitor. When the ECU leaves the bus the stream
ends, the guest sees the port go empty, and the loop starts a new stream as
soon as the attach loop has the ECU back. dockur DNATs every inbound TCP
port it does not know to the guest, so the loop also exempts its port from
that chain inside the container (`iptables -t nat ... QEMU_DNAT ... RETURN`).

## 4. Windows guest

The guest is [dockur/windows](https://github.com/dockur/windows): QEMU/KVM
inside a container, so the pit host needs `/dev/kvm` (`ls /dev/kvm`; enable
virtualisation in the firmware if it is missing).

Set `OPENLAPS_ECU_USB_VID`, `OPENLAPS_ECU_USB_PID` and `ECU_WINDOWS_PASSWORD`
in `deploy/.env` (see `example.env`), then:

```bash
docker compose -f deploy/pit-compose.yaml --profile ecu up -d ecu-windows
```

The first start downloads and installs Windows into the
`ecu-windows-storage` volume, unattended; follow it at
`http://<pit-host>:8006`. Once at the desktop, install NSP, which brings its
own USB driver. Use RDP on 3389 for day-to-day work. Windows needs a licence
like any other install. Set `OPENLAPS_ECU_VM=ecu-windows` in
`/etc/default/openlaps-ecu` so the redirect loop feeds this guest; the
service has no USB passthrough of its own.

Stop it with `docker compose ... stop ecu-windows`; the two-minute grace
period lets Windows shut down instead of being cut off.

### Using Omarchy's Windows VM instead

On an Omarchy pit host that already has `omarchy-windows-vm` installed, skip
`ecu-windows` and use that guest: leave `OPENLAPS_ECU_VM=omarchy-windows`
(the default) and nothing in its compose needs to change. If an earlier
setup added a `-device usb-host,...,vendorid=0x<VID>,productid=0x<PID>` for
the ECU to the `ARGUMENTS` line in `/var/lib/omarchy/windows/docker-compose.yml`,
remove it: at VM boot that device would claim the ECU ahead of the redirect
loop. The loop adds its own usb-redir device at run time, so the guest needs
no USB passthrough configured. The VM only reads `ARGUMENTS` at start.

The desktop `windows-usb` tool on the pit host uses the same mechanism for
locally plugged devices and reserves QEMU ports 4100-4159; the ECU uses 4160.

## 5. Each session

Arm, on the vehicle:

```bash
sudo systemctl start openlaps-ecu-export
```

Within a few seconds the pit's attach loop picks it up, the redirect loop
streams it into the guest (`redirecting 5-N ... to omarchy-windows` in its
journal), Windows enumerates it, and NSP connects. Check either end with:

```bash
sudo openlaps-ecu-usbip status
journalctl -u openlaps-ecu-export -u openlaps-usbipd -f           # vehicle
journalctl -u openlaps-ecu-attach -u openlaps-ecu-redirect -f     # pit
```

Disarm when finished, on the vehicle:

```bash
sudo systemctl stop openlaps-ecu-export
```

That unbinds the ECU, which ends the pit's session at once, and stops
usbipd. Disarming is the reliable way to make sure nothing on the pit can
reach the ECU.

### When the ECU resets

A key-off or power cycle makes the ECU leave the bus and return as a new
device. The vehicle loop rebinds it, the pit's attach loop reattaches it, the
stream that was feeding the guest ends and the redirect loop starts a new one
for the new device. Expect NSP to report a lost connection and reconnect
within roughly ten to fifteen seconds. No VM restart is involved; the same
holds after a vehicle reboot or a disarm and re-arm.

## 6. Bench test before relying on it

Run this with the car on the bench and the engine off.

1. Measure the real link: `ping -c 200 192.168.12.222` from the pit, over
   HaLow, and note the average and worst round trip.
2. Connect NSP over the wired car LAN first, read the calibration, make and
   revert one harmless change.
3. Add delay and loss on the pit's interface toward the car, starting near
   the measured figures and doubling them:

    ```bash
    sudo tc qdisc add dev <iface> root netem delay 30ms 10ms loss 1%
    # ... repeat step 2 ...
    sudo tc qdisc del dev <iface> root
    ```

4. Key the ECU off and on while NSP is connected and time the reconnect.

Note where NSP starts timing out. That is the margin the link has to keep.

## Troubleshooting

| Symptom | Look at |
| --- | --- |
| `usbip-host driver not present` in the export journal | the kernel lacks `CONFIG_USBIP_HOST` (step 2) |
| Pit loop keeps logging `not exported by` | export unit not started; ECU off; `nc -zv 192.168.12.222 3240` from the pit; the nft rule's `PIT_ADDR` |
| `vhci-hcd not loaded` on the pit | `usbip` package not installed, or the kernel lacks the module |
| `status` shows attached on the pit but the guest has no device | `journalctl -u openlaps-ecu-redirect`: `is not running` means `OPENLAPS_ECU_VM` names the wrong container; `usbredirect not found` means the package is missing |
| Redirect journal shows `redirecting ...` then `stream ... ended` at once, repeatedly | the host cannot reach QEMU's port: `docker exec <vm> iptables -t nat -S QEMU_DNAT \| head -2` should show the RETURN for the port; a stale `usb-host` for the ECU in the VM's `ARGUMENTS` also steals the device at boot |
| Guest loses the ECU after a reset and does not get it back | the attach loop has it back (`status`) but no new `redirecting` line: restart `openlaps-ecu-redirect` and report it |
| NSP connects but times out on reads | link latency; see step 6, and try a narrower HaLow channel for margin |
