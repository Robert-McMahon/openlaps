#!/usr/bin/env bash
# Keep the pit's telemetry link on the car's Starlink tunnel when it is usable.
#
# The pit leaf dials the Starlink-only tunnel first and the plain HaLow address
# second (deploy/.env NATS_VEHICLE_EXTRA_HOST / NATS_LEAFNODE_URL_ALT). NATS only
# re-walks that list when the link drops, so after a Starlink outage it would
# sit on HaLow for good. This loop watches for that: when the leaf is on the
# HaLow address and the Starlink tunnel has answered for 30 s in a row, it
# restarts the pit NATS container (a second or two of stale data) so the link
# redials Starlink. At most one restart every two minutes.
#
#   tools/prefer_starlink.sh        # run on the pit laptop; Ctrl-C to stop
set -u
TUNNEL=${OPENLAPS_STARLINK_TUNNEL:-100.94.249.23}
HALOW=${OPENLAPS_CAR_LAN:-192.168.12.222}
NATS=${OPENLAPS_PIT_NATS:-openlaps-pit-nats-1}
good=0
last=0
while sleep 3; do
  leaf=$(curl -s -m 2 localhost:8222/leafz | grep -o '"ip": *"[^"]*"' | head -1 | grep -o '[0-9.]*"$' | tr -d '"')
  if [ "$leaf" != "$HALOW" ]; then good=0; continue; fi
  if timeout 2 bash -c "</dev/tcp/$TUNNEL/7422" 2>/dev/null; then good=$((good + 1)); else good=0; fi
  now=$(date +%s)
  if [ "$good" -ge 10 ] && [ $((now - last)) -ge 120 ]; then
    echo "$(date +%T) Starlink steady for 30 s, moving the link back"
    docker restart "$NATS" >/dev/null
    last=$now
    good=0
  fi
done
