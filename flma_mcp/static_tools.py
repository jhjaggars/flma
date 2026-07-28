"""MCP tools ported from the homelab repo's `apps/recipe-mcp/src/server.py`
(the always-on Factorio recipe-lookup MCP server Hermes already had wired
up) -- the pure recipes.db lookups that `planner/cli.py` never grew CLI
subcommands for (search-by-fuzzy-name, browse-by-category/group, machine/
drill lookups, research-path expansion).

This exists to let flma supersede recipe-mcp: flma's `recipes.db` schema is
a strict superset of recipe-mcp's (verified: same 13 core tables, plus
fuels/generators/machine_fuel_categories), built from the CURRENTLY RUNNING
save rather than a hand-copied, git-committed dump, so every query below
ports over unchanged. The overlapping tools recipe-mcp also had
(find_recipes_producing/find_recipes_using/get_recipe/plan_factory/
find_recipes_unlocked_by_technology/list_researchable_technologies) are
NOT re-ported here -- flma's own `producers`/`consumers`/`recipe`/`plan`/
`recommend`/`tech`/`tech-tree` CLI commands (see planning_tools.py) already
cover the same ground with LIVE annotations (built-count tags, live
tech-scoping) recipe-mcp's static DB can't provide.

None of this needs live game state or `state.warm()` -- it's read-only
recipes.db lookups, same as the CLI commands that already use
`planner/recipedb/`."""

from __future__ import annotations

from typing import Any

from planner import config as planner_config
from planner.recipedb import engine as recipedb_engine
from planner.recipedb.db import AsyncDatabase
from planner.recipedb.engine import _DEFAULT_EXCLUDE, _expand_node

MAX_RESULTS = 50


def _db() -> AsyncDatabase | None:
    if not planner_config.RECIPES_DB.exists():
        return None
    return AsyncDatabase(str(planner_config.RECIPES_DB))


def _no_db_error() -> dict[str, Any]:
    return {
        "error": f"recipes.db not found at {planner_config.RECIPES_DB}",
        "hint": "build it first: make build-db (or: uv run python -m planner build-db)",
    }


async def _tech_detail(db: AsyncDatabase, tech_name: str, row: dict) -> dict[str, Any]:
    prereqs = await db.fetch_all(
        "SELECT prereq_name FROM technology_prerequisites WHERE tech_name = ? ORDER BY prereq_name",
        (tech_name,),
    )
    ings = await db.fetch_all(
        "SELECT item_name, amount FROM technology_ingredients WHERE tech_name = ? ORDER BY position",
        (tech_name,),
    )
    return {
        "id": tech_name,
        "name": row["translated_name"],
        "enabled": bool(row["enabled"]),
        "researched": bool(row["researched"]),
        "prerequisites": [p["prereq_name"] for p in prereqs],
        "unit_count": row["unit_count"],
        "unit_count_formula": row["unit_count_formula"],
        "unit_energy": row["unit_energy"],
        "unit_ingredients": [{"name": i["item_name"], "amount": i["amount"]} for i in ings],
    }


async def _prereqs_bfs(db: AsyncDatabase, seed_names: set[str], max_techs: int = 600) -> dict:
    visited: dict[str, dict] = {}
    queue = list(seed_names)
    while queue and len(visited) < max_techs:
        name = queue.pop(0)
        if name in visited:
            continue
        row = await db.fetch_one(
            """SELECT name, translated_name, enabled, researched,
                      unit_count, unit_count_formula, unit_energy
               FROM technologies WHERE name = ?""",
            (name,),
        )
        if row is None:
            continue
        visited[name] = await _tech_detail(db, name, row)
        for p in visited[name]["prerequisites"]:
            if p not in visited:
                queue.append(p)
    return visited


def _collect_recipe_ids(tree: dict) -> set[str]:
    used: set[str] = set()
    if not tree.get("leaf") and "recipe" in tree:
        used.add(tree["recipe"]["id"])
        for child in tree.get("ingredients", []):
            used |= _collect_recipe_ids(child)
    return used


