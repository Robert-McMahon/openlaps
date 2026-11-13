#!/usr/bin/env bash
# Keep the ECU's USB port reachable from the pit over USB/IP.
#
#     tools/ecu_usbip.sh export     # on the vehicle SBC (openlaps-ecu-export.service)
#     tools/ecu_usbip.sh attach     # on the pit host    (openlaps-ecu-attach.service)
#     tools/ecu_usbip.sh unexport   # vehicle: hand the ECU back to its local driver
#     tools/ecu_usbip.sh detach     # pit: drop the vhci port(s) holding the ECU
#     tools/ecu_usbip.sh redirect   # pit: stream the attached ECU into the Windows VM (openlaps-ecu-redirect.service)
#     tools/ecu_usbip.sh unredirect # pit: remove the VM's usb-redir binding
#     tools/ecu_usbip.sh status     # either end: what this end can see right now
#
# docs/operations/remote-ecu.md is the runbook. Both loops exist for one
# reason: the ECU leaves the bus every time it resets -- a power cycle, a
# key-off, the reboot after a calibration burn -- and comes back as a *new*
# USB device. On the vehicle the new device is bound to its ordinary driver,
# not to usbip-host, so it is invisible to the pit until something binds it
# again; on the pit the vhci port is torn down the moment the server's device
# goes away. A udev rule could cover the first half, but the second half has
# no local event to hang off, so both ends poll. Polling every few seconds is
# free next to the reattach time anyone will notice.
#
# Configuration is read from /etc/default/openlaps-ecu
# (deploy/ecu/openlaps-ecu.env is the template); a variable already set in
# the environment wins over the file, so one value can be overridden by hand.
#
#   OPENLAPS_ECU_USB_VID   4 hex digits, no 0x -- `lsusb` on the vehicle
#   OPENLAPS_ECU_USB_PID   4 hex digits, no 0x
#   OPENLAPS_ECU_HOST      attach only: the vehicle's address on the car LAN
#   OPENLAPS_ECU_POLL_S    loop period, default 3
#   OPENLAPS_ECU_VM        redirect only: the Windows guest's container, default omarchy-windows
#   OPENLAPS_ECU_VM_PORT   redirect only: TCP port QEMU listens on for the stream, default 4160
#
# The pit hands the attached ECU to the guest with usbredir, not QEMU's
# usb-host: libusb inside the dockur container never receives hotplug events
# (no udevd there), so usb-host only ever opens devices that were present when
# the VM booted -- every ECU reset or re-arm needed a Windows restart.
# `redirect` opens the device on the host with usbredirect, where udev works,
# and streams it to a usb-redir device it adds to the guest's xhci.0 through
# the QEMU monitor. dockur DNATs every inbound TCP port it does not know to
# the guest, so the loop also exempts its port from that chain.
#
# The device is found by VID:PID rather than by bus id because the bus id
# follows the physical port: move the cable to another socket and a bus id
# pinned in config would export the wrong thing, or nothing.
set -euo pipefail

CONFIG="${OPENLAPS_ECU_CONFIG:-/etc/default/openlaps-ecu}"
if [[ -r "$CONFIG" ]]; then
	# Parsed, not sourced: only OPENLAPS_ECU_* assignments are taken, and
	# nothing in the file is executed as root.
	while IFS='=' read -r key value; do
		[[ "$key" =~ ^OPENLAPS_ECU_[A-Z_]+$ ]] || continue
		[[ -z "${!key:-}" ]] || continue
		value="${value%%#*}"
		value="${value//[[:space:]]/}"
		export "$key=$value"
	done <"$CONFIG"
fi

VID="${OPENLAPS_ECU_USB_VID:-}"
PID="${OPENLAPS_ECU_USB_PID:-}"
POLL="${OPENLAPS_ECU_POLL_S:-3}"
HOST="${OPENLAPS_ECU_HOST:-}"
VM="${OPENLAPS_ECU_VM:-omarchy-windows}"
VM_PORT="${OPENLAPS_ECU_VM_PORT:-4160}"
VM_MONITOR="${OPENLAPS_ECU_VM_MONITOR:-/run/shm/monitor.sock}"
LOOPS="${OPENLAPS_ECU_LOOPS:-}" # tests only: stop a loop after this many passes
SYS_USB="${OPENLAPS_ECU_SYSFS:-/sys/bus/usb/devices}" # overridable for tests only

log() { printf '%s\n' "$*" >&2; }

need_ids() {
	if [[ ! "$VID" =~ ^[0-9a-fA-F]{4}$ || ! "$PID" =~ ^[0-9a-fA-F]{4}$ ]]; then
		log "OPENLAPS_ECU_USB_VID/PID must be 4 hex digits each (got '$VID:$PID'); see lsusb on the vehicle"
		exit 2
	fi
	VID="${VID,,}"
	PID="${PID,,}"
}

