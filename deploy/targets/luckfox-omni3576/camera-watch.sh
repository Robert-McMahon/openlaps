#!/usr/bin/env bash
# Restart go2rtc when the camera has re-enumerated on USB.
#
# Docker resolves the camera's /dev/v4l/by-id link to a device number once,
# when the container starts. If the camera drops off the bus and comes back,
# it gets a new number and the container is left holding a dead /dev/video0
# ("No such device or address") until it is restarted. This loop compares the
# two numbers and restarts the container when they differ.
#
# It will not hammer a camera that keeps dropping:
#   - no camera on the bus          -> wait, do nothing
#   - container stopped by someone  -> leave it stopped
#   - camera must sit still for SETTLE polls before a restart
#   - after each restart the next one is held off for HOLD seconds, doubling
#     each time up to HOLD_MAX; HOLD resets once the camera has stayed good
#     for CALM seconds
#
# CAMERA_WATCH_DRY_RUN=1 logs what it would do without restarting anything.
set -u
DEV=${CAMERA_WATCH_DEVICE:-/dev/v4l/by-id/usb-Amba_Insta360_X3-video-index0}
CTR=${CAMERA_WATCH_CONTAINER:-openlaps-vehicle-go2rtc}
CTR_DEV=${CAMERA_WATCH_CONTAINER_DEVICE:-/dev/video0}
POLL=${CAMERA_WATCH_POLL:-2}
SETTLE=${CAMERA_WATCH_SETTLE:-3}
HOLD_MIN=${CAMERA_WATCH_HOLD_MIN:-20}
HOLD_MAX=${CAMERA_WATCH_HOLD_MAX:-600}
CALM=${CAMERA_WATCH_CALM:-600}
DRY=${CAMERA_WATCH_DRY_RUN:-0}

hold=$HOLD_MIN   # seconds to wait after the next restart
not_before=0     # earliest time the next restart is allowed
good_since=0     # when the numbers last started matching
seen=""          # host device number on the previous poll
steady=0         # polls in a row the host number has not changed
state=""         # last state logged, so each change is logged once

say() { [ "$state" = "$1" ] || { state=$1; echo "$2"; }; }

while sleep "$POLL"; do
	now=$(date +%s)
	host=$(stat -L -c %t:%T "$DEV" 2>/dev/null) || { say absent "camera not on the bus, waiting"; seen=""; good_since=0; continue; }
	ctr=$(docker exec "$CTR" stat -c %t:%T "$CTR_DEV" 2>/dev/null) || { say stopped "$CTR is not running, leaving it alone"; good_since=0; continue; }

	if [ "$host" = "$seen" ]; then steady=$((steady + 1)); else steady=1; seen=$host; fi

	if [ "$host" = "$ctr" ]; then
		say ok "camera $host matches the container"
		[ "$good_since" -ne 0 ] || good_since=$now
		if [ "$hold" -ne "$HOLD_MIN" ] && [ $((now - good_since)) -ge "$CALM" ]; then
			hold=$HOLD_MIN
			echo "camera steady for ${CALM}s, hold-off back to ${hold}s"
		fi
		continue
	fi

	good_since=0
	[ "$steady" -ge "$SETTLE" ] || continue
	if [ "$now" -lt "$not_before" ]; then
		say holding "camera moved to $host (container has $ctr); holding off $((not_before - now))s more"
		continue
	fi

	state=restart
	echo "camera moved to $host (container has $ctr); restarting $CTR, next restart no sooner than ${hold}s"
	if [ "$DRY" = 1 ]; then echo "dry run: not restarting"; else docker restart "$CTR" >/dev/null || echo "restart failed"; fi
	not_before=$(( $(date +%s) + hold ))
	hold=$((hold * 2)); [ "$hold" -le "$HOLD_MAX" ] || hold=$HOLD_MAX
done
