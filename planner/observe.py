"""Live-observe helpers backing the `research`/`tech-tree`/`production`/
`logistics`/`inventory`/`buildings` planner subcommands.

Pure functions over a `GameState` (src/game_state.py), each shaping one
snapshot file into the dict `planner/cli.py` prints (as `--json` or rendered
text). Ported from the former `src/server.py` MCP tools — same shaping logic
(force/surface fallback, tech classification, no-data hints), just without
the MCP/asyncio wrapping: a CLI invocation is a one-shot sync read.
"""

from __future__ import annotations

from typing import Any

from src.game_state import GameState

MAX_BUILDING_RESULTS = 200


def _no_data_hint(gs: GameState) -> dict[str, Any]:
    return {
        "error": "no data yet",
        "hint": (
            "Enable the flma mod in-game: set the map setting "
            "'flma-export-enabled' to true (Mod settings -> Map), or run "
            "/c settings.global['flma-export-enabled'] = {value=true}. "
            f"Expecting files under {gs.dir}."
        ),
    }


def research_status(gs: GameState, force: str = "player") -> dict[str, Any]:
    """Current research target, progress, and queue for `force`.

    Prefers research.json (refreshed every flma-tick-interval cycle, so
    research_progress stays live) over tech.json's copy of the same fields
    (only refreshed on research started/finished/queued/cancelled/reversed
    events). Falls back to tech.json if research.json is empty (e.g. an
    older mod build that predates it).
    """
    research_data = gs.get_research()
    tech_data = gs.get_tech()
    if not research_data and not tech_data:
        return _no_data_hint(gs)

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


def tech_tree(gs: GameState, force: str = "player", status: str = "all") -> dict[str, Any]:
    """Technologies for `force`: researched, available (prerequisites met),
    or still locked. `status` filters to one of those, or 'all'."""
    data = gs.get_tech()
    if not data:
        return _no_data_hint(gs)
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


def production_stats(
    gs: GameState,
    force: str = "player",
    surface: str | None = None,
    kind: str = "both",
) -> dict[str, Any]:
    """Item/fluid production for `force` on `surface`.

    Each `items`/`fluids` block has two different kinds of numbers:
    `input_counts`/`output_counts` are CUMULATIVE totals since the game (or
    force) began (produced/consumed, matching the in-game GUI's left/right
    split); `input_rates_per_min`/`output_rates_per_min` are real per-minute
    flow rates — use those, not the cumulative counts, for "how much am I
    making right now".
    """
    data = gs.get_production()
    if not data:
        return _no_data_hint(gs)
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
    out: dict[str, Any] = {"tick": data.get("tick"), "force": force, "surface": surface_name}
    if kind in ("items", "both"):
        out["items"] = surface_data.get("items")
    if kind in ("fluids", "both"):
        out["fluids"] = surface_data.get("fluids")
    return out


def logistics(gs: GameState, force: str = "player", surface: str | None = None) -> dict[str, Any]:
    """Logistic network contents and robot counts for `force`, optionally
    filtered to one surface/planet."""
    data = gs.get_logistics()
    if not data:
        return _no_data_hint(gs)
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


def player_inventory(gs: GameState, player: str | None = None) -> dict[str, Any]:
    """A connected player's main inventory contents.

    Requires the flma-export-inventories map setting (off by default —
    player inventory contents are more sensitive than aggregate stats).
    If `player` is omitted and exactly one player is connected, that
    player's inventory is returned.
    """
    data = gs.get_inventories()
    if not data:
        return _no_data_hint(gs)
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


def building_counts(gs: GameState, force: str | None = None) -> dict[str, Any]:
    """Placed-building counts grouped by name and by type, optionally
    filtered to one force. Requires the flma-export-buildings map setting."""
    all_buildings = gs.get_buildings()
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


def query_buildings(
    gs: GameState,
    name: str | None = None,
    type: str | None = None,
    surface: str | None = None,
    force: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Placed buildings filtered by exact name/type/surface/force, with
    positions. Requires the flma-export-buildings map setting."""
    limit = max(1, min(limit, MAX_BUILDING_RESULTS))
    buildings = gs.get_buildings()
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
