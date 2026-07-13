"""Configuration from environment variables for the factory-planner CLI."""

from __future__ import annotations

import os
from pathlib import Path

from src.config import SCRIPT_OUTPUT_DIR  # flma's own live-snapshot directory

# Path to the recipes.db built by `planner build-db` (planner/recipedb/build_db.py,
# vendored from recipe-mcp) from the flma mod's own recipes.json export. Not
# committed to the repo — build it locally before using the planner
# (`uv run python -m planner build-db`, or `make build-db`).
#
# Deliberately a flat default next to SCRIPT_OUTPUT_DIR rather than nested
# under the live save's <save_id> subdirectory the way recipes.json itself is
# (see src/game_state.py) — one recipes.db, explicitly rebuilt when the
# modpack/save changes, with `planner status`'s alignment check catching a
# stale one rather than silently auto-tracking per-save.
RECIPES_DB: Path = Path(os.environ.get("RECIPES_DB", str(SCRIPT_OUTPUT_DIR / "recipes.db")))

__all__ = ["SCRIPT_OUTPUT_DIR", "RECIPES_DB"]
