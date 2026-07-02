#!/usr/bin/env bash
# Stop the dev server and/or client. Usage: stop.sh [server|client] (default: both)
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env.sh"

stop_one() {
    local name="$1" pidfile="$2"
    if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        echo "stopping $name (pid $(cat "$pidfile"))"
        kill "$(cat "$pidfile")"
    else
        echo "$name not running"
    fi
    rm -f "$pidfile"
}

case "${1:-both}" in
    server) stop_one server "$SERVER_PID_FILE" ;;
    client) stop_one client "$CLIENT_PID_FILE" ;;
    both)
        stop_one client "$CLIENT_PID_FILE"
        stop_one server "$SERVER_PID_FILE"
        ;;
    *) echo "usage: $0 [server|client|both]"; exit 1 ;;
esac
