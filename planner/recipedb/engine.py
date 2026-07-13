"""Recipe-expansion and factory-planning engine.

Vendored from recipe-mcp's `src/engine.py` (see `planner/recipedb/__init__.py`
for provenance) — the pure calculation logic recipe-mcp originally extracted
from its `server.py` so it could be imported by anything that just wants to
*compute* (recipe-chain expansion, machine-count math, raw-input rollup)
without pulling in FastMCP or running an MCP server. This is the single
source of truth for the math; `planner/cli.py` is the only caller here.

Usage:

    from planner.recipedb import engine
    from planner.recipedb.db import AsyncDatabase

    engine.set_db(AsyncDatabase("/path/to/recipes.db"))
    result = await engine.plan_product("electronic-circuit", rate_per_min=120)

All functions reference the module-global `db` by name (not as a parameter) —
call `set_db()` once before using anything else in this module.
"""

from __future__ import annotations

import itertools
import math

from .db import AsyncDatabase

db: AsyncDatabase | None = None


def set_db(database: AsyncDatabase) -> None:
    """Point the engine at a recipes.db. Call once before using this module."""
    global db
    db = database


# ---------------------------------------------------------------------------
# expand_recipe_chain / plan_factory core — recursive bill-of-materials
# ---------------------------------------------------------------------------

_DEFAULT_EXCLUDE: frozenset[str] = frozenset({"py-incineration", "py-barreling", "py-unbarreling"})

# Items with a `resources` table entry (a real mineable/harvestable map
# resource, per RecipeExporter's own entity dump) that we deliberately do
# NOT auto-stop at, because they're confirmed (or suspected, by having their
# own `<item>-plantation-mk*` building — same pattern as ralesia, confirmed
# below) to need real cultivation infrastructure rather than "walk up and
# harvest": planting/growing/plantation buildings, not a drill on a patch.
# Revisit and move entries down to _AUTO_RAW_MANUAL_INCLUDE once confirmed.
_AUTO_RAW_FLORA_EXCLUDE: frozenset[str] = frozenset(
    {
        "ralesia",  # confirmed: needs a ralesia-plantation, not just a harvester
        "kicalk",  # has kicalk-plantation-mk* — same pattern as ralesia, unconfirmed
        "rennea",  # has rennea-plantation-mk* — same pattern as ralesia, unconfirmed
        "tuuphra",  # has tuuphra-plantation-mk* — same pattern as ralesia, unconfirmed
        "mova",  # no plantation building found, but not independently confirmed simple
        "yotoi",  # no plantation building found, but not independently confirmed simple
        "yotoi-fruit",  # no plantation building found, but not independently confirmed simple
        "grod",  # no plantation building found, but not independently confirmed simple
        "cadaveric-arum",  # no plantation building found, but not independently confirmed simple
    }
)

# Confirmed-simple items with no `resources` table entry at all (they're
# drawn via a zero-ingredient recipe, e.g. water-free, or are extreme
# statistical outliers with no sane default producer, e.g. biomass — see
# elide-candidate analysis) but should still be treated as raw by default.
_AUTO_RAW_MANUAL_INCLUDE: frozenset[str] = frozenset({"water", "biomass", "native-flora"})


async def _auto_raw_items() -> frozenset[str]:
    """Items to treat as raw/stop-expansion by default: real mineable or
    harvestable map resources (from RecipeExporter's own `resources` entity
    dump — ground truth, not inferred from recipe counts), minus flora that
    need real cultivation setup, plus a small manually-confirmed list the
    `resources` table doesn't cover. See plan_product's `auto_stop_raw`."""
    rows = await db.fetch_all(
        "SELECT DISTINCT product_name FROM resources WHERE product_name != ''"
    )
    mined = {r["product_name"] for r in rows}
    return frozenset(mined - _AUTO_RAW_FLORA_EXCLUDE) | _AUTO_RAW_MANUAL_INCLUDE


def _effective_out(row: dict) -> float:
    """Expected product amount per recipe batch, accounting for probability."""
    amt: float | None = row.get("out_amount")
    if amt is None:
        lo = float(row.get("amount_min") or 0)
        hi = float(row.get("amount_max") or 0)
        amt = (lo + hi) / 2.0
    prob = float(row.get("probability") or 1.0)
    return max(float(amt) * prob, 1e-9)


