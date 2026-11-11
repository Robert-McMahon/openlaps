#!/usr/bin/env bash
# Choose which link the pit uses to reach the car. Run on the pit host.
#
#   tools/car_link.sh status     show the path in use
#   tools/car_link.sh starlink   stop using HaLow; car is reached over Starlink only
#   tools/car_link.sh auto       back to normal: HaLow when it works, else Starlink
#
# The pit's NATS leaf dials the car's Tailscale address, and Tailscale uses
# the HaLow LAN whenever the car answers there. "starlink" makes the car's
# LAN address unreachable from this host (a blackhole route), so Tailscale
# gives up on that path within a few seconds, on both ends, and stays on
# Starlink. Nothing changes on the car or the radios, so this still works
# when HaLow is too poor to reach them. It lasts until `auto` or a reboot.
#
# While it is on, the car's LAN address does not answer from here: use the
# Tailscale name for ssh (ssh luckfox@openlaps-car).
set -euo pipefail
CAR_LAN=${OPENLAPS_CAR_LAN_ADDR:-192.168.12.222}
CAR_TS=${OPENLAPS_CAR_TAILNET_NAME:-openlaps-car}

blocked() { ip route show type blackhole | grep -q "^blackhole $CAR_LAN\b"; }
status() {
	if blocked; then echo "mode: STARLINK ONLY (HaLow path to $CAR_LAN is blocked)"; else echo "mode: auto (HaLow preferred, Starlink when HaLow fails)"; fi
	tailscale status | awk -v n="$CAR_TS" '$2==n{ $1=$3=$4=""; print "path:" $0 }' | cut -c1-110
}

case "${1:-status}" in
	status) status ;;
	starlink)
		sudo ip route replace blackhole "$CAR_LAN/32"
		echo "HaLow path blocked. Waiting for the tunnel to move to Starlink..."
		for _ in $(seq 1 15); do sleep 2; tailscale status | awk -v n="$CAR_TS" '$2==n' | grep -q "$CAR_LAN" || break; done
		status ;;
	auto)
		sudo ip route del blackhole "$CAR_LAN/32" 2>/dev/null || true
		tailscale ping -c 3 --timeout 3s "$CAR_TS" >/dev/null 2>&1 || true
		status ;;
	*) sed -n '2,8p' "$0"; exit 2 ;;
esac