def register(mcp) -> None:
    @mcp.tool()
    async def flma_search_recipes(query: str, limit: int = 25) -> dict[str, Any]:
        """Search recipes.db for recipes by name or translated name (fuzzy
        match) -- a starting point when you don't know the exact recipe id.
        Returns recipe id, translated name, category, and group."""
        db = _db()
        if db is None:
            return _no_db_error()
        limit = max(1, min(limit, MAX_RESULTS))
        pattern = f"%{query}%"
        rows = await db.fetch_all(
            """
            SELECT r.name, r.translated_name, r.category, r.group_name, r.enabled,
                   COUNT(*) OVER () AS total_matches
            FROM recipes r
            WHERE r.name LIKE ? COLLATE NOCASE
               OR r.translated_name LIKE ? COLLATE NOCASE
            ORDER BY
                CASE WHEN r.name = ? THEN 0
                     WHEN r.translated_name = ? THEN 1
                     ELSE 2 END,
                r.translated_name COLLATE NOCASE
            LIMIT ?
            """,
            (pattern, pattern, query, query, limit),
        )
        if not rows:
            return {"results": [], "total_matches": 0, "truncated": False}
        total = rows[0]["total_matches"]
        return {
            "results": [
                {
                    "name": r["name"],
                    "translated_name": r["translated_name"],
                    "category": r["category"],
                    "group": r["group_name"],
                    "enabled": bool(r["enabled"]),
                }
                for r in rows
            ],
            "total_matches": total,
            "truncated": total > limit,
        }

    @mcp.tool()
    async def flma_lookup_item(name: str) -> dict[str, Any]:
        """Resolve a fuzzy item or fluid name to its internal id and kind --
        useful for disambiguating a human-readable name before calling
        flma's other tools with an exact id."""
        db = _db()
        if db is None:
            return _no_db_error()
        pattern = f"%{name}%"
        rows = await db.fetch_all(
            """
            SELECT name, kind, translated_name
            FROM names
            WHERE name = ?
               OR name LIKE ? COLLATE NOCASE
               OR translated_name LIKE ? COLLATE NOCASE
            ORDER BY
                CASE WHEN name = ? THEN 0
                     WHEN translated_name = ? THEN 1
                     ELSE 2 END,
                translated_name COLLATE NOCASE
            LIMIT 20
            """,
            (name, pattern, pattern, name, name),
        )
        if not rows:
            return {"matches": [], "hint": f"No item or fluid found matching '{name}'."}
        return {
            "matches": [
                {"id": r["name"], "kind": r["kind"], "translated_name": r["translated_name"]}
                for r in rows
            ],
            "total": len(rows),
        }

    @mcp.tool()
    async def flma_list_recipes_by_category(category: str, limit: int = 25) -> dict[str, Any]:
        """List recipes.db recipes crafted in a specific machine category
        (e.g. 'crafting', 'chemistry', 'smelting'). Use flma_search_recipes
        first if unsure of the category name."""
        db = _db()
        if db is None:
            return _no_db_error()
        limit = max(1, min(limit, MAX_RESULTS))
        pattern = f"%{category}%"
        rows = await db.fetch_all(
            """
            SELECT name, translated_name, category, group_name, enabled,
                   COUNT(*) OVER () AS total_matches
            FROM recipes
            WHERE category = ? OR category LIKE ? COLLATE NOCASE
            ORDER BY translated_name COLLATE NOCASE
            LIMIT ?
            """,
            (category, pattern, limit),
        )
        if not rows:
            cats = await db.fetch_all(
                """SELECT category, COUNT(*) AS n FROM recipes
                   GROUP BY category ORDER BY n DESC LIMIT 20"""
            )
            return {
                "results": [],
                "total_matches": 0,
                "hint": f"Category '{category}' not found. Top categories:",
                "top_categories": [{"category": c["category"], "count": c["n"]} for c in cats],
            }
        total = rows[0]["total_matches"]
        return {
            "category": rows[0]["category"],
            "results": [
                {
                    "name": r["name"],
                    "translated_name": r["translated_name"],
                    "group": r["group_name"],
                    "enabled": bool(r["enabled"]),
                }
                for r in rows
            ],
            "total_matches": total,
            "truncated": total > limit,
        }

    @mcp.tool()
    async def flma_list_recipes_by_group(group: str, limit: int = 25) -> dict[str, Any]:
        """List recipes.db recipes belonging to a tech-tree group (e.g.
        'logistics', 'combat', a modpack-specific group like 'py-industry').
        Use flma_search_recipes first if unsure of the group name."""
        db = _db()
        if db is None:
            return _no_db_error()
        limit = max(1, min(limit, MAX_RESULTS))
        pattern = f"%{group}%"
        rows = await db.fetch_all(
            """
            SELECT name, translated_name, category, group_name, enabled,
                   COUNT(*) OVER () AS total_matches
            FROM recipes
            WHERE group_name = ? OR group_name LIKE ? COLLATE NOCASE
            ORDER BY translated_name COLLATE NOCASE
            LIMIT ?
            """,
            (group, pattern, limit),
        )
        if not rows:
            groups = await db.fetch_all(
                """SELECT group_name, COUNT(*) AS n FROM recipes
                   GROUP BY group_name ORDER BY n DESC LIMIT 20"""
            )
            return {
                "results": [],
                "total_matches": 0,
                "hint": f"Group '{group}' not found. Top groups:",
                "top_groups": [{"group": g["group_name"], "count": g["n"]} for g in groups],
            }
        total = rows[0]["total_matches"]
        return {
            "group": rows[0]["group_name"],
            "results": [
                {
                    "name": r["name"],
                    "translated_name": r["translated_name"],
                    "category": r["category"],
                    "enabled": bool(r["enabled"]),
                }
                for r in rows
            ],
            "total_matches": total,
            "truncated": total > limit,
        }

    @mcp.tool()
    async def flma_find_machines_for_category(category: str, limit: int = 25) -> dict[str, Any]:
        """Find buildings that can craft recipes.db recipes in a given
        crafting category, fastest first -- use when a recipe shows a
        category (e.g. 'smelting', 'chemistry') and you need to know which
        building crafts it and how fast."""
        db = _db()
        if db is None:
            return _no_db_error()
        limit = max(1, min(limit, MAX_RESULTS))
        pattern = f"%{category}%"
        rows = await db.fetch_all(
            """
            SELECT m.name, m.translated_name, m.type, m.crafting_speed,
                   m.energy_consumption, m.energy_source, m.module_slots,
                   mcc.category,
                   COUNT(*) OVER () AS total_matches
            FROM machines m
            JOIN machine_crafting_categories mcc ON mcc.machine_name = m.name
            WHERE mcc.category = ? OR mcc.category LIKE ? COLLATE NOCASE
            ORDER BY m.crafting_speed DESC, m.name ASC
            LIMIT ?
            """,
            (category, pattern, limit),
        )
        if not rows:
            top_cats = await db.fetch_all(
                """SELECT mcc.category, COUNT(*) AS n
                   FROM machine_crafting_categories mcc
                   GROUP BY mcc.category ORDER BY n DESC LIMIT 20"""
            )
            return {
                "results": [],
                "total_matches": 0,
                "hint": f"Category '{category}' not found. Sample categories:",
                "sample_categories": [r["category"] for r in top_cats],
            }
        total = rows[0]["total_matches"]
        return {
            "category": rows[0]["category"],
            "results": [
                {
                    "id": r["name"],
                    "name": r["translated_name"],
                    "type": r["type"],
                    "crafting_speed": r["crafting_speed"],
                    "energy_consumption_w": r["energy_consumption"],
                    "energy_source": r["energy_source"],
                    "module_slots": r["module_slots"],
                }
                for r in rows
            ],
            "total_matches": total,
            "truncated": total > limit,
        }

    @mcp.tool()
    async def flma_get_machine(machine: str) -> dict[str, Any]:
        """Full detail for a crafting machine from recipes.db: speed,
        energy, crafting categories, and how to build the machine itself
        (its build recipe(s) + unlocking technology)."""
        db = _db()
        if db is None:
            return _no_db_error()
        return await _get_machine(db, machine)

    @mcp.tool()
    async def flma_find_drills_for_resource_category(
        resource_category: str, limit: int = 25
    ) -> dict[str, Any]:
        """Find mining drills from recipes.db that can mine a given
        resource category (modpacks often split ores into several
        categories, e.g. 'basic-solid' vs a modpack-specific one). Empty
        results are expected if the export predates drill data."""
        db = _db()
        if db is None:
            return _no_db_error()
        limit = max(1, min(limit, MAX_RESULTS))
        pattern = f"%{resource_category}%"
        rows = await db.fetch_all(
            """
            SELECT md.name, md.translated_name, md.mining_speed,
                   md.energy_consumption, md.energy_source, md.module_slots,
                   drc.resource_category,
                   COUNT(*) OVER () AS total_matches
            FROM mining_drills md
            JOIN drill_resource_categories drc ON drc.drill_name = md.name
            WHERE drc.resource_category = ? OR drc.resource_category LIKE ? COLLATE NOCASE
            ORDER BY md.mining_speed DESC, md.name ASC
            LIMIT ?
            """,
            (resource_category, pattern, limit),
        )
        if not rows:
            top_cats = await db.fetch_all(
                """SELECT resource_category, COUNT(*) AS n
                   FROM drill_resource_categories
                   GROUP BY resource_category ORDER BY n DESC LIMIT 20"""
            )
            return {
                "results": [],
                "total_matches": 0,
                "hint": f"Resource category '{resource_category}' not found. Sample categories:",
                "sample_categories": [r["resource_category"] for r in top_cats],
            }
        total = rows[0]["total_matches"]
        return {
            "resource_category": rows[0]["resource_category"],
            "results": [
                {
                    "id": r["name"],
                    "name": r["translated_name"],
                    "mining_speed": r["mining_speed"],
                    "energy_consumption_w": r["energy_consumption"],
                    "energy_source": r["energy_source"],
                    "module_slots": r["module_slots"],
                }
                for r in rows
            ],
            "total_matches": total,
            "truncated": total > limit,
        }

    @mcp.tool()
    async def flma_find_technologies_unlocking_recipe(recipe: str) -> dict[str, Any]:
        """Which technologies unlock a given recipe.db recipe, with each
        tech's prerequisites and science pack cost, and whether the recipe
        is available from the start with no research at all. This is
        recipes.db's static answer -- for whether it's ACTUALLY researched
        on the running save right now, prefer flma_research/flma_tech_tree."""
        db = _db()
        if db is None:
            return _no_db_error()
        row = await db.fetch_one(
            "SELECT name, translated_name, enabled FROM recipes WHERE name = ?", (recipe,)
        )
        if row is None:
            row = await db.fetch_one(
                "SELECT name, translated_name, enabled FROM recipes "
                "WHERE translated_name = ? COLLATE NOCASE",
                (recipe,),
            )
        if row is None:
            fuzzy = await db.fetch_all(
                """SELECT name, translated_name, enabled FROM recipes
                   WHERE name LIKE ? COLLATE NOCASE OR translated_name LIKE ? COLLATE NOCASE
                   LIMIT 10""",
                (f"%{recipe}%", f"%{recipe}%"),
            )
            if not fuzzy:
                return {"error": f"No recipe found matching '{recipe}'."}
            if len(fuzzy) > 1:
                return {
                    "ambiguous": True,
                    "candidates": [{"id": r["name"], "name": r["translated_name"]} for r in fuzzy],
                    "hint": "Pass the exact recipe id.",
                }
            row = fuzzy[0]

        recipe_id = row["name"]
        tech_rows = await db.fetch_all(
            """SELECT t.name, t.translated_name, t.enabled, t.researched,
                      t.unit_count, t.unit_count_formula, t.unit_energy
               FROM technology_recipe_unlocks tu
               JOIN technologies t ON t.name = tu.tech_name
               WHERE tu.recipe_name = ?
               ORDER BY t.name""",
            (recipe_id,),
        )
        unlocked_by = [await _tech_detail(db, t["name"], t) for t in tech_rows]
        initially_enabled = bool(row["enabled"]) and not unlocked_by
        return {
            "recipe": recipe_id,
            "recipe_name": row["translated_name"],
            "initially_enabled": initially_enabled,
            "unlocked_by": unlocked_by,
        }

    @mcp.tool()
    async def flma_get_research_requirements(
        product: str,
        amount: float = 1.0,
        max_depth: int = 5,
        stop_items: list[str] | None = None,
        stop_categories: list[str] | None = None,
        exclude_categories: list[str] | None = None,
        prefer_enabled: bool = True,
        recipe_overrides: dict[str, str] | None = None,
        include_prerequisites: bool = True,
        summary: bool = True,
    ) -> dict[str, Any]:
        """Full research path (recipes.db-only, not live-tech-scoped) needed
        to unlock every recipe in a product's chain: expands the recipe
        chain the same way flma_expand does, maps each recipe to the
        technology that unlocks it, and (when include_prerequisites=True)
        walks transitive tech prerequisites too. Returns total science pack
        cost for everything not yet researched at recipes.db build time --
        for whether YOU have researched it right now, cross-check against
        flma_research/flma_tech_tree instead, since recipes.db's
        `researched` flags are a snapshot from whenever `build-db` last ran,
        not the live save."""
        db = _db()
        if db is None:
            return _no_db_error()
        recipedb_engine.set_db(db)

        max_depth = max(1, min(max_depth, 15))
        exclude_cats = frozenset(
            exclude_categories if exclude_categories is not None else _DEFAULT_EXCLUDE
        )
        stop_cats = frozenset(stop_categories or [])
        stop_set = frozenset(stop_items or [])
        overrides: dict[str, str] = dict(recipe_overrides or {})

        name_row = await db.fetch_one("SELECT name, kind FROM names WHERE name = ?", (product,))
        if name_row is None:
            name_row = await db.fetch_one(
                "SELECT name, kind FROM names WHERE translated_name = ? COLLATE NOCASE",
                (product,),
            )
        if name_row is None:
            fuzzy = await db.fetch_all(
                """SELECT name, kind, translated_name FROM names
                   WHERE name LIKE ? COLLATE NOCASE OR translated_name LIKE ? COLLATE NOCASE
                   ORDER BY translated_name COLLATE NOCASE LIMIT 10""",
                (f"%{product}%", f"%{product}%"),
            )
            if not fuzzy:
                return {"error": f"No item or fluid found matching '{product}'."}
            if len(fuzzy) > 1:
                return {
                    "ambiguous": True,
                    "candidates": [
                        {"id": r["name"], "kind": r["kind"], "name": r["translated_name"]}
                        for r in fuzzy
                    ],
                }
            name_row = fuzzy[0]

        item_id: str = name_row["name"]
        item_kind: str = name_row["kind"]

        totals_items: dict[str, float] = {}
        totals_fluids: dict[str, float] = {}
        unresolved: list[dict] = []
        alternates_map: dict[str, list[dict]] = {}
        selection_notes: list[str] = []

        tree = await _expand_node(
            item_id,
            item_kind,
            amount,
            depth=0,
            max_depth=max_depth,
            exclude_cats=exclude_cats,
            stop_cats=stop_cats,
            stop_items=stop_set,
            prefer_enabled=prefer_enabled,
            overrides=overrides,
            ancestors=frozenset(),
            totals_items=totals_items,
            totals_fluids=totals_fluids,
            unresolved=unresolved,
            alternates_map=alternates_map,
            selection_notes=selection_notes,
        )

        recipe_ids = _collect_recipe_ids(tree)
        unlocked_initially: list[str] = []
        direct_tech_names: set[str] = set()
        recipe_tech_map: list[dict] = []

        for recipe_id in sorted(recipe_ids):
            tech_rows = await db.fetch_all(
                """SELECT t.name FROM technology_recipe_unlocks tu
                   JOIN technologies t ON t.name = tu.tech_name
                   WHERE tu.recipe_name = ?""",
                (recipe_id,),
            )
            if tech_rows:
                techs = [r["name"] for r in tech_rows]
                direct_tech_names.update(techs)
                recipe_tech_map.append({"recipe": recipe_id, "unlocked_by": techs})
            else:
                rec = await db.fetch_one("SELECT enabled FROM recipes WHERE name = ?", (recipe_id,))
                if rec and rec["enabled"]:
                    unlocked_initially.append(recipe_id)
                    recipe_tech_map.append({"recipe": recipe_id, "unlocked_by": []})

        if include_prerequisites and direct_tech_names:
            all_techs = await _prereqs_bfs(db, direct_tech_names)
        else:
            all_techs = {}
            for tname in direct_tech_names:
                row = await db.fetch_one(
                    """SELECT name, translated_name, enabled, researched,
                              unit_count, unit_count_formula, unit_energy
                       FROM technologies WHERE name = ?""",
                    (tname,),
                )
                if row:
                    all_techs[tname] = await _tech_detail(db, tname, row)

        pack_totals: dict[str, float] = {}
        for tech in all_techs.values():
            if tech["researched"]:
                continue
            count = tech.get("unit_count") or 0
            for ing in tech.get("unit_ingredients", []):
                pack_totals[ing["name"]] = pack_totals.get(ing["name"], 0.0) + ing["amount"] * count

        direct_out = [v for k, v in all_techs.items() if k in direct_tech_names]
        prereq_out = [v for k, v in all_techs.items() if k not in direct_tech_names]

        base: dict[str, Any] = {
            "product": item_id,
            "recipes_in_chain": sorted(recipe_ids),
            "unlocked_initially": unlocked_initially,
            "science_pack_totals": [
                {"id": k, "amount": v}
                for k, v in sorted(pack_totals.items(), key=lambda kv: -kv[1])
            ],
            "total_technologies": len(all_techs),
            "unresearched_count": sum(1 for t in all_techs.values() if not t["researched"]),
            "buildable_now": all(t["researched"] for t in all_techs.values()),
        }
        if summary:
            base["direct_technologies"] = sorted(direct_tech_names)
            base["prerequisite_technologies"] = sorted(
                k for k in all_techs if k not in direct_tech_names
            )
        else:
            base["recipe_tech_map"] = recipe_tech_map
            base["direct_technologies"] = sorted(direct_out, key=lambda t: t["id"])
            base["prerequisite_technologies"] = sorted(prereq_out, key=lambda t: t["id"])
        return base