def _tally(
    item_id: str,
    kind: str,
    amount: float,
    totals_items: dict[str, float],
    totals_fluids: dict[str, float],
) -> None:
    bucket = totals_fluids if kind == "fluid" else totals_items
    bucket[item_id] = bucket.get(item_id, 0.0) + amount


async def _techs_unlocking(recipe_names: list[str]) -> list[str]:
    """Translated names of technologies that would unlock any of `recipe_names`."""
    if not recipe_names:
        return []
    placeholders = ",".join("?" * len(recipe_names))
    rows = await db.fetch_all(
        f"""SELECT DISTINCT t.translated_name FROM technology_recipe_unlocks tru
            JOIN technologies t ON t.name = tru.tech_name
            WHERE tru.recipe_name IN ({placeholders})
            ORDER BY t.translated_name""",
        tuple(recipe_names),
    )
    return [r["translated_name"] for r in rows]


async def unlocked_recipes_for_techs(tech_names: list[str] | None) -> frozenset[str]:
    """Recipe ids unlocked by any of the given (already-researched) tech ids."""
    if not tech_names:
        return frozenset()
    placeholders = ",".join("?" * len(tech_names))
    rows = await db.fetch_all(
        f"SELECT DISTINCT recipe_name FROM technology_recipe_unlocks WHERE tech_name IN ({placeholders})",
        tuple(tech_names),
    )
    return frozenset(r["recipe_name"] for r in rows)


async def _is_item_buildable(
    item_name: str, extra_unlocked: frozenset[str]
) -> tuple[bool, list[str]]:
    """Whether `item_name` (a machine/drill entity's own item) can currently be
    built: true if it has no build recipe at all AND it's a real, named item
    (a bare starter entity), or if any of its build recipes is enabled,
    already researched per the DB snapshot, or unlocked by a tech in
    `extra_unlocked`. Returns (buildable, missing_tech_names) —
    missing_tech_names lists the still-needed technologies, for "blocked"
    messages.
    """
    build_recs = await db.fetch_all(
        """
        SELECT r.name, r.enabled,
               EXISTS(
                   SELECT 1 FROM technology_recipe_unlocks tru
                   JOIN technologies t ON t.name = tru.tech_name
                   WHERE tru.recipe_name = r.name AND t.researched = 1
               ) AS db_unlocked
        FROM recipes r
        WHERE r.name IN (SELECT recipe_name FROM recipe_products WHERE item_name = ?)
        """,
        (item_name,),
    )
    if not build_recs:
        # No build recipe at all is ambiguous: it's either a genuine bare
        # starter entity (burner-mining-drill, stone-furnace — always
        # available), or a dead/orphaned machine-table row that duplicates a
        # real, properly tech-gated tier under a different id (seen in the
        # wild as "-legacy"/"-turd"/"-base" suffixed rows — e.g.
        # "wpu-mk04-legacy" duplicating "wpu-mk04" at the same crafting
        # speed with no recipe or tech-unlock recorded at all). The `names`
        # table is the curated real-item registry built from the game's own
        # localization data; every genuine starter entity is in it, and
        # every one of these orphaned duplicates (confirmed across 68
        # machines + 5 mining drills in the Pyanodons DB) is absent from it.
        # Treat absence from `names` as "not a real item" rather than
        # "freely buildable".
        name_row = await db.fetch_one("SELECT 1 FROM names WHERE name = ?", (item_name,))
        return bool(name_row), []

    for br in build_recs:
        if br["enabled"] or br["db_unlocked"] or br["name"] in extra_unlocked:
            return True, []

    missing = await _techs_unlocking([br["name"] for br in build_recs])
    return False, missing


