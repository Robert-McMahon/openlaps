#!/usr/bin/env bash
# Keep the ECU's USB port reachable from the pit over USB/IP.
#
#     tools/ecu_usbip.sh export     # on the vehicle SBC (openlaps-ecu-export.service)
#     tools/ecu_usbip.sh attach     # on the pit host    (openlaps-ecu-attach.service)
#     tools/ecu_usbip.sh unexport   # vehicle: hand the ECU back to its local driver
#     tools/ecu_usbip.sh detach     # pit: drop the vhci port(s) holding the ECU
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
status) cmd_status ;;
*)
	sed -n '2,8p' "$0" >&2
	exit 64
	;;
esac
