"""Configuration from environment variables for the factory-planner CLI."""

from __future__ import annotations

import os
from pathlib import Path

from src.config import SCRIPT_OUTPUT_DIR  # flma's own live-snapshot directory

# The homelab checkout containing recipe-mcp. Used to (a) locate its built
# recipes.db and (b) import its engine.py calculation logic directly (see
# planner/_recipe_mcp_loader.py). This tool is single-machine, single-user
# local tooling, so it references that checkout in place rather than
# vendoring a copy of the recipe data or the engine code — see CLAUDE.md's
# factory-planner section for the reasoning.
RECIPE_MCP_DIR: Path = Path(
    os.environ.get(
        "RECIPE_MCP_DIR",
        str(Path.home() / "code" / "homelab" / "apps" / "recipe-mcp"),
    )
)

# Path to the recipes.db built by recipe-mcp's `make build-db`
# (`python -m src.build_db recipes.json recipes.db`, run inside that repo).
# Not committed to either repo — build it locally before using the planner.
RECIPES_DB: Path = Path(os.environ.get("RECIPES_DB", str(RECIPE_MCP_DIR / "recipes.db")))

__all__ = ["SCRIPT_OUTPUT_DIR", "RECIPE_MCP_DIR", "RECIPES_DB"]