async def _pick_producer(
    item_id: str,
    *,
    exclude_cats: frozenset[str],
    stop_cats: frozenset[str],
    prefer_enabled: bool,
    overrides: dict[str, str],
    extra_unlocked: frozenset[str] = frozenset(),
    enforce_tech: bool = False,
) -> tuple[dict | None, list[dict], list[str], str]:
    """Select the best recipe that produces `item_id`.

    Returns (chosen | None, all_candidate_dicts, selection_notes, leaf_reason).
    `leaf_reason` is meaningful only when chosen is None:
      "no_recipe"      — nothing produces this item
      "excluded"       — all producers are in exclude_categories
      "stop_category"  — all non-excluded producers are in stop_categories
      "tech_locked"    — all non-excluded/non-stop producers need research
                          that isn't done yet (only when enforce_tech=True)
    """
    rows = await db.fetch_all(
        """
        SELECT r.name, r.translated_name, r.category, r.enabled, r.energy, r.main_product,
               p.amount AS out_amount, p.amount_min, p.amount_max, p.probability,
               EXISTS(
                   SELECT 1 FROM technology_recipe_unlocks tru
                   JOIN technologies t ON t.name = tru.tech_name
                   WHERE tru.recipe_name = r.name AND t.researched = 1
               ) AS db_unlocked
        FROM recipe_products p
        JOIN recipes r ON r.name = p.recipe_name
        WHERE p.item_name = ?
        ORDER BY r.enabled DESC, r.name ASC
        """,
        (item_id,),
    )

    if not rows:
        return None, [], [f"{item_id}: no producer recipe found"], "no_recipe"

    notes: list[str] = []

    # annotate each candidate
    for c in rows:
        c["_selected"] = False
        tech_ok = bool(c["enabled"]) or bool(c["db_unlocked"]) or c["name"] in extra_unlocked
        if c["category"] in exclude_cats:
            c["_tag"] = "excluded"
            c["_tag_reason"] = f"category {c['category']} is excluded"
        elif c["category"] in stop_cats:
            c["_tag"] = "stop_category"
            c["_tag_reason"] = f"category {c['category']} is a stop category"
        elif enforce_tech and not tech_ok:
            c["_tag"] = "tech_locked"
            c["_tag_reason"] = "requires research not yet completed"
        else:
            c["_tag"] = "available"
            c["_tag_reason"] = ""

    # ── manual override ──────────────────────────────────────────────────────
    if item_id in overrides:
        override_id = overrides[item_id]
        match = next((c for c in rows if c["name"] == override_id), None)
        if match:
            match["_selected"] = True
            notes.append(f"{item_id}: using recipe '{override_id}' (manual recipe_overrides)")
            return match, rows, notes, ""
        notes.append(f"{item_id}: override '{override_id}' not found; falling back to auto-select")

    # ── partition candidates ─────────────────────────────────────────────────
    available = [c for c in rows if c["_tag"] == "available"]
    stop_only = [c for c in rows if c["_tag"] == "stop_category"]
    tech_locked = [c for c in rows if c["_tag"] == "tech_locked"]

    if not available and not stop_only and not tech_locked:
        skipped = {c["category"] for c in rows}
        notes.append(
            f"{item_id}: all {len(rows)} producer(s) excluded "
            f"(categories: {', '.join(sorted(skipped))})"
        )
        return None, rows, notes, "excluded"

    if not available and stop_only:
        # Only stop-category producers remain
        cats = {c["category"] for c in stop_only}
        notes.append(
            f"{item_id}: only stop-category producer(s) available "
            f"(categories: {', '.join(sorted(cats))}) — treating as raw"
        )
        return None, rows, notes, "stop_category"

    if not available:
        # Only tech-locked producers remain (stop_only is empty here)
        missing = await _techs_unlocking([c["name"] for c in tech_locked])
        notes.append(
            f"{item_id}: only tech-locked producer(s) available "
            f"(requires: {', '.join(missing) if missing else 'unknown research'}) — treating as raw"
        )
        return None, rows, notes, "tech_locked"

    # ── select best from available ───────────────────────────────────────────
    # Tier 1: recipe whose id matches the item id (e.g. "solder" recipe for "solder" item)
    name_match = next((c for c in available if c["name"] == item_id), None)
    if name_match:
        chosen = name_match
        chosen["_selected"] = True
        notes.append(f"{item_id}: selected recipe '{chosen['name']}' (direct name match)")
        n_skipped = len(rows) - len(available)
        if n_skipped:
            notes.append(f"{item_id}: {n_skipped} producer(s) skipped (excluded/stop)")
        return chosen, rows, notes, ""

    # Tier 1.5: recipe whose main_product is the item — the item is this
    # recipe's actual purpose, not a probabilistic/secondary byproduct of an
    # unrelated refining line (e.g. Pyanodons byproduct-fishing recipes that
    # produce a scarce ore at low probability from bulk waste material).
    # Restricting the pool here still lets Tier 2 below pick among ties.
    main_matches = [c for c in available if c["main_product"] == item_id]
    if main_matches:
        if len(main_matches) < len(available):
            notes.append(
                f"{item_id}: {len(main_matches)} of {len(available)} available producer(s) "
                "have it as their main product, preferring those"
            )
        pool = main_matches
    else:
        pool = available

    # Tier 2: prefer enabled recipes; fall back to first available
    if prefer_enabled:
        enabled_avail = [c for c in pool if c["enabled"]]
        chosen = enabled_avail[0] if enabled_avail else pool[0]
    else:
        chosen = pool[0]

    chosen["_selected"] = True
    reason_parts = []
    if chosen["enabled"]:
        reason_parts.append("enabled")
    if len(pool) > 1:
        reason_parts.append(f"first of {len(pool)} available")
    notes.append(
        f"{item_id}: selected recipe '{chosen['name']}'"
        + (f" ({', '.join(reason_parts)})" if reason_parts else "")
    )
    n_skipped = len(rows) - len(available)
    if n_skipped:
        notes.append(f"{item_id}: {n_skipped} producer(s) skipped (excluded/stop)")

    return chosen, rows, notes, ""


