"""Factorio live game-data MCP server (local bridge for the flma mod).

Reads script-output/flma/* written by the flma Factorio mod (see
apps/factorio-live-mcp/mod/) and serves it over Streamable HTTP as MCP tools.
Runs locally on the machine running the Factorio client — see
apps/factorio-live-mcp/CLAUDE.md for why (the mod writes to the *local*
script-output of whichever peer runs it; a pure client can't pull from a
remote server without RCON).

Tools:
  get_research_status   -- current research, progress, queue
  get_tech_tree          -- researched / available / locked technologies
  get_production_stats   -- item/fluid cumulative totals & per-minute rates
  get_logistics           -- logistic network contents and robot counts
  get_player_inventory    -- a connected player's main inventory
  get_building_counts     -- placed-building counts by name/type
  query_buildings         -- filter placed buildings by name/type/surface/force
  get_snapshot_age        -- staleness (seconds) of each feed, for sanity checking
"""

from __future__ import annotations

import asyncio
import logging

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from .config import HOST, LOG_LEVEL, MIN_REFRESH_INTERVAL_SECONDS, PORT, SCRIPT_OUTPUT_DIR
from .game_state import GameState

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="factorio-live",
    host=HOST,
    port=PORT,
    streamable_http_path="/mcp",
)

state = GameState(SCRIPT_OUTPUT_DIR, min_refresh_interval=MIN_REFRESH_INTERVAL_SECONDS)

MAX_RESULTS = 200


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> PlainTextResponse:
    ok = await asyncio.to_thread(state.health_check)
    if ok:
        return PlainTextResponse("ok")
    return PlainTextResponse(f"script-output dir not found: {state.dir}", status_code=503)


def _no_data_hint() -> dict:
    return {
        "error": "no data yet",
        "hint": (
            "Enable the flma mod in-game: set the map setting "
            "'flma-export-enabled' to true (Mod settings -> Map), or run "
            "/c settings.global['flma-export-enabled'] = {value=true}. "
            f"Expecting files under {state.dir}."
        ),
    }


@mcp.tool()
async def get_research_status(force: str = "player") -> dict:
    """Get the current research target, progress, and queue for a force.

    Prefers research.json (refreshed every flma-tick-interval cycle, so
    research_progress stays live) over tech.json's copy of the same fields
    (only refreshed on research started/finished/queued/cancelled/reversed
    events, plus whichever fields research.json's snapshot doesn't carry —
    currently none, since tech.json's version is a superset for these three
    fields). Falls back to tech.json if research.json is empty (e.g. an
    older mod build that predates it).

    Args:
        force: Force name (default 'player', the usual single-player/co-op force).
    """
    research_data = await asyncio.to_thread(state.get_research)
    tech_data = await asyncio.to_thread(state.get_tech)
    if not research_data and not tech_data:
        return _no_data_hint()

    research_forces: dict = research_data.get("forces") or {}
    tech_forces: dict = tech_data.get("forces") or {}
    research_force_data = research_forces.get(force)
    tech_force_data = tech_forces.get(force)
    if research_force_data is None and tech_force_data is None:
        available = sorted(set(research_forces) | set(tech_forces))
        return {"error": f"no snapshot for force '{force}'", "available_forces": available}

    research_force_data = research_force_data or {}
    tech_force_data = tech_force_data or {}
    return {
        "tick": research_data.get("tick") if research_force_data else tech_data.get("tick"),
        "force": force,
        "current_research": research_force_data.get("current_research")
        if research_force_data
        else tech_force_data.get("current_research"),
        "research_progress": research_force_data.get("research_progress")
        if research_force_data
        else tech_force_data.get("research_progress"),
        "research_queue": (
            research_force_data.get("research_queue") if research_force_data else None
        )
        or tech_force_data.get("research_queue")
        or [],
    }


