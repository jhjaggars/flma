#!/usr/bin/env bash
# Start (or restart) the local headless dev server, with RCON enabled.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env.sh"

if [ ! -f "$SAVE" ]; then
    echo "error: dev save not found: $SAVE" >&2
    echo "seed it with a copy of whichever save you want to develop against:" >&2
    echo "  cp ~/.factorio/saves/<save>.zip $DEV_DIR/saves/flma-dev.zip" >&2
    echo "(or set FLMA_DEV_SAVE=/path/to/save.zip)" >&2
    exit 1
fi

if [ -f "$SERVER_PID_FILE" ] && kill -0 "$(cat "$SERVER_PID_FILE")" 2>/dev/null; then
    old_pid="$(cat "$SERVER_PID_FILE")"
    echo "stopping existing server (pid $old_pid)"
    kill "$old_pid"
    # Wait for it to actually exit: a gracefully-stopping server saves the map
    # AND rewrites mod-list.json on the way out. Syncing/snapshotting while
    # that's still pending races against it (a fixed sleep 2 lost that race).
    for _ in $(seq 1 30); do
        kill -0 "$old_pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$old_pid" 2>/dev/null; then
        echo "WARNING: old server (pid $old_pid) still shutting down after 30s" >&2
    fi
fi

# Align the master profile's mod set with the save being served. The save
# records exactly which mods+versions it was created with; loading it under a
# different set silently migrates the world (that's how a Space Age save got
# pyanodons-contaminated here once). Factorio does this natively: --sync-mods
# rewrites mod-list.json (enable/disable + version pins) to match the save,
# using locally-installed mods. Must run while no instance holds the profile
# lock, i.e. after the wait-for-exit above.
echo "syncing mod list to save: $SAVE"
if "$FACTORIO_BIN" --sync-mods "$SAVE" --mod-directory "$MASTER_PROFILE/mods" \
    > "$DEV_DIR/logs/sync-mods.log" 2>&1; then
    grep -iE "enabled|disabled|missing|not found|version" "$DEV_DIR/logs/sync-mods.log" \
        | grep -iv "Loading mod" | sed 's/^/  sync-mods: /' | head -20 || true
else
    echo "WARNING: factorio --sync-mods failed; continuing with the current" \
         "mod-list (see $DEV_DIR/logs/sync-mods.log)" >&2
fi

# sync_mods.py runs AFTER --sync-mods on purpose: the save probably doesn't
# have flma in its mod set yet (it's the mod under development), so the sync
# above disables it -- this re-enables it and repairs the repo symlink.
python3 "$DEV_DIR/sync_mods.py" "$MASTER_PROFILE" "$CLIENT_PROFILE" "$REPO_DIR/mod"

# Snapshot the exact mod list this server launch will read. start-client.sh
# derives the client's mod set from this snapshot (not the live master list,
# which the server rewrites on exit and GUI edits mutate) so the client always
# matches the *running* server without a manual in-game "sync mods" round-trip.
cp "$MASTER_PROFILE/mods/mod-list.json" "$DEV_DIR/.server-mod-list.json"
# Startup mod settings must match the server too (mismatches trigger the
# client's own sync-and-restart prompt), so snapshot them at launch as well.
cp "$MASTER_PROFILE/mods/mod-settings.dat" "$DEV_DIR/.server-mod-settings.dat"

cd "$FACTORIO_HOME/bin/x64"
./factorio --start-server "$SAVE" \
    --server-settings "$DEV_DIR/server-settings.json" \
    --rcon-bind "$RCON_HOST:$RCON_PORT" \
    --rcon-password "$RCON_PASSWORD" \
    --port "$GAME_PORT" \
    > "$DEV_DIR/logs/server.log" 2>&1 &
disown
echo $! > "$SERVER_PID_FILE"
echo "server starting, pid $! -- log: $DEV_DIR/logs/server.log"
echo "rcon: 127.0.0.1:$RCON_PORT (password in $RCON_PASSWORD_FILE)"
