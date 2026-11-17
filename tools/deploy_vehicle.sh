#!/usr/bin/env bash
# Push the committed tree (HEAD) to a vehicle SBC that runs the agent natively
# from /opt/openlaps as a systemd unit, then restart the agent.
#
#     tools/deploy_vehicle.sh                      # luckfox, the default
#     tools/deploy_vehicle.sh user@host            # another board
#
# What it does, in order:
#   1. `git archive HEAD` over ssh into $DEST as the service user. Only
#      committed files travel, so commit a trackside fix first. Files deleted
#      from the repo are *not* removed from the board.
#   2. Writes $DEST/.deployed-rev (git describe, UTC time, source host) so the
#      board can say which revision it is running -- it has no .git.
#   3. Runs `uv sync --frozen` on the board if pyproject.toml or uv.lock
#      changed. That downloads packages, so do it before leaving for the track.
#   4. Restarts openlaps-agent and prints its status and recent journal, and
#      restarts openlaps-can-watchdog if it is running (it is tools/ code too).
#   5. Restarts the login user's own units whose script lives under $DEST and
#      changed in this deploy (camera-watch, camera-record on the luckfox).
#      Restarting camera-record leaves a few seconds' gap in the video.
#
# Not restarted: openlaps-can (re-run it only when the profile's buses or
# bitrates change), the nats container (deploy/nats/vehicle.conf) and go2rtc
# (the target's go2rtc.yaml) -- `docker restart openlaps-vehicle-{nats,go2rtc}`
# by hand when those files are what changed.
set -euo pipefail

HOST="${1:-${OPENLAPS_VEHICLE_HOST:-luckfox@192.168.12.222}}"
DEST="${OPENLAPS_VEHICLE_DIR:-/opt/openlaps}"
SVC_USER="${OPENLAPS_VEHICLE_USER:-openlaps}"
UNIT=openlaps-agent

cd "$(git rev-parse --show-toplevel)"

rev="$(git describe --always --tags --dirty)"
if [[ "$rev" == *-dirty ]]; then
    echo "note: working tree has uncommitted changes; deploying HEAD ($rev) only" >&2
fi

echo "==> $HOST: deploying $rev to $DEST"
before="$(ssh "$HOST" "cat $DEST/pyproject.toml $DEST/uv.lock 2>/dev/null | md5sum")"

# The login user's units that run a script straight out of $DEST, as
# "<md5>  <script>" lines, so a changed script can be told from an unchanged one.
user_scripts() {
    ssh "$HOST" "grep -hs '^ExecStart=$DEST/' .config/systemd/user/*.service | cut -d= -f2 | cut -d' ' -f1 | sort -u | xargs -r md5sum 2>/dev/null" || true
}
scripts_before="$(user_scripts)"

git archive --format=tar HEAD | ssh "$HOST" "sudo -u $SVC_USER tar xf - -C $DEST"

stamp="$rev $(date -u +%Y-%m-%dT%H:%M:%SZ) from $(hostname)"
ssh "$HOST" "printf '%s\n' '$stamp' | sudo -u $SVC_USER tee $DEST/.deployed-rev >/dev/null"

after="$(ssh "$HOST" "cat $DEST/pyproject.toml $DEST/uv.lock | md5sum")"
if [[ "$before" != "$after" ]]; then
    echo "==> dependencies changed: uv sync --frozen (needs internet)"
    ssh "$HOST" "cd $DEST && sudo -u $SVC_USER /usr/local/bin/uv sync --frozen"
fi

echo "==> restarting $UNIT"
ssh "$HOST" "sudo systemctl restart $UNIT && sleep 3 && systemctl is-active $UNIT && journalctl -u $UNIT -n 15 --no-pager"
ssh "$HOST" "sudo systemctl try-restart openlaps-can-watchdog 2>/dev/null || true"
changed="$(comm -13 <(sort <<<"$scripts_before") <(user_scripts | sort) | awk '{print $2}')"
ssh "$HOST" "systemctl --user daemon-reload" || true
for script in $changed; do
    for unit in $(ssh "$HOST" "grep -ls '^ExecStart=$script' .config/systemd/user/*.service"); do
        echo "==> $script changed: restarting user unit $(basename "$unit")"
        ssh "$HOST" "systemctl --user try-restart $(basename "$unit")"
    done
done

echo "==> done: $(ssh "$HOST" "cat $DEST/.deployed-rev")"