@mcp.tool()
async def get_tech_tree(force: str = "player", status: str = "all") -> dict:
    """Get the technology tree: which technologies are researched, available
    to research now (prerequisites met), or still locked.

    Args:
        force: Force name (default 'player').
        status: One of 'all', 'researched', 'available', 'locked' (default 'all').
    """
    data = await asyncio.to_thread(state.get_tech)
    if not data:
        return _no_data_hint()
    forces: dict = data.get("forces") or {}
    force_data = forces.get(force)
    if force_data is None:
        return {"error": f"no snapshot for force '{force}'", "available_forces": list(forces)}

    technologies: dict = force_data.get("technologies") or {}

    def classify(name: str, tech: dict) -> str:
        if tech.get("researched"):
            return "researched"
        if not tech.get("enabled"):
            return "locked"
        prereqs = tech.get("prerequisites") or []
        all_met = all(technologies.get(p, {}).get("researched") for p in prereqs)
        return "available" if all_met else "locked"

    results = []
    for name, tech in technologies.items():
        tech_status = classify(name, tech)
        if status != "all" and tech_status != status:
            continue
        results.append(
            {
                "name": name,
                "status": tech_status,
                "level": tech.get("level"),
                "prerequisites": tech.get("prerequisites") or [],
            }
        )

    return {
        "tick": data.get("tick"),
        "force": force,
        "status_filter": status,
        "count": len(results),
        "technologies": sorted(results, key=lambda t: t["name"]),
    }


@mcp.tool()
async def get_production_stats(
    force: str = "player",
    surface: str | None = None,
    kind: str = "both",
) -> dict:
    """Get production statistics for items and/or fluids on a surface.

    Each returned `items`/`fluids` block has two different kinds of numbers —
    don't confuse them:
      - `input_counts`/`output_counts`: CUMULATIVE totals since the game (or
        force) began, not rates. `input_counts` is what the force has ever
        *produced*; `output_counts` is what it has ever *consumed* (matches
        the left/right split in the in-game production statistics GUI).
      - `input_rates_per_min`/`output_rates_per_min`: real per-minute flow
        rates (roughly the last 60s), suitable for "how much am I making
        right now" — use these, not the cumulative counts, for anything
        rate-shaped.

    Values are aggregated by the game engine (not scanned), so this is cheap
    to call frequently.

    Args:
        force: Force name (default 'player').
        surface: Surface name (default: 'nauvis' if present, else the first
            surface in the snapshot).
        kind: One of 'items', 'fluids', 'both' (default 'both').
    """
    data = await asyncio.to_thread(state.get_production)
    if not data:
        return _no_data_hint()
    forces: dict = data.get("forces") or {}
    force_data = forces.get(force)
    if force_data is None:
        return {"error": f"no snapshot for force '{force}'", "available_forces": list(forces)}

    surfaces: dict = force_data.get("surfaces") or {}
    surface_name = surface or ("nauvis" if "nauvis" in surfaces else next(iter(surfaces), None))
    if surface_name is None or surface_name not in surfaces:
        return {
            "error": f"no snapshot for surface '{surface_name}'",
            "available_surfaces": list(surfaces),
        }

    surface_data = surfaces[surface_name]
    out: dict = {"tick": data.get("tick"), "force": force, "surface": surface_name}
    if kind in ("items", "both"):
        out["items"] = surface_data.get("items")
    if kind in ("fluids", "both"):
        out["fluids"] = surface_data.get("fluids")
    return out


@mcp.tool()
async def get_logistics(force: str = "player", surface: str | None = None) -> dict:
    """Get logistic network contents and robot counts.

    Args:
        force: Force name (default 'player').
        surface: Optional surface name to filter to a single surface/planet.
    """
    data = await asyncio.to_thread(state.get_logistics)
    if not data:
        return _no_data_hint()
    forces: dict = data.get("forces") or {}
    networks = forces.get(force)
    if networks is None:
        return {"error": f"no snapshot for force '{force}'", "available_forces": list(forces)}

    if surface is not None:
        networks = [n for n in networks if n.get("surface") == surface]

    return {
        "tick": data.get("tick"),
        "force": force,
        "network_count": len(networks),
        "networks": networks,
    }


