#!/usr/bin/env bash
# Record the car camera to the NVMe whenever go2rtc is running.
#
# The camera can only be opened once, by go2rtc, so this records go2rtc's
# own `car_h264` stream over local RTSP without re-encoding: the files are
# exactly what the pit sees (640x360, ~800k video + 64k audio, about 9 GB
# per 24 h). Being a viewer, it also keeps the encoder running for as long
# as the car is on, where before it ran only while someone was watching.
#
# ffmpeg comes from the go2rtc image (the board has none of its own) and
# runs in a second container, so the go2rtc container is not touched.
#
# Files: $DIR/car_<UTC start time>.mkv, a new one every SEGMENT seconds.
# Matroska because the car is switched off, not shut down: a file cut short
# by a power loss still plays up to the cut, where an MP4 would be lost.
# The data is pushed to disk every few seconds for the same reason.
# The names use the board's clock, which can be wrong until GNSS has a fix.
#
# It looks after itself:
#   - go2rtc not running (no camera)   -> wait, do nothing
#   - go2rtc restarts                  -> ffmpeg exits, start again
#   - stream stalls                    -> ffmpeg gives up after about twice
#     STALL seconds (measured: 30 s for the default 15), start again
#   - disk below MIN_FREE_GB           -> delete the oldest recordings
set -u
DIR=${CAMERA_RECORD_DIR:-/srv/openlaps/video}
SRC=${CAMERA_RECORD_SOURCE:-rtsp://127.0.0.1:8554/car_h264}
GO2RTC=${CAMERA_RECORD_GO2RTC:-openlaps-vehicle-go2rtc}
CTR=${CAMERA_RECORD_CONTAINER:-openlaps-vehicle-record}
SEGMENT=${CAMERA_RECORD_SEGMENT:-300}
MIN_FREE_GB=${CAMERA_RECORD_MIN_FREE_GB:-100}
STALL=${CAMERA_RECORD_STALL:-15}
RETRY_MIN=${CAMERA_RECORD_RETRY_MIN:-5}
RETRY_MAX=${CAMERA_RECORD_RETRY_MAX:-60}

state=""
say() { [ "$state" = "$1" ] || { state=$1; echo "$2"; }; }

mkdir -p "$DIR" || exit 1

# Flush to disk every 5 s, and once a minute make room if the disk is low.
housekeeping() {
	n=0
	while sleep 5; do
		sync -f "$DIR"
		n=$((n + 1)); [ $((n % 12)) -eq 0 ] || continue
		while free=$(df -BG --output=avail "$DIR" | tail -1 | tr -dc 0-9) && [ "$free" -lt "$MIN_FREE_GB" ]; do
			# Never the newest file: that is the one being written.
			old=$(ls "$DIR" | grep '^car_.*\.mkv$' | sort | head -n -1 | head -n 1)
			[ -n "$old" ] || break
			echo "disk has ${free}G free (want ${MIN_FREE_GB}G), deleting $old"
			rm -f "$DIR/$old" || break
		done
	done
}
housekeeping &
keeper=$!
trap 'kill $keeper 2>/dev/null; docker rm -f "$CTR" >/dev/null 2>&1; exit 0' TERM INT

retry=$RETRY_MIN
while :; do
	if [ "$(docker inspect -f '{{.State.Running}}' "$GO2RTC" 2>/dev/null)" != true ]; then
		say waiting "$GO2RTC is not running, waiting"
		sleep 5 & wait $!
		continue
	fi
	image=$(docker inspect -f '{{.Config.Image}}' "$GO2RTC")
	docker rm -f "$CTR" >/dev/null 2>&1
	say recording "recording $SRC to $DIR"
	started=$(date +%s)
	docker run --rm --init --name "$CTR" --network host \
		--user "$(id -u):$(id -g)" -v "$DIR:/rec" --entrypoint ffmpeg "$image" \
		-hide_banner -v error -nostdin \
		-rtsp_transport tcp -timeout $((STALL * 1000000)) -i "$SRC" \
		-map 0 -c copy -f segment -segment_time "$SEGMENT" -segment_atclocktime 1 \
		-reset_timestamps 1 -strftime 1 "/rec/car_%Y%m%dT%H%M%SZ.mkv" &
	wait $!
	ran=$(( $(date +%s) - started ))
	# A run that lasted is a healthy recorder that lost its source; one
	# that died at once is a source that is not there yet, so back off.
	if [ "$ran" -ge 60 ]; then retry=$RETRY_MIN; fi
	state=""
	echo "recorder stopped after ${ran}s, trying again in ${retry}s"
	sleep "$retry" & wait $!
	retry=$((retry * 2)); [ "$retry" -le "$RETRY_MAX" ] || retry=$RETRY_MAX
done