# Bus ids (e.g. 1-1.2) of every *physical* USB device matching VID:PID on this
# host, excluding vhci ports so the pit never mistakes its own attached copy
# for a local device.
local_busids() {
	local dev path
	for dev in "$SYS_USB"/*; do
		[[ -r "$dev/idVendor" && -r "$dev/idProduct" ]] || continue
		[[ "$(<"$dev/idVendor")" == "$VID" && "$(<"$dev/idProduct")" == "$PID" ]] || continue
		path="$(readlink -f "$dev")"
		[[ "$path" == *vhci_hcd* ]] && continue
		basename "$dev"
	done
}

# True when a vhci port on this host already carries the ECU.
attached_here() {
	local dev
	for dev in "$SYS_USB"/*; do
		[[ -r "$dev/idVendor" && -r "$dev/idProduct" ]] || continue
		[[ "$(<"$dev/idVendor")" == "$VID" && "$(<"$dev/idProduct")" == "$PID" ]] || continue
		[[ "$(readlink -f "$dev")" == *vhci_hcd* ]] && return 0
	done
	return 1
}

bound_to_usbip() {
	local drv="$SYS_USB/$1/driver"
	[[ -L "$drv" && "$(basename "$(readlink -f "$drv")")" == usbip-host ]]
}

cmd_export() {
	need_ids
	# Built in (=y) on the openlaps Luckfox kernel, where modprobe may find no
	# module tree at all; what matters is that the driver is registered.
	modprobe usbip-host 2>/dev/null || true
	if [[ ! -d /sys/bus/usb/drivers/usbip-host ]]; then
		log "usbip-host driver not present: the kernel needs CONFIG_USBIP_HOST (deploy/targets/luckfox-omni3576/README.md)"
		exit 3
	fi
	log "exporting $VID:$PID every ${POLL}s"
	local busid
	while :; do
		for busid in $(local_busids); do
			if ! bound_to_usbip "$busid"; then
				if usbip bind -b "$busid"; then
					log "bound $busid ($VID:$PID) to usbip-host"
				fi
			fi
		done
		sleep "$POLL"
	done
}

cmd_unexport() {
	need_ids
	local busid
	for busid in $(local_busids); do
		if bound_to_usbip "$busid"; then
			usbip unbind -b "$busid" && log "unbound $busid; the local driver has it again"
		fi
	done
}

# Remote bus id of the ECU from `usbip list -r`, whose device lines read
#   "      1-1.2: Haltech : unknown product (xxxx:yyyy)"
remote_busid() {
	usbip list -r "$HOST" 2>/dev/null |
		sed -nE "s/^[[:space:]]*([0-9][0-9.-]*):.*\\(${VID}:${PID}\\)[[:space:]]*$/\\1/Ip" |
		head -n1
}

cmd_attach() {
	need_ids
	if [[ -z "$HOST" ]]; then
		log "OPENLAPS_ECU_HOST is not set"
		exit 2
	fi
	modprobe vhci-hcd 2>/dev/null || true
	if [[ ! -d /sys/devices/platform/vhci_hcd.0 ]]; then
		log "vhci-hcd not loaded: install the distro's usbip package (Arch: pacman -S usbip)"
		exit 3
	fi
	log "keeping $VID:$PID from $HOST attached, checking every ${POLL}s"
	local busid waiting=0
	while :; do
		if ! attached_here; then
			busid="$(remote_busid || true)"
			if [[ -n "$busid" ]]; then
				if usbip attach -r "$HOST" -b "$busid"; then
					log "attached $HOST:$busid ($VID:$PID)"
					waiting=0
				fi
			elif ((waiting == 0)); then
				# Logged once per absence, not every poll: an ECU that is
				# keyed off for an hour should not fill the journal.
				log "$VID:$PID not exported by $HOST (ECU off, export stopped, or link down); waiting"
				waiting=1
			fi
		fi
		sleep "$POLL"
	done
}

cmd_detach() {
	need_ids
	# `usbip port` prints a "Port NN:" header per port followed by a line
	# carrying "(vid:pid)"; detach every port whose device is the ECU.
	local port
	for port in $(usbip port 2>/dev/null | awk -v id="(${VID}:${PID})" '
		/^Port [0-9]+:/ { p = $2; sub(":", "", p) }
		tolower($0) ~ id { print p + 0 }'); do
		usbip detach -p "$port" && log "detached vhci port $port"
	done
}

# BUS-DEVNUM (usbredirect's form) of the ECU on this host, vhci copy included:
# on the pit that copy is exactly what should go to the guest.
host_busdev() {
	local dev
	for dev in "$SYS_USB"/*; do
		[[ -r "$dev/idVendor" && -r "$dev/idProduct" && -r "$dev/busnum" && -r "$dev/devnum" ]] || continue
		[[ "$(<"$dev/idVendor")" == "$VID" && "$(<"$dev/idProduct")" == "$PID" ]] || continue
		echo "$(<"$dev/busnum")-$(<"$dev/devnum")"
		return 0
	done
	return 1
}

vm_running() {
	[[ "$(docker inspect -f '{{.State.Running}}' "$VM" 2>/dev/null)" == true ]]
}

vm_ip() {
	docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$VM" 2>/dev/null
}

# One HMP command to the guest's QEMU; prints whatever QEMU answered (nothing
# on success for chardev-add/device_add, the error text otherwise).
qemu_monitor() {
	# -q 1: send, then give QEMU a second to answer before closing.
	printf '%s\r\n' "$1" | docker exec -i "$VM" nc -U -q 1 -w 2 "$VM_MONITOR" 2>/dev/null |
		sed 's/\x1b\[[0-9;]*[A-Za-z]//g' | tr -d '\r' | grep -vE '^\(qemu\)|^QEMU [0-9]' || true
}

# Idempotent: the NAT exemption, the listening chardev and the usb-redir device.
ensure_vm_binding() {
	local rule=(-p tcp --dport "$VM_PORT" -m comment --comment openlaps-ecu -j RETURN) err
	if ! docker exec "$VM" iptables -t nat -C QEMU_DNAT "${rule[@]}" >/dev/null 2>&1; then
		docker exec "$VM" iptables -t nat -I QEMU_DNAT "${rule[@]}" || {
			log "could not exempt port $VM_PORT from $VM's guest NAT"
			return 1
		}
	fi
	if ! qemu_monitor 'info chardev' | grep -q '^openlaps_ecu_cd:'; then
		err="$(qemu_monitor "chardev-add socket,id=openlaps_ecu_cd,host=0.0.0.0,port=$VM_PORT,server=on,wait=off")"
		[[ -z "$err" ]] || {
			log "QEMU chardev-add: $err"
			return 1
		}
	fi
	if ! qemu_monitor 'info qtree' | grep -q 'id "openlaps_ecu"'; then
		err="$(qemu_monitor 'device_add usb-redir,id=openlaps_ecu,chardev=openlaps_ecu_cd,bus=xhci.0')"
		[[ -z "$err" ]] || {
			log "QEMU device_add usb-redir: $err"
			return 1
		}
	fi
}

cmd_redirect() {
	need_ids
	if ! command -v usbredirect >/dev/null; then
		log "usbredirect not found: install the distro's usbredir package (Arch: pacman -S usbredir)"
		exit 3
	fi
	log "streaming $VID:$PID into $VM (QEMU port $VM_PORT), checking every ${POLL}s"
	local busdev ip stream waiting_vm=0 waiting_dev=0 pass=0
	while :; do
		[[ -z "$LOOPS" ]] || ((pass++ < LOOPS)) || break
		if ! vm_running; then
			((waiting_vm)) || log "$VM is not running; waiting"
			waiting_vm=1
		elif ! busdev="$(host_busdev)"; then
			waiting_vm=0
			((waiting_dev)) || log "$VID:$PID is not on this host (not attached yet); waiting"
			waiting_dev=1
		elif ensure_vm_binding && ip="$(vm_ip)" && [[ -n "$ip" ]]; then
			waiting_vm=0 waiting_dev=0
			log "redirecting $busdev ($VID:$PID) to $VM at $ip:$VM_PORT"
			usbredirect --device "$busdev" --to "$ip:$VM_PORT" -k &
			stream=$!
			# usbredirect exits when the VM side closes, but not when its
			# device leaves the bus: an ECU reset leaves it holding the old
			# bus-devnum with a dead placeholder in the guest (pit host,
			# 2026-11-13). Watch the device here and end the stream as soon
			# as it is gone or has come back as a new one.
			while kill -0 "$stream" 2>/dev/null; do
				if [[ "$(host_busdev || true)" != "$busdev" ]]; then
					log "$busdev left the bus; ending its stream"
					kill "$stream" 2>/dev/null || true
					break
				fi
				sleep "$POLL"
			done
			wait "$stream" 2>/dev/null || true
			log "stream for $busdev ended"
		fi
		sleep "$POLL"
	done
}

cmd_unredirect() {
	# The unit's cgroup kill already ended usbredirect; take the guest-side
	# binding down too so the next VM boot or `redirect` starts clean.
	vm_running || return 0
	if qemu_monitor 'info qtree' | grep -q 'id "openlaps_ecu"'; then
		qemu_monitor 'device_del openlaps_ecu' >/dev/null
		log "removed usb-redir device from $VM"
	fi
	if qemu_monitor 'info chardev' | grep -q '^openlaps_ecu_cd:'; then
		qemu_monitor 'chardev-remove openlaps_ecu_cd' >/dev/null
	fi
}

cmd_status() {
	need_ids
	local busid
	echo "local physical $VID:$PID:"
	for busid in $(local_busids); do
		if bound_to_usbip "$busid"; then
			echo "  $busid (exported via usbip-host)"
		else
			echo "  $busid (local driver)"
		fi
	done
	if attached_here; then
		echo "attached here over vhci: yes"
	else
		echo "attached here over vhci: no"
	fi
	if [[ -n "$HOST" ]]; then
		echo "exported by $HOST: $(remote_busid || true)"
	fi
}

case "${1:-}" in
export) cmd_export ;;
unexport) cmd_unexport ;;
attach) cmd_attach ;;
detach) cmd_detach ;;
redirect) cmd_redirect ;;
unredirect) cmd_unredirect ;;
status) cmd_status ;;
*)
	sed -n '2,10p' "$0" >&2
	exit 64
	;;
esac
