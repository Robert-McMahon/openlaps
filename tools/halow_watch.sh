#!/usr/bin/env bash
# Range-test logger for the HaLow mesh. Run on the pit laptop while someone
# walks or drives the car radio away. Once a second it prints, and appends to
# a CSV, what the *car radio* sees: signal from each fixed radio, which radio
# it is currently routing through to reach the pit radio, and ping loss.
#
#   tools/halow_watch.sh [car-radio-ip] [pit-radio-mac]
#
# Stop with Ctrl-C. When the car radio is unreachable the row says "no link".
set -u
CAR=${1:-192.168.12.158}
PIT_MAC=${2:-94:83:c4:67:4b:e3}
OUT=${HALOW_WATCH_OUT:-$HOME/halow-range-$(date +%Y%m%d-%H%M%S).csv}

declare -A NAME=(
  [94:83:c4:67:42:3f]=master
  [94:83:c4:67:4b:e3]=pit
  [94:83:c4:67:44:bf]=spare
)

echo "time,ping_ms,master_dbm,pit_dbm,spare_dbm,next_hop_to_pit,tx_mcs" >"$OUT"
echo "logging to $OUT"
printf '%-8s %8s %7s %7s %7s  %-8s\n' time ping_ms master pit spare via

while :; do
  t=$(date +%H:%M:%S)
  ping_ms=$(ping -c1 -W1 "$CAR" 2>/dev/null | sed -n 's/.*time=\([0-9.]*\).*/\1/p')
  raw=$(timeout 4 ssh -o BatchMode=yes -o ConnectTimeout=2 -o LogLevel=ERROR \
        -o ControlMaster=auto -o ControlPath=/tmp/halow-watch-%r@%h -o ControlPersist=30 \
        root@"$CAR" "iw dev wlh0 station dump | grep -E 'Station|signal avg|tx bitrate'; echo ---; iw dev wlh0 mpath dump | grep -i '^$PIT_MAC'" 2>/dev/null)
  if [ -z "$raw" ]; then
    printf '%-8s %8s  no link\n' "$t" "${ping_ms:--}"
    echo "$t,${ping_ms:-},,,,no-link," >>"$OUT"
    sleep 1; continue
  fi
  declare -A SIG=([master]="" [pit]="" [spare]="")
  cur=""; mcs=""
  while IFS= read -r line; do
    case "$line" in
      Station*) mac=$(awk '{print $2}' <<<"$line"); cur=${NAME[$mac]:-} ;;
      *"signal avg"*) [ -n "$cur" ] && SIG[$cur]=$(awk '{print $3}' <<<"$line") ;;
      *"tx bitrate"*) [ "$cur" = pit ] && mcs=$(grep -o 'MCS [0-9]*' <<<"$line" | awk '{print $2}') ;;
    esac
  done <<<"${raw%%---*}"
  hop_mac=$(awk '{print $2}' <<<"${raw##*---}" | tr -d '[:space:]')
  via=${NAME[$hop_mac]:-${hop_mac:--}}
  [ "$via" = pit ] && via=direct
  printf '%-8s %8s %7s %7s %7s  %-8s\n' "$t" "${ping_ms:--}" "${SIG[master]:--}" "${SIG[pit]:--}" "${SIG[spare]:--}" "$via"
  echo "$t,${ping_ms:-},${SIG[master]},${SIG[pit]},${SIG[spare]},$via,$mcs" >>"$OUT"
  sleep 1
done
