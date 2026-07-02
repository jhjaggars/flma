# Shared config for the dev/*.sh scripts. Source, don't execute.
FACTORIO_HOME="${FACTORIO_HOME:-/home/jhjaggars/.local/share/Steam/steamapps/common/Factorio}"
FACTORIO_BIN="$FACTORIO_HOME/bin/x64/factorio"
MASTER_PROFILE="${MASTER_PROFILE:-/home/jhjaggars/.factorio}"
CLIENT_PROFILE="${CLIENT_PROFILE:-/home/jhjaggars/.factorio-client}"
RCON_HOST=127.0.0.1
RCON_PORT="${RCON_PORT:-27015}"
GAME_PORT="${GAME_PORT:-34197}"
DEV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$DEV_DIR")"
# The save the dev server serves. Defaults to a DEV-OWNED COPY under dev/saves
# rather than a real save in $MASTER_PROFILE/saves: the server exit-saves back
# into whatever file it loaded, so serving a real save mutates it (a real
# _autosave got overwritten this way once). Create it by copying whichever
# save you want to develop against:
#   mkdir -p dev/saves && cp ~/.factorio/saves/<save>.zip dev/saves/flma-dev.zip
SAVE="${FLMA_DEV_SAVE:-$DEV_DIR/saves/flma-dev.zip}"
RCON_PASSWORD_FILE="$DEV_DIR/.rcon-password"
SERVER_PID_FILE="$DEV_DIR/.server.pid"
CLIENT_PID_FILE="$DEV_DIR/.client.pid"

mkdir -p "$DEV_DIR/logs" "$DEV_DIR/saves"

if [ ! -f "$RCON_PASSWORD_FILE" ]; then
    openssl rand -hex 16 > "$RCON_PASSWORD_FILE"
fi
RCON_PASSWORD="$(cat "$RCON_PASSWORD_FILE")"

rcon() {
    python3 "$DEV_DIR/rcon.py" "$RCON_HOST" "$RCON_PORT" "$RCON_PASSWORD" "/silent-command $1"
}
