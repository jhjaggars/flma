"""MCP tools wrapping `planner/observe.py`'s seven live-observe functions --
the same shaping logic the CLI's `research`/`tech-tree`/`production`/
`logistics`/`inventory`/`buildings` subcommands use, and before that,
`src/server.py`'s deleted MCP tools of the same shapes. Nothing here
reimplements that logic; each tool is `state.warm()` + one `observe.*` call
off the event loop + a `freshness` envelope.

Every tool description below opens with "LIVE" for the reason explained in
flma_mcp/CLAUDE.md: Hermes also has the (currently still-registered)
`recipes` MCP server, and the two can answer overlapping questions from
different-quality data. Making the distinction legible in the tool
description is the cheapest disambiguation that actually reaches the model.
"""

from __future__ import annotations

import asyncio
from typing import Any

from planner import observe

from flma_mcp import state


async def _observe(fn, /, **kwargs) -> dict[str, Any]:
    await state.warm()
    result = await asyncio.to_thread(fn, state.gs(), **kwargs)
    return {**result, "freshness": state.freshness()}


def register(mcp) -> None:
    @mcp.tool()
    async def flma_research(force: str = "player") -> dict[str, Any]:
        """LIVE — current research target, progress, and queue for `force`,
        reflecting the currently running Factorio save. Falls back to a
        `freshness.stale=true` result (not an error) when the game is not
        running or the mod's export is off."""
        return await _observe(observe.research_status, force=force)

    @mcp.tool()
    async def flma_tech_tree(force: str = "player", status: str = "all") -> dict[str, Any]:
        """LIVE — technologies for `force`, classified researched/available
        (prerequisites met)/locked. `status` filters to one of
        researched/available/locked/all (default all)."""
        return await _observe(observe.tech_tree, force=force, status=status)

    @mcp.tool()
    async def flma_production(
        force: str = "player", surface: str | None = None, kind: str = "both"
    ) -> dict[str, Any]:
        """LIVE — item/fluid production for `force` on `surface` (default:
        nauvis, else the first surface present). `kind` is items/fluids/both.
        Includes both cumulative totals and real per-minute rates -- prefer
        the rates for "how much am I making right now"."""
        return await _observe(observe.production_stats, force=force, surface=surface, kind=kind)

    @mcp.tool()
    async def flma_logistics(force: str = "player", surface: str | None = None) -> dict[str, Any]:
        """LIVE — logistic network contents and robot counts for `force`,
        optionally filtered to one surface/planet."""
        return await _observe(observe.logistics, force=force, surface=surface)

    @mcp.tool()
    async def flma_inventory(player: str | None = None) -> dict[str, Any]:
        """LIVE — a connected player's main inventory. Requires the
        flma-export-inventories map setting (off by default). If `player` is
        omitted and exactly one player is connected, that player's inventory
        is returned; otherwise pass an explicit name."""
        return await _observe(observe.player_inventory, player=player)

    @mcp.tool()
    async def flma_building_counts(force: str | None = None) -> dict[str, Any]:
        """LIVE — placed-building counts grouped by name and by type,
        optionally filtered to one force. Requires the flma-export-buildings
        map setting (off by default)."""
        return await _observe(observe.building_counts, force=force)

    @mcp.tool()
    async def flma_query_buildings(
        name: str | None = None,
        type: str | None = None,
        surface: str | None = None,
        force: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """LIVE — placed buildings filtered by exact name/type/surface/force,
        with positions (and per-machine crafting contents, for machines the
        flma-contents-tracked-names setting names). Requires the
        flma-export-buildings map setting. `limit` is capped at 200."""
        return await _observe(
            observe.query_buildings, name=name, type=type, surface=surface, force=force, limit=limit
        )

    @mcp.tool()
    async def flma_status() -> dict[str, Any]:
        """LIVE — a compact health summary: which snapshot files exist and
        how stale each is, the active save id, and whether recipes.db looks
        aligned with the currently running save's own tech list (a mismatch
        means recipes.db was built from a different modpack/save and its
        recipe-chain math should not be trusted against this save's live
        state). This is the same signal /healthz exposes, as an MCP tool
        rather than a bare HTTP GET."""
        await state.warm()
        return await asyncio.to_thread(_status_payload)


def _status_payload() -> dict[str, Any]:
    """Runs off the event loop (called via asyncio.to_thread), so a nested
    `asyncio.run()` for the DB fetch below is safe -- no loop is already
    running on this thread."""
    import time as _time

    from planner import config as planner_config

    g = state.gs()
    ages = g.snapshot_ages()
    payload: dict[str, Any] = {
        "script_output_dir": str(g.base_dir),
        "active_save_id": getattr(g, "_active_save_id", None),
        "snapshot_ages_seconds": ages,
        "recipes_db": {
            "path": str(planner_config.RECIPES_DB),
            "exists": planner_config.RECIPES_DB.exists(),
        },
        "freshness": state.freshness(),
    }
    if payload["recipes_db"]["exists"]:
        db_stat = planner_config.RECIPES_DB.stat()
        payload["recipes_db"]["mtime"] = db_stat.st_mtime
        recipes_age = ages.get("recipes")
        if recipes_age is not None:
            db_age = _time.time() - db_stat.st_mtime
            payload["recipes_db"]["older_than_recipes_json"] = db_age > recipes_age
        payload["modpack_alignment"] = _modpack_alignment(g)
    return payload


def _modpack_alignment(g) -> dict[str, Any] | None:
    from planner import config as planner_config
    from planner import live_state
    from planner.recipedb.db import AsyncDatabase

    async def _fetch() -> set[str]:
        db = AsyncDatabase(str(planner_config.RECIPES_DB))
        rows = await db.fetch_all("SELECT name FROM technologies")
        return {r["name"] for r in rows}

    try:
        db_tech_ids = asyncio.run(_fetch())
    except Exception:  # noqa: BLE001 -- a bad/missing DB shouldn't break flma_status
        return None
    return live_state.modpack_alignment(g, db_tech_ids)


__all__ = ["register"]
