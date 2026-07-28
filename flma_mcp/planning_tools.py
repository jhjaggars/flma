"""MCP tools for the 12 text-only factory-planning subcommands (everything
in `planner/cli.py` except the 7 live-observe commands and `build-db` --
see `cli_bridge.EXPOSED_COMMANDS`). Each tool builds an argv list via
`cli_bridge.build_argv` and runs it via `cli_bridge.run_cli`; none of the
recipe-chain/machine-count math is reimplemented here.

Every tool calls `state.warm()` first (see state.py) so the shared
GameState is refreshed off the event loop before the handler's own
synchronous `open_game_state()` call runs, and every result carries the
same `freshness` envelope the live tools do -- plus, since these return
rendered text rather than a dict, a one-line freshness banner prepended to
`output` so it survives even if the model only reads the text.
"""

from __future__ import annotations

from typing import Any

from flma_mcp import cli_bridge, state


def _ids(value: str | list[str]) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


async def _run(command: str, positionals: list[str] | None = None, **flags: Any) -> dict[str, Any]:
    await state.warm()
    argv = cli_bridge.build_argv(command, positionals, **flags)
    result = await cli_bridge.run_cli(argv)
    fresh = state.freshness()
    if fresh["stale"] and result.get("output"):
        result["output"] = f"[{fresh['note']}]\n\n{result['output']}"
    result["freshness"] = fresh
    return result


# The 12 planner subcommands this module wraps as MCP tools -- distinct from
# `cli_bridge.EXPOSED_COMMANDS`, which also includes the 6 live-observe
# commands (research/tech-tree/production/logistics/inventory/buildings)
# that live_tools.py covers directly via observe.py instead, since that path
# skips a round trip through argv/stdout for data live_tools already returns
# as structured dicts. Exists so a test can assert this module's registered
# tools match cli.py's subcommands exactly, minus that intentional overlap.
PLANNING_COMMANDS = frozenset(
    {
        "status",
        "recommend",
        "options",
        "plan",
        "expand",
        "recipe",
        "producers",
        "consumers",
        "power",
        "have",
        "belts",
        "tech",
    }
)