async def _get_machine(db: AsyncDatabase, machine: str) -> dict[str, Any]:
    row = await db.fetch_one("SELECT * FROM machines WHERE name = ?", (machine,))
    if row is None:
        row = await db.fetch_one(
            "SELECT * FROM machines WHERE translated_name = ? COLLATE NOCASE", (machine,)
        )
    if row is None:
        candidates = await db.fetch_all(
            """SELECT name, translated_name, crafting_speed FROM machines
               WHERE name LIKE ? COLLATE NOCASE OR translated_name LIKE ? COLLATE NOCASE
               ORDER BY translated_name COLLATE NOCASE LIMIT 10""",
            (f"%{machine}%", f"%{machine}%"),
        )
        if not candidates:
            return {"error": f"No machine found matching '{machine}'"}
        if len(candidates) == 1:
            return await _get_machine(db, candidates[0]["name"])
        return {
            "ambiguous": True,
            "candidates": [
                {
                    "id": c["name"],
                    "name": c["translated_name"],
                    "crafting_speed": c["crafting_speed"],
                }
                for c in candidates
            ],
        }

    cat_rows = await db.fetch_all(
        "SELECT category FROM machine_crafting_categories WHERE machine_name = ? ORDER BY category",
        (row["name"],),
    )
    build_recs = await db.fetch_all(
        """SELECT r.name, r.translated_name, r.enabled,
                  COALESCE(t.name, '') AS tech_name,
                  COALESCE(t.translated_name, '') AS tech_display,
                  t.researched
           FROM recipe_products rp
           JOIN recipes r ON r.name = rp.recipe_name
           LEFT JOIN technology_recipe_unlocks tru ON tru.recipe_name = r.name
           LEFT JOIN technologies t ON t.name = tru.tech_name
           WHERE rp.item_name = ?
           ORDER BY r.enabled DESC, r.name""",
        (row["name"],),
    )
    seen_recs: dict[str, dict] = {}
    for br in build_recs:
        rname = br["name"]
        if rname not in seen_recs:
            seen_recs[rname] = {
                "id": rname,
                "name": br["translated_name"],
                "enabled": bool(br["enabled"]),
                "unlocked_by": [],
            }
        if br["tech_name"]:
            seen_recs[rname]["unlocked_by"].append(
                {
                    "id": br["tech_name"],
                    "name": br["tech_display"],
                    "researched": bool(br["researched"]),
                }
            )
    return {
        "id": row["name"],
        "name": row["translated_name"],
        "type": row["type"],
        "crafting_speed": row["crafting_speed"],
        "energy_consumption_w": row["energy_consumption"],
        "energy_source": row["energy_source"],
        "module_slots": row["module_slots"],
        "crafting_categories": [r["category"] for r in cat_rows],
        "build_recipes": list(seen_recs.values()),
    }


__all__ = ["register"]
