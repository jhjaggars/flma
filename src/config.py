"""Configuration from environment variables."""

import os
from pathlib import Path

# Directory the flma mod writes into — this is normally Factorio's
# script-output/flma under the user's Factorio config dir, e.g.
# ~/.factorio/script-output/flma on Linux. Point this at wherever the local
# game client's script-output lives.
#
# Since mod 0.3.1 the actual data files live one level deeper, under a
# per-save <save_id> subdirectory (see SCHEMA.md) — this should still point at
# the *parent* flma/ directory. GameState resolves the active <save_id> itself
# via the current-save.json pointer the mod maintains there, and re-checks it
# on every refresh, so switching which save/server is running doesn't require
# reconfiguring this.
SCRIPT_OUTPUT_DIR: Path = Path(
    os.environ.get(
        "SCRIPT_OUTPUT_DIR",
        str(Path.home() / ".factorio" / "script-output" / "flma"),
    )
)
