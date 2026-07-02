#!/usr/bin/env bash
# Start (or restart) a game client in an isolated profile ($CLIENT_PROFILE),
# so it doesn't fight the headless server for ~/.factorio/.lock.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env.sh"

if [ -f "$CLIENT_PID_FILE" ] && kill -0 "$(cat "$CLIENT_PID_FILE")" 2>/dev/null; then
    old_pid="$(cat "$CLIENT_PID_FILE")"
    echo "stopping existing client (pid $old_pid)"
    kill "$old_pid"
    # Wait for it to actually release $CLIENT_PROFILE/.lock -- an in-game
    # client takes several seconds to shut down, and launching the new
    # instance too early loses the race and dies with "Couldn't acquire
    # exclusive lock".
    for _ in $(seq 1 30); do
        kill -0 "$old_pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$old_pid" 2>/dev/null; then
        echo "WARNING: old client (pid $old_pid) still shutting down after 30s" >&2
    fi
fi

mkdir -p "$CLIENT_PROFILE"/{config,saves,script-output,temp}

cat > "$CLIENT_PROFILE/config/config.ini" <<EOF
; version=13
[path]
read-data=$FACTORIO_HOME/data
write-data=$CLIENT_PROFILE

[general]
locale=auto

[other]
EOF

# Lets the standalone binary authenticate against an already-running Steam
# client instead of failing Steamworks init (needed to launch outside Steam's
# own "Play" button, which always uses the default ~/.factorio profile).
echo -n "427520" > "$FACTORIO_HOME/bin/x64/steam_appid.txt"

# Derive the client's mod set from the server-launch snapshot when one exists
# (written by start-server.sh) so it matches the *running* server exactly --
# no manual in-game "sync mods with server" needed. Falls back to the live
# master list if no server has been started from these scripts yet.
python3 "$DEV_DIR/sync_mods.py" "$MASTER_PROFILE" "$CLIENT_PROFILE" "$REPO_DIR/mod" \
    "$DEV_DIR/.server-mod-list.json"

cd "$FACTORIO_HOME/bin/x64"
./factorio --config "$CLIENT_PROFILE/config/config.ini" \
    --mp-connect "localhost:$GAME_PORT" \
    > "$DEV_DIR/logs/client.log" 2>&1 &
disown
echo $! > "$CLIENT_PID_FILE"
echo "client starting, pid $! -- log: $DEV_DIR/logs/client.log"
echo "auto-connecting to: localhost:$GAME_PORT"