async def _expand_node(
    item_id: str,
    item_type: str,
    amount: float,
    depth: int,
    *,
    max_depth: int,
    exclude_cats: frozenset[str],
    stop_cats: frozenset[str],
    stop_items: frozenset[str],
    prefer_enabled: bool,
    overrides: dict[str, str],
    ancestors: frozenset[str],
    totals_items: dict[str, float],
    totals_fluids: dict[str, float],
    unresolved: list[dict],
    alternates_map: dict[str, list[dict]],
    selection_notes: list[str],
    extra_unlocked: frozenset[str] = frozenset(),
    enforce_tech: bool = False,
) -> dict:
    """Recursively expand one node in the ingredient tree."""
    name_row = await db.fetch_one(
        "SELECT kind, translated_name FROM names WHERE name = ?", (item_id,)
    )
    item_name = name_row["translated_name"] if name_row else item_id
    kind = name_row["kind"] if name_row else item_type

    base: dict = {"id": item_id, "name": item_name, "amount": amount, "kind": kind}

    # ── stop conditions (checked before recipe lookup) ───────────────────────
    if item_id in stop_items:
        _tally(item_id, kind, amount, totals_items, totals_fluids)
        return {**base, "leaf": True, "stop_reason": "stop_item"}

    if item_id in ancestors:
        return {**base, "leaf": True, "stop_reason": "cycle"}

    if depth >= max_depth:
        _tally(item_id, kind, amount, totals_items, totals_fluids)
        unresolved.append({"id": item_id, "name": item_name, "reason": "max_depth"})
        return {**base, "leaf": True, "stop_reason": "max_depth"}

    # ── pick producer recipe ─────────────────────────────────────────────────
    chosen, candidates, notes, leaf_reason = await _pick_producer(
        item_id,
        exclude_cats=exclude_cats,
        stop_cats=stop_cats,
        prefer_enabled=prefer_enabled,
        overrides=overrides,
        extra_unlocked=extra_unlocked,
        enforce_tech=enforce_tech,
    )
    selection_notes.extend(notes)

    if candidates:
        alternates_map[item_id] = [
            {
                "id": c["name"],
                "name": c["translated_name"],
                "category": c["category"],
                "enabled": bool(c["enabled"]),
                "selected": bool(c.get("_selected")),
                "tag": c.get("_tag", "available"),
                "tag_reason": c.get("_tag_reason", ""),
            }
            for c in candidates
        ]

    if chosen is None:
        stop_reason = leaf_reason if leaf_reason else "no_recipe"
        _tally(item_id, kind, amount, totals_items, totals_fluids)
        if stop_reason == "tech_locked":
            unresolved.append({"id": item_id, "name": item_name, "reason": "tech_locked"})
        return {**base, "leaf": True, "stop_reason": stop_reason}

    # ── scale by batches ─────────────────────────────────────────────────────
    eff_out = _effective_out(chosen)
    batches = amount / eff_out

    # ── fetch and recurse into ingredients ───────────────────────────────────
    ings = await db.fetch_all(
        """
        SELECT i.item_name, i.item_type, i.amount,
               COALESCE(n.translated_name, i.item_name) AS display_name
        FROM recipe_ingredients i
        LEFT JOIN names n ON n.name = i.item_name
        WHERE i.recipe_name = ?
        ORDER BY i.position
        """,
        (chosen["name"],),
    )

    new_ancestors = ancestors | {item_id}
    children = []
    for ing in ings:
        child = await _expand_node(
            ing["item_name"],
            ing["item_type"],
            float(ing["amount"]) * batches,
            depth + 1,
            max_depth=max_depth,
            exclude_cats=exclude_cats,
            stop_cats=stop_cats,
            stop_items=stop_items,
            prefer_enabled=prefer_enabled,
            overrides=overrides,
            ancestors=new_ancestors,
            totals_items=totals_items,
            totals_fluids=totals_fluids,
            unresolved=unresolved,
            alternates_map=alternates_map,
            selection_notes=selection_notes,
            extra_unlocked=extra_unlocked,
            enforce_tech=enforce_tech,
        )
        children.append(child)

    return {
        **base,
        "leaf": False,
        "recipe": {
            "id": chosen["name"],
            "name": chosen["translated_name"],
            "category": chosen["category"],
            "batches": batches,
            "output_per_batch": eff_out,
            "energy": float(chosen.get("energy") or 0.0),
        },
        "ingredients": children,
    }