@mcp.tool()
async def get_player_inventory(player: str | None = None) -> dict:
    """Get a connected player's main inventory contents.

    Requires the flma-export-inventories map setting to be enabled (off by
    default — player inventory contents are more sensitive than aggregate
    stats).

    Args:
        player: Player name. If omitted and exactly one player is connected,
            that player's inventory is returned.
    """
    data = await asyncio.to_thread(state.get_inventories)
    if not data:
        return _no_data_hint()
    players: dict = data.get("players") or {}
    if not players:
        return {
            "error": "no player inventory data",
            "hint": "Enable the 'flma-export-inventories' map setting.",
        }
    if player is None:
        if len(players) == 1:
            player = next(iter(players))
        else:
            return {"error": "player name required", "connected_players": list(players)}
    if player not in players:
        return {"error": f"player '{player}' not found", "connected_players": list(players)}
    return {"tick": data.get("tick"), "player": player, **players[player]}


@mcp.tool()
async def get_building_counts(force: str | None = None) -> dict:
    """Get placed-building counts grouped by name and by type.

    Requires the flma-export-buildings map setting to be enabled.

    Args:
        force: Optional force name to filter to (e.g. 'player').
    """
    all_buildings = await asyncio.to_thread(state.get_buildings)
    if not all_buildings:
        return {
            "total": 0,
            "hint": "No building data. Enable the 'flma-export-buildings' map setting.",
        }
    buildings = all_buildings
    if force is not None:
        buildings = [b for b in buildings if b.get("force") == force]
        if not buildings:
            # Data exists, just not for this force -- distinct from "the mod
            # isn't exporting buildings at all", so no setting-enable hint.
            return {
                "total": 0,
                "available_forces": sorted(
                    {b.get("force") for b in all_buildings if b.get("force")}
                ),
            }
    by_name: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for b in buildings:
        by_name[b["name"]] = by_name.get(b["name"], 0) + 1
        by_type[b["type"]] = by_type.get(b["type"], 0) + 1
    return {
        "total": len(buildings),
        "by_name": dict(sorted(by_name.items(), key=lambda kv: -kv[1])),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
    }


@mcp.tool()
async def query_buildings(
    name: str | None = None,
    type: str | None = None,
    surface: str | None = None,
    force: str | None = None,
    limit: int = 100,
) -> dict:
    """Query placed buildings by name/type/surface/force, with positions.

    Requires the flma-export-buildings map setting to be enabled.

    Args:
        name: Exact entity prototype name (e.g. 'assembling-machine-2').
        type: Exact entity type (e.g. 'assembling-machine', 'inserter').
        surface: Surface name (e.g. 'nauvis').
        force: Force name (e.g. 'player').
        limit: Max results to return (1-200, default 100).
    """
    limit = max(1, min(limit, MAX_RESULTS))
    buildings = await asyncio.to_thread(state.get_buildings)
    if not buildings:
        return {
            "results": [],
            "total_matches": 0,
            "hint": "No building data. Enable the 'flma-export-buildings' map setting.",
        }

    def matches(b: dict) -> bool:
        return (
            (name is None or b.get("name") == name)
            and (type is None or b.get("type") == type)
            and (surface is None or b.get("surface") == surface)
            and (force is None or b.get("force") == force)
        )

    results = [b for b in buildings if matches(b)]
    return {
        "results": results[:limit],
        "total_matches": len(results),
        "truncated": len(results) > limit,
    }


@mcp.tool()
async def get_snapshot_age() -> dict:
    """Get how many seconds old each data feed is — use this to sanity-check
    that the mod is actually running and exporting (e.g. before trusting a
    'no research in progress' answer, confirm the tech feed is fresh)."""
    ages = await asyncio.to_thread(state.snapshot_ages)
    return {"script_output_dir": str(state.dir), "age_seconds": ages}


if __name__ == "__main__":
    logger.info("Starting factorio-live MCP server on port %d (reading %s)…", PORT, state.dir)
    mcp.run(transport="streamable-http")
