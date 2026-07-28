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

# --- /metrics (Prometheus exporter, see flma_mcp/metrics.py) --------------

# Forces to emit game-data series for. production.json/tech.json/etc. cover
# every force in the game, including `enemy`/`neutral` -- always present but
# empty in every save inspected so far (see SCHEMA.md) -- so emitting all of
# them would triple every count-style series for no information. Comma-
# separated.
METRICS_FORCES: tuple[str, ...] = tuple(
    f.strip() for f in os.environ.get("FLMA_MCP_METRICS_FORCES", "player").split(",") if f.strip()
)

# Per-item/per-fluid production series are capped at the top N by current
# throughput (produced rate + consumed rate, summed across surfaces),
# applied independently to items and to fluids -- a Pyanodons save can have
# 400+ distinct item ids, only a fraction of them ever nonzero, so uncapped
# is thousands of mostly-idle series. 0 disables per-item production
# entirely (METRICS_ITEM_ALLOWLIST is still honored); -1 emits everything.
METRICS_TOP_ITEMS: int = int(os.environ.get("FLMA_MCP_METRICS_TOP_ITEMS", "40"))

# Item/fluid ids ALWAYS emitted regardless of rank, rendered as an explicit
# 0 when absent from the snapshot so the series never gaps. Anything an
# alert or recording rule references belongs here -- top-N membership is not
# stable enough to alert on (see flma_mcp/CLAUDE.md's cardinality section).
METRICS_ITEM_ALLOWLIST: frozenset[str] = frozenset(
    n.strip() for n in os.environ.get("FLMA_MCP_METRICS_ITEM_ALLOWLIST", "").split(",") if n.strip()
)

# Hysteresis against top-N flapping: an id already being emitted is retained
# while it stays within the top N*(1+slack). Without this, ids clustered
# near the cutoff enter and leave every scrape, producing gappy series for
# no analytical reason. 0 disables retention entirely.
METRICS_TOP_ITEMS_SLACK: float = float(os.environ.get("FLMA_MCP_METRICS_TOP_ITEMS_SLACK", "0.5"))

# Building counts scan the whole in-memory building index (can be well over
# 100k entities on a mature save). Set to 0 as a kill switch if that ever
# shows up in flma_scrape_duration_seconds.
METRICS_BUILDINGS: bool = os.environ.get("FLMA_MCP_METRICS_BUILDINGS", "1") != "0"

# Cap on flma_buildings_by_name's cardinality (distinct placed-entity names
# -- usually well under a few hundred, but a modpack with many entity
# prototypes might not stay that way). -1 = unlimited.
METRICS_TOP_BUILDING_NAMES: int = int(os.environ.get("FLMA_MCP_METRICS_TOP_BUILDING_NAMES", "-1"))

__all__ = [
    "HOST",
    "PORT",
    "TOKEN",
    "STALE_AFTER_SECONDS",
    "LOG_LEVEL",
    "SCRIPT_OUTPUT_DIR",
    "RECIPES_DB",
    "METRICS_FORCES",
    "METRICS_TOP_ITEMS",
    "METRICS_ITEM_ALLOWLIST",
    "METRICS_TOP_ITEMS_SLACK",
    "METRICS_BUILDINGS",
    "METRICS_TOP_BUILDING_NAMES",
]
