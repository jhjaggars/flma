#!/usr/bin/env bash
# Quick health check: are server/client running, and is RCON responsive?
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env.sh"

check() {
    local name="$1" pidfile="$2"
    if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        echo "$name: running (pid $(cat "$pidfile"))"
    else
        echo "$name: not running"
    fi
}

check server "$SERVER_PID_FILE"
check client "$CLIENT_PID_FILE"

if tick=$(rcon "rcon.print(game.tick)" 2>/dev/null); then
    echo "rcon: alive, tick=$tick"
else
    echo "rcon: unreachable"
fi