def _collect_recipe_nodes(tree: dict) -> list[tuple[str, float, float]]:
    """Walk the expansion tree and collect (category, batches_per_min, energy_sec) tuples."""
    nodes: list[tuple[str, float, float]] = []
    if not tree.get("leaf") and "recipe" in tree:
        r = tree["recipe"]
        nodes.append((r["category"], r["batches"], float(r.get("energy") or 0.0)))
        for child in tree.get("ingredients", []):
            nodes.extend(_collect_recipe_nodes(child))
    return nodes


async def plan_product(
    product: str,
    rate_per_min: float = 60.0,
    max_depth: int = 6,
    available_machines: list[str] | None = None,
    assume_researched: list[str] | None = None,
    only_enabled: bool = False,
    stop_items: list[str] | None = None,
    exclude_categories: list[str] | None = None,
    recipe_overrides: dict[str, str] | None = None,
    auto_stop_raw: bool = True,
) -> dict:
    """Design a factory block to produce a product at a given rate per minute.

    Expands the recipe chain for the product, then for each recipe step picks
    the fastest eligible crafting machine and computes how many you need.
    Returns a buildings bill, a raw-inputs list (resources to bring in), and
    approximate drill counts for any mineable raw inputs.

    Tech-level filtering: pass available_machines (machine ids you have built),
    assume_researched (tech ids you plan to research), or only_enabled (restrict
    to currently enabled build recipes). When either assume_researched or
    only_enabled is set, this governs not just which crafting machine gets
    picked but also which recipe is chosen at each step of the chain and which
    mining drill is picked for raw inputs — a recipe/drill that needs research
    you haven't (assumed to have) done is skipped in favor of one you can
    actually build, or the item falls back to a raw input if nothing you can
    build produces it. Re-run with different filters to model different
    tech-level scenarios.

    Args:
        product: Item or fluid id or translated name.
        rate_per_min: Target production rate in items per minute (default 60).
        max_depth: Recipe chain depth limit (default 6, max 15).
        available_machines: If set, only use machines in this list.
        assume_researched: Tech ids to treat as if researched when checking
            recipe/machine/drill build availability.
        only_enabled: When True, only use recipes/machines/drills whose build
            recipe is currently enabled or unlocked by a researched technology.
        stop_items: Items to treat as raw inputs (halt expansion there).
        exclude_categories: Recipe categories to skip entirely.
        recipe_overrides: Force a specific recipe per item {item_id: recipe_id}.
        auto_stop_raw: Also stop at real mineable/harvestable map resources
            (see _auto_raw_items) so a recipe-DB byproduct chain never gets
            picked over "this is just mined" — on by default; set False to
            see the full expansion anyway.
    """
    max_depth = max(1, min(max_depth, 15))
    exclude_cats = frozenset(
        exclude_categories if exclude_categories is not None else _DEFAULT_EXCLUDE
    )
    stop_set = frozenset(stop_items or [])
    if auto_stop_raw:
        stop_set = stop_set | await _auto_raw_items()
    overrides: dict[str, str] = dict(recipe_overrides or {})
    avail_set: set[str] | None = set(available_machines) if available_machines is not None else None

    extra_unlocked = await unlocked_recipes_for_techs(assume_researched)
    enforce_tech = only_enabled or bool(extra_unlocked)

    # Resolve product name
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

    # Expand recipe chain with rate_per_min as amount so batches = batches/min
    totals_items: dict[str, float] = {}
    totals_fluids: dict[str, float] = {}
    unresolved: list[dict] = []
    alternates_map: dict[str, list[dict]] = {}
    selection_notes: list[str] = []

    tree = await _expand_node(
        item_id,
        item_kind,
        rate_per_min,
        depth=0,
        max_depth=max_depth,
        exclude_cats=exclude_cats,
        stop_cats=frozenset(),
        stop_items=stop_set,
        prefer_enabled=True,
        overrides=overrides,
        ancestors=frozenset(),
        totals_items=totals_items,
        totals_fluids=totals_fluids,
        unresolved=unresolved,
        alternates_map=alternates_map,
        selection_notes=selection_notes,
        extra_unlocked=extra_unlocked,
        enforce_tech=enforce_tech,
    )

    # Walk tree to collect (category, batches_per_min, energy_sec) for each step
    recipe_nodes = _collect_recipe_nodes(tree)

    # For each step, pick fastest eligible machine and compute count
    buildings: dict[str, dict] = {}
    blocked: list[dict] = []

    for category, batches_per_min, energy_sec in recipe_nodes:
        machine_rows = await db.fetch_all(
            """SELECT m.name, m.translated_name, m.crafting_speed
               FROM machines m
               JOIN machine_crafting_categories mcc ON mcc.machine_name = m.name
               WHERE mcc.category = ?
               ORDER BY m.crafting_speed DESC""",
            (category,),
        )

        # Filter by available_machines
        if avail_set is not None:
            machine_rows = [m for m in machine_rows if m["name"] in avail_set]

        # Filter by build-recipe eligibility
        if enforce_tech:
            eligible = []
            for m in machine_rows:
                ok, _missing = await _is_item_buildable(m["name"], extra_unlocked)
                if ok:
                    eligible.append(m)
            machine_rows = eligible

        if not machine_rows:
            blocked.append(
                {
                    "category": category,
                    "batches_per_min": batches_per_min,
                    "reason": "no eligible machine (check available_machines / tech level)",
                }
            )
            continue

        chosen = machine_rows[0]  # fastest first
        speed = float(chosen["crafting_speed"]) or 1.0
        # count = batches_per_min × energy_sec / (60 × speed)
        count = math.ceil(batches_per_min * energy_sec / (60.0 * speed)) if energy_sec > 0 else 1

        mid = chosen["name"]
        if mid not in buildings:
            buildings[mid] = {
                "id": mid,
                "name": chosen["translated_name"],
                "crafting_speed": speed,
                "count": 0,
            }
        buildings[mid]["count"] += count

    # Approximate drill counts for mineable raw inputs. A product can have more
    # than one extraction path (e.g. Pyanodons' ore-tin is mineable both as a
    # steam-fed "basic-with-fluid" patch and as a fluid-free "tin-rock" deposit)
    # — surface every path rather than silently picking one, since which is
    # actually buildable depends on what resource patches are on the map.
    drills: list[dict] = []
    blocked_drills: list[dict] = []
    # Items and fluids share one id namespace (never collide), so a raw fluid
    # input (e.g. geothermal-water) needs the same drill/resource lookup a
    # raw item does — checking only totals_items silently dropped every
    # fluid raw input from the drills/blocked_drills output.
    for ore_id, amount_per_min in itertools.chain(totals_items.items(), totals_fluids.items()):
        res_rows = await db.fetch_all(
            "SELECT resource_category, mining_time, required_fluid, fluid_amount "
            "FROM resources WHERE product_name = ?",
            (ore_id,),
        )
        for res_row in res_rows:
            rc = res_row["resource_category"]
            mining_time = float(res_row["mining_time"] or 1.0)
            drill_rows = await db.fetch_all(
                """SELECT md.name, md.translated_name, md.mining_speed
                   FROM mining_drills md
                   JOIN drill_resource_categories drc ON drc.drill_name = md.name
                   WHERE drc.resource_category = ?
                   ORDER BY md.mining_speed DESC""",
                (rc,),
            )
            if not drill_rows:
                continue

            drill = None
            if enforce_tech:
                for candidate in drill_rows:
                    ok, missing = await _is_item_buildable(candidate["name"], extra_unlocked)
                    if ok:
                        drill = candidate
                        break
                    blocked_drills.append(
                        {
                            "resource": ore_id,
                            "resource_category": rc,
                            "drill_id": candidate["name"],
                            "drill_name": candidate["translated_name"],
                            "requires_research": missing,
                        }
                    )
            else:
                drill = drill_rows[0]

            if drill is None:
                continue

            mining_speed = float(drill["mining_speed"]) or 1.0
            # drills = (amount_per_min / 60) / (mining_speed / mining_time)
            drill_count = math.ceil(amount_per_min * mining_time / (60.0 * mining_speed))
            required_fluid = res_row["required_fluid"]
            # One resource unit is produced per mining cycle (same assumption
            # drill_count already makes), so fluid-per-unit-product ==
            # fluid_amount/10 and the rate scales directly with amount_per_min.
            # The raw prototype field is exactly 10x the real per-operation
            # consumption -- confirmed empirically in-game across four
            # different Pyanodons ores (lead 100->10, zinc 40->4, chromium
            # 40->4, titanium 40->4, checked via each entity's own tooltip/
            # production-graph rate), and explained by Factorio dev Rseding91:
            # "A 'mining operation' on a drill is 10 ores so it requires 0.1
            # fluid for 10 ores. On the ore itself it requires only 0.1 / 10
            # fluid per individual ore" -- the field is defined per 10-ore
            # batch, not per single mining operation.
            # https://forums.factorio.com/viewtopic.php?p=688574
            fluid_rate_per_min = (
                amount_per_min * float(res_row["fluid_amount"] or 0.0) / 10.0
                if required_fluid
                else 0.0
            )
            drills.append(
                {
                    "resource": ore_id,
                    "resource_category": rc,
                    "drill_id": drill["name"],
                    "drill_name": drill["translated_name"],
                    "drill_count": drill_count,
                    "required_fluid": required_fluid,
                    "fluid_rate_per_min": fluid_rate_per_min,
                    "approximate": True,
                    "note": "Ignores purity and productivity bonuses",
                }
            )

    # A raw input can look identical to a genuinely-mineable one (ore-chromium,
    # raw-coal) while actually having zero currently-buildable extraction
    # drill — e.g. every tar-extractor tier locked behind research not yet
    # done. Recipe-based raw inputs already get a "tech-locked ... treating
    # as raw" note from _pick_producer; a stop_items short-circuit (real
    # mined resources, see _auto_raw_items) skips _pick_producer entirely and
    # would otherwise silently hide this. Surface the same note here so it's
    # not presented as an ordinary, ready-to-use raw input.
    drilled_ids = {d["resource"] for d in drills}
    blocked_ids = {d["resource"] for d in blocked_drills}
    for ore_id in sorted(blocked_ids - drilled_ids):
        missing = sorted(
            {t for d in blocked_drills if d["resource"] == ore_id for t in d["requires_research"]}
        )
        selection_notes.append(
            f"{ore_id}: only tech-locked extraction drill(s) available "
            f"(requires: {', '.join(missing) if missing else 'unknown research'}) — treating as raw"
        )

    # Localized/display names for raw inputs — the internal id (e.g.
    # "ore-quartz") is never what's shown in-game; a consumer cross-checking
    # a plan against the actual game UI needs the translated_name too.
    raw_ids = list(totals_items) + list(totals_fluids)
    raw_names: dict[str, str] = {}
    if raw_ids:
        name_rows = await db.fetch_all(
            f"SELECT name, translated_name FROM names WHERE name IN ({','.join('?' * len(raw_ids))})",
            tuple(raw_ids),
        )
        raw_names = {r["name"]: r["translated_name"] for r in name_rows}

    _raw_entries: list[tuple[float, dict]] = [
        (v, {"id": k, "name": raw_names.get(k, k), "amount_per_min": v, "kind": "item"})
        for k, v in totals_items.items()
    ] + [
        (v, {"id": k, "name": raw_names.get(k, k), "amount_per_min": v, "kind": "fluid"})
        for k, v in totals_fluids.items()
    ]
    raw_inputs = [d for _, d in sorted(_raw_entries, key=lambda t: -t[0])]

    return {
        "product": item_id,
        "rate_per_min": rate_per_min,
        "buildings": sorted(buildings.values(), key=lambda b: -b["count"]),
        "raw_inputs": raw_inputs,
        "drills": drills,
        "blocked_drills": blocked_drills,
        "blocked_categories": blocked,
        "selection_notes": selection_notes,
        "unresolved": list(unresolved),
    }
