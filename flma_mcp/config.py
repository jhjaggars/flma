"""Configuration for the flma MCP server, from environment variables.

Re-exports `src.config.SCRIPT_OUTPUT_DIR` and `planner.config.RECIPES_DB`
rather than duplicating them — both stay the single source of truth for
where live game state and the recipe DB live; this module only adds the
settings specific to running as a server (bind address, auth token,
staleness threshold).
"""

from __future__ import annotations

import os

from planner.config import RECIPES_DB
from src.config import SCRIPT_OUTPUT_DIR

# Bind address. Deliberately NOT "0.0.0.0" by default -- see flma_mcp/CLAUDE.md
# and the homelab repo's networkpolicy.yaml comment: this host is also on a
# Tailscale tailnet with third-party nodes on it, and 0.0.0.0 would bind that
# interface too. Set explicitly to the LAN interface's address in production.
HOST: str = os.environ.get("FLMA_MCP_HOST", "127.0.0.1")

PORT: int = int(os.environ.get("FLMA_MCP_PORT", "9110"))

# Bearer token required on every route except /healthz. If unset, the server
# logs a loud warning and runs with no auth at all -- convenient for local
# dev, never silent (see server.py's BearerTokenMiddleware).
TOKEN: str | None = os.environ.get("FLMA_MCP_TOKEN") or None

# A tool result's `freshness.stale` flips true once the freshest live
# snapshot is older than this -- about 6x the mod's own ~5s export cadence
# (flma-tick-interval default 300 ticks), generous enough that normal
# scheduling jitter never falsely reports staleness.
STALE_AFTER_SECONDS: float = float(os.environ.get("FLMA_MCP_STALE_AFTER_SECONDS", "30"))

LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

__all__ = [
    "HOST",
    "PORT",
    "TOKEN",
    "STALE_AFTER_SECONDS",
    "LOG_LEVEL",
    "SCRIPT_OUTPUT_DIR",
    "RECIPES_DB",
]
