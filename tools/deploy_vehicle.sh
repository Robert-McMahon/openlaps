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
#   3b. Builds the openlaps_canfast wheel for the board's architecture
#      (tools/build_canfast.sh) and installs it into the board's environment.
#      Without a Rust toolchain on this machine that step is skipped with a
#      note and the agent runs its Python CAN ingest path.
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

# The compiled CAN ingest path is a wheel built here and installed over the
# board's environment (a `uv sync` removes it, so it goes on after one). No
# toolchain here just means the agent keeps its Python ingest path.
arch="$(ssh "$HOST" uname -m)"
if wheel="$(tools/build_canfast.sh "$arch" 2>/tmp/openlaps-canfast-build.log)"; then
    echo "==> installing $(basename "$wheel")"
    scp -q "$wheel" "$HOST:/tmp/"
    ssh "$HOST" "cd $DEST && sudo -u $SVC_USER /usr/local/bin/uv pip install -q --python .venv/bin/python --force-reinstall --no-deps /tmp/$(basename "$wheel")"
else
    echo "note: openlaps_canfast was not built (see /tmp/openlaps-canfast-build.log); the agent will use its Python CAN ingest" >&2
fi

# The link tunnel is a standalone binary built here. It is only restarted on
# a board that has its unit installed (deploy/systemd/openlaps-link.service).
if link="$(tools/build_link.sh "$arch" 2>/tmp/openlaps-link-build.log)"; then
    echo "==> installing openlaps-link"
    scp -q "$link" "$HOST:/tmp/openlaps-link"
    ssh "$HOST" "sudo -u $SVC_USER install -D -m 0755 /tmp/openlaps-link $DEST/bin/openlaps-link.new && sudo -u $SVC_USER mv $DEST/bin/openlaps-link.new $DEST/bin/openlaps-link && rm -f /tmp/openlaps-link && (sudo systemctl try-restart openlaps-link 2>/dev/null || true)"
else
    echo "note: openlaps-link was not built (see /tmp/openlaps-link-build.log); the tunnel binary on the board is unchanged" >&2
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