def register(mcp) -> None:
    @mcp.tool()
    async def flma_status_text(force: str = "player") -> dict[str, Any]:
        """Modpack/live-data health check -- is recipes.db aligned with the
        currently running save, current research, snapshot ages. This is
        the CLI's own rendered text; prefer the `flma_status` tool (the
        live-observe one, no "_text" suffix) for a structured version of
        the same data unless you specifically want the human-readable
        rendering."""
        return await _run("status", force=force)

    @mcp.tool()
    async def flma_recommend(
        product: str | list[str],
        rate: str | None = None,
        unit: str = "per-sec",
        max_depth: int = 6,
        force: str = "player",
        stop_items: str | None = None,
        auto_stop_raw: bool = True,
        recipe: str | None = None,
    ) -> dict[str, Any]:
        """START HERE for an open-ended "how do I make X" question -- the
        single best current way to make one or more products, live-tech-
        scoped against the running save. `product` may be an internal
        item/fluid id, a human display name, or a list of several to answer
        in one call. `rate` is a comparison yardstick (e.g. "15/s", "900/min";
        a bare number uses `unit`); default 60/min. `recipe` is a
        comma-separated ITEM=RECIPE override list."""
        return await _run(
            "recommend",
            _ids(product),
            rate=rate,
            unit=unit,
            max_depth=max_depth,
            force=force,
            stop_items=stop_items,
            auto_stop_raw=auto_stop_raw,
            recipe=recipe,
        )

    @mcp.tool()
    async def flma_options(
        product: str | list[str],
        rate: str | None = None,
        unit: str = "per-sec",
        max_depth: int = 6,
        top: int = 8,
        force: str = "player",
        stop_items: str | None = None,
        auto_stop_raw: bool = True,
        recipe: str | None = None,
        include_byproducts: bool = False,
    ) -> dict[str, Any]:
        """Compare every viable way to make one or more products side by
        side, at a shared comparison yardstick (`rate`, default 60/min) --
        the decision menu behind `flma_plan`/`flma_expand`. For a single
        best answer instead, use `flma_recommend`."""
        return await _run(
            "options",
            _ids(product),
            rate=rate,
            unit=unit,
            max_depth=max_depth,
            top=top,
            force=force,
            stop_items=stop_items,
            auto_stop_raw=auto_stop_raw,
            recipe=recipe,
            include_byproducts=include_byproducts,
        )

    @mcp.tool()
    async def flma_plan(
        product: str | list[str],
        rate: str | None = None,
        unit: str = "per-sec",
        cap: float | None = None,
        belts: float | None = None,
        belt_tier: str | None = None,
        max_depth: int = 6,
        top: int = 15,
        force: str = "player",
        stop_items: str | None = None,
        auto_stop_raw: bool = True,
        recipe: str | None = None,
        full: bool = False,
    ) -> dict[str, Any]:
        """Design a sized production line (machine counts, raw inputs, belt
        counts) for one or more products, cross-referenced against the LIVE
        running save -- existing machines/production/buffered stock, so the
        result says what to reuse instead of assuming a from-scratch build.
        `rate`/`cap`/`belts` are mutually exclusive rate-selection modes (see
        each param); omit all three to size for exactly 1 of the top-level
        recipe's fastest eligible machine. `recipe` is a comma-separated
        ITEM=RECIPE override list; `full` gives a per-row breakdown instead
        of the default compact summary."""
        return await _run(
            "plan",
            _ids(product),
            rate=rate,
            unit=unit,
            cap=cap,
            belts=belts,
            belt_tier=belt_tier,
            max_depth=max_depth,
            top=top,
            force=force,
            stop_items=stop_items,
            auto_stop_raw=auto_stop_raw,
            recipe=recipe,
            full=full,
        )

    @mcp.tool()
    async def flma_expand(
        product: str | list[str],
        rate: str | None = None,
        unit: str = "per-sec",
        max_depth: int = 6,
        top: int = 15,
        force: str = "player",
        stop_items: str | None = None,
        auto_stop_raw: bool = True,
        recipe: str | None = None,
        alternates: bool = False,
    ) -> dict[str, Any]:
        """Full bill-of-materials tree for one or more products -- use to
        sanity-check a recipe chain `flma_plan`'s raw-input totals look
        wrong for. `alternates` also lists each node's other viable
        candidate recipes instead of only the one auto-selected."""
        return await _run(
            "expand",
            _ids(product),
            rate=rate,
            unit=unit,
            max_depth=max_depth,
            top=top,
            force=force,
            stop_items=stop_items,
            auto_stop_raw=auto_stop_raw,
            recipe=recipe,
            alternates=alternates,
        )

    @mcp.tool()
    async def flma_recipe(name: str | list[str], force: str = "player") -> dict[str, Any]:
        """Full ingredient/product detail for one or more already-identified
        recipe ids or human names -- use `flma_recommend`/`flma_producers`
        first to find the id(s)."""
        return await _run("recipe", _ids(name), force=force)

    @mcp.tool()
    async def flma_producers(item: str | list[str], force: str = "player") -> dict[str, Any]:
        """What recipes produce this item (exact internal id), tagged with
        how many are already built on the live save. For a single best
        answer use `flma_recommend` instead; this is for manual comparison."""
        return await _run("producers", _ids(item), force=force)

    @mcp.tool()
    async def flma_consumers(item: str | list[str], force: str = "player") -> dict[str, Any]:
        """What recipes consume this item (exact internal id)."""
        return await _run("consumers", _ids(item), force=force)

    @mcp.tool()
    async def flma_power(entity: str | list[str]) -> dict[str, Any]:
        """Power stats for one or more machines/drills/generators (exact id
        or human name) -- burner fuel burn rate, or generator max output/
        fluid usage."""
        return await _run("power", _ids(entity))

    @mcp.tool()
    async def flma_have(item: str | list[str], force: str = "player") -> dict[str, Any]:
        """LIVE net production (input − output, per minute) and buffered
        logistics stock for one or more items on the running save -- "what
        am I already producing/storing toward this"."""
        return await _run("have", _ids(item), force=force)

    @mcp.tool()
    async def flma_belts(
        count: float, tier: str | None = None, force: str = "player"
    ) -> dict[str, Any]:
        """Convert a belt-lane count to an achievable item rate, for feeding
        into `flma_plan`'s `rate` -- e.g. "2 belts of iron plate" -> items/sec.
        `tier` is a belt id (transport-belt/fast-transport-belt/express-
        transport-belt/turbo-transport-belt); default is the fastest tier
        the running save can currently build."""
        return await _run("belts", [str(count)], tier=tier, force=force)

    @mcp.tool()
    async def flma_tech(
        name: str,
        rate: str | None = None,
        unit: str = "per-sec",
        anchor: str | None = None,
        consume: str | None = None,
        force: str = "player",
    ) -> dict[str, Any]:
        """What one technology unlocks, and whether its unlocked recipes
        combine into a zero-waste recycling bundle (a small exact-blend
        solve across only the recipes that one tech unlocks together) --
        `flma_recommend` already calls this when relevant; use directly only
        to size a bundle's math yourself. `consume` (format "ITEM=RATE")
        sizes the bundle so a raw INPUT is fully consumed at that rate
        instead of hitting an output target; overrides `rate`/`anchor`."""
        return await _run(
            "tech", [name], rate=rate, unit=unit, anchor=anchor, consume=consume, force=force
        )


__all__ = ["register"]
