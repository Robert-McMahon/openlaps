#!/usr/bin/env bash
# One-screen health check of the four HaLow mesh radios. Run on the pit host.
#
#   tools/halow_status.sh
#
# For each radio: whether it answers, how many mesh peers it has (3 = it can
# hear every other radio), whether the link-probe service is running, and the
# signal it sees from each peer. Fixed radios should see each other at about
# -70 dBm or stronger.
set -u
declare -A NAME=(
  [94:83:c4:67:42:3f]=master [94:83:c4:67:4b:e3]=pit
  [94:83:c4:67:44:bf]=spare  [94:83:c4:67:3c:13]=car
)
for h in master:192.168.12.1 pit:192.168.12.201 spare:192.168.12.122 car:192.168.12.158; do
  n=${h%%:*}; ip=${h#*:}
  out=$(timeout 8 ssh -o BatchMode=yes -o ConnectTimeout=4 -o LogLevel=ERROR root@"$ip" \
    'iw dev wlh0 station dump | awk "/^Station/{m=\$2} /signal avg/{s=\$3} /mesh plink/{print m, s, \$3}"; echo "probe $(ps w | grep -c "[u]sr/sbin/mesh-probe")"' 2>/dev/null)
  if [ -z "$out" ]; then printf '%-7s %-15s NOT ANSWERING\n' "$n" "$ip"; continue; fi
  peers=$(grep -c ESTAB <<<"$out"); probe=$(awk '/^probe/{print $2}' <<<"$out")
  sig=$(grep ESTAB <<<"$out" | while read -r mac s _; do printf '%s %s  ' "${NAME[$mac]:-$mac}" "$s"; done)
  printf '%-7s %-15s peers %s  probe %s  | %s\n' "$n" "$ip" "$peers" "$probe" "$sig"
done
