"""Configuration from environment variables."""

import os
from pathlib import Path

# Directory the flma mod writes into — this is normally Factorio's
# script-output/flma under the user's Factorio config dir, e.g.
# ~/.factorio/script-output/flma on Linux. Point this at wherever the local
# game client's script-output lives.
SCRIPT_OUTPUT_DIR: Path = Path(
    os.environ.get(
        "SCRIPT_OUTPUT_DIR",
        str(Path.home() / ".factorio" / "script-output" / "flma"),
    )
)
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()
PORT: int = int(os.environ.get("PORT", "8080"))

# Bind address for the MCP server. Defaults to loopback-only — this bridge
# serves unauthenticated live game state, so it should not be reachable from
# the network unless the operator explicitly opts in (e.g. HOST=0.0.0.0 to
# expose it to other machines on a trusted LAN).
HOST: str = os.environ.get("HOST", "127.0.0.1")

# Minimum seconds between re-reading files from disk per tool call. Snapshot
# files are small and cadence is controlled by the mod's own
# flma-tick-interval setting, so this just avoids re-parsing on every single
# tool call in a tight burst.
MIN_REFRESH_INTERVAL_SECONDS: float = float(os.environ.get("MIN_REFRESH_INTERVAL_SECONDS", "0.5"))
