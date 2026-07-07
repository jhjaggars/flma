"""flma factory-planner CLI.

Combines recipe-mcp's calculation engine (machine-count math, recipe
expansion — see planner/_recipe_mcp_loader.py) with flma's live game state
(planner/live_state.py) to answer "how do I build a line for X at rate Y,
and what do I already have toward it" — without an MCP server or Hermes.

Usage:
    uv run python -m planner.cli                         # status (default)
    uv run python -m planner.cli options copper-plate    # viable ways to make X
    uv run python -m planner.cli plan "processing unit" --rate 10
    uv run python -m planner.cli expand iron-plate --rate 5
    uv run python -m planner.cli recipe electronic-circuit
    uv run python -m planner.cli producers iron-plate
    uv run python -m planner.cli consumers iron-plate
    uv run python -m planner.cli have iron-plate
    uv run python -m planner.cli belts 2                 # belts -> achievable rate
    uv run python -m planner.cli tech "Copper processing - Stage 1"  # what it unlocks & whether it combines

See .claude/skills/factory-planner/SKILL.md for the workflows this backs, and
CLAUDE.md's factory-planner section for the modpack-alignment caveat.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from collections import Counter
from types import ModuleType

from planner import config, live_state, module_bonus, techbundle, throughput
from planner._recipe_mcp_loader import load_async_database_class, load_engine
from planner.options import classify_producer, deeper_choices, tree_categories, tree_stages
from planner.recommend import rank_candidates


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def _fmt_num(n: float) -> str:
    if abs(n - round(n)) < 1e-6:
        return str(int(round(n)))
    return f"{n:.2f}"


_RATE_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("/second", "per-sec"),
    ("/sec", "per-sec"),
    ("/s", "per-sec"),
    ("/minute", "per-min"),
    ("/min", "per-min"),
    ("/m", "per-min"),
)


def _parse_rate_per_min(raw: str, default_unit: str) -> float:
    """Parse a --rate/--consume rate value into items/min. `raw` may carry
    its own unit suffix (`15/s`, `900/min`, ...) that overrides
    `default_unit` (the command's --unit flag) -- this exists because a bare
    number's unit depends on a separate flag the caller can easily forget to
    set (or a value pasted from this CLI's own `X/s = Y/min` output can't be
    pasted back in as-is), so an explicit suffix should always win. Falls
    back to `default_unit` when `raw` has no suffix, so old-style bare
    numbers keep working unchanged. Raises ValueError (caller should catch
    and report via `_fail`) on anything unparseable."""
    text = raw.strip()
    unit = default_unit
    for suffix, canonical in _RATE_SUFFIXES:
        if text.lower().endswith(suffix):
            text = text[: -len(suffix)]
            unit = canonical
            break
    try:
        value = float(text)
    except ValueError:
        raise ValueError(
            f"rate must be a number, optionally suffixed with /s, /sec, or /min — got {raw!r}"
        ) from None
    return value * 60.0 if unit == "per-sec" else value


def _rate_per_min_or_default(raw: str | None, unit: str, default: float = 60.0) -> float:
    """`_parse_rate_per_min`, but returns `default` (60/min) when no --rate
    was given at all -- the common `rate_per_min = 60.0 if args.rate is
    None else ...` pattern shared by `options`/`tech`/`recommend`."""
    return default if raw is None else _parse_rate_per_min(raw, unit)


def _rate_hint(amount: float | None, probability: float | None, craft_time: float | None) -> str:
    """`amount`x is per craft, not per second — appending the actual
    per-machine rate heads off reading the per-craft batch size as a --rate
    value (a craft-time-blind mistake this eval caught)."""
    if amount is None or not craft_time:
        return ""
    per_sec = amount * (probability if probability is not None else 1.0) / craft_time
    return f"  ({_fmt_num(per_sec)}/s per machine)"


def _parse_recipe_overrides(raw: str | None) -> dict[str, str]:
    """Parse --recipe's `item=recipe[,item2=recipe2,...]` into a dict."""
    if not raw:
        return {}
    overrides: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        item_id, sep, recipe_id = pair.partition("=")
        if not sep:
            print(
                f"warning: --recipe entry '{pair}' must be ITEM=RECIPE, skipping", file=sys.stderr
            )
            continue
        overrides[item_id.strip()] = recipe_id.strip()
    return overrides


def _make_engine() -> ModuleType | None:
    """Load recipe-mcp's engine module and point it at our recipes.db.
    Returns None (after printing an error) if the DB hasn't been built yet."""
    if not config.RECIPES_DB.exists():
        print(
            f"error: recipes.db not found at {config.RECIPES_DB}\n"
            f"  build it first: cd {config.RECIPE_MCP_DIR} && make build-db\n"
            f"  (or set RECIPES_DB / RECIPE_MCP_DIR if the homelab checkout is elsewhere)",
            file=sys.stderr,
        )
        return None
    async_database_cls = load_async_database_class(config.RECIPE_MCP_DIR)
    engine = load_engine(config.RECIPE_MCP_DIR)
    engine.set_db(async_database_cls(str(config.RECIPES_DB)))
    return engine


async def _db_tech_ids(engine: ModuleType) -> set[str]:
    rows = await engine.db.fetch_all("SELECT name FROM technologies")
    return {r["name"] for r in rows}


async def _resolve_item(engine: ModuleType, query: str) -> tuple[dict | None, list[dict]]:
    """Resolve an item/fluid id or human name. Returns (row, []) on a clean
    match, (None, candidates) when ambiguous or unmatched."""
    db = engine.db
    row = await db.fetch_one(
        "SELECT name, kind, translated_name FROM names WHERE name = ?", (query,)
    )
    if row is not None:
        return row, []
    row = await db.fetch_one(
        "SELECT name, kind, translated_name FROM names WHERE translated_name = ? COLLATE NOCASE",
        (query,),
    )
    if row is not None:
        return row, []
    fuzzy = await db.fetch_all(
        """SELECT name, kind, translated_name FROM names
           WHERE name LIKE ? COLLATE NOCASE OR translated_name LIKE ? COLLATE NOCASE
           ORDER BY translated_name COLLATE NOCASE LIMIT 10""",
        (f"%{query}%", f"%{query}%"),
    )
    if len(fuzzy) == 1:
        return fuzzy[0], []
    return None, fuzzy


def _print_ambiguous(query: str, candidates: list[dict]) -> int:
    """Print ambiguous-match candidates. Two shapes flow through here:
    `_resolve_item`'s raw `names`-table rows ({name, kind, translated_name},
    `name`=internal id) and `engine.plan_product`'s ambiguous dicts
    ({id, kind, name}, `name`=translated display text). Handle both rather
    than assuming one."""
    if not candidates:
        print(f"no item or fluid found matching '{query}'.")
        return 1
    print(f"'{query}' is ambiguous — candidates:")
    for c in candidates:
        item_id = c.get("id", c.get("name"))
        display = c.get("translated_name", c.get("name", ""))
        print(f"  {item_id:<30} ({c.get('kind', '')})  {display}")
    print("\nnext: re-run with the exact id from the list above.")
    return 1


async def _resolve_tech(engine: ModuleType, query: str) -> tuple[dict | None, list[dict]]:
    """Resolve a technology id or human name — mirrors `_resolve_item` above,
    but against the `technologies` table instead of `names`. Returns
    (row, []) on a clean match, (None, candidates) when ambiguous/unmatched."""
    db = engine.db
    row = await db.fetch_one(
        "SELECT name, translated_name FROM technologies WHERE name = ?", (query,)
    )
    if row is not None:
        return row, []
    row = await db.fetch_one(
        "SELECT name, translated_name FROM technologies WHERE translated_name = ? COLLATE NOCASE",
        (query,),
    )
    if row is not None:
        return row, []
    fuzzy = await db.fetch_all(
        """SELECT name, translated_name FROM technologies
           WHERE name LIKE ? COLLATE NOCASE OR translated_name LIKE ? COLLATE NOCASE
           ORDER BY translated_name COLLATE NOCASE LIMIT 10""",
        (f"%{query}%", f"%{query}%"),
    )
    if len(fuzzy) == 1:
        return fuzzy[0], []
    return None, fuzzy


def _print_ambiguous_tech(query: str, candidates: list[dict]) -> int:
    if not candidates:
        print(f"no technology found matching '{query}'.")
        return 1
    print(f"'{query}' is ambiguous — candidates:")
    for c in candidates:
        print(f"  {c['name']:<32} {c['translated_name']}")
    print("\nnext: re-run with the exact id/name from the list above.")
    return 1


async def _fastest_eligible_machine(
    engine: ModuleType,
    category: str,
    *,
    extra_unlocked: frozenset[str],
    enforce_tech: bool,
) -> dict | None:
    """Fastest machine that can build recipes in `category`, tech-filtered
    the same way `plan_product`'s own machine-count pass is — factored out of
    `_one_machine_rate_per_min` so `options` can look up a category's fastest
    machine without first picking a specific recipe (it needs this per
    *candidate* recipe, not just the auto-picked one). Returns None if no
    eligible machine exists."""
    raw_rows = await engine.db.fetch_all(
        """SELECT m.name, m.translated_name, m.crafting_speed, m.module_slots
           FROM machines m
           JOIN machine_crafting_categories mcc ON mcc.machine_name = m.name
           WHERE mcc.category = ?""",
        (category,),
    )
    # Apply module_bonus before sorting/picking "fastest" -- for Moondrop
    # greenhouses/Auog paddocks this can change which machine in a category
    # is actually fastest once modules are assumed, not just the resulting
    # count.
    machine_rows: list[dict] = [
        {
            "name": r["name"],
            "translated_name": r["translated_name"],
            "crafting_speed": module_bonus.effective_speed(
                r["name"], float(r["crafting_speed"]), int(r["module_slots"] or 0)
            ),
        }
        for r in raw_rows
    ]
    machine_rows.sort(key=lambda m: m["crafting_speed"], reverse=True)
    if enforce_tech:
        eligible = []
        for m in machine_rows:
            ok, _missing = await engine._is_item_buildable(m["name"], extra_unlocked)
            if ok:
                eligible.append(m)
        machine_rows = eligible
    if not machine_rows:
        return None
    result: dict = machine_rows[0]
    return result


async def _fastest_buildable_belt_tier(
    engine: ModuleType, extra_unlocked: frozenset[str]
) -> str | None:
    """Fastest belt tier (see throughput.BELT_THROUGHPUT_ITEMS_PER_SEC) that
    both exists in this modpack's recipe DB and is currently buildable —
    same tech-scoping `_fastest_eligible_machine` uses, applied to a fixed
    list of belt item ids instead of a DB-driven machine query. Checks
    `names` before trusting `_is_item_buildable`: that function treats "no
    build recipe at all" as "always available" (correct for bare starter
    entities), which would otherwise misreport a tier that doesn't exist in
    this modpack at all (e.g. Space Age's turbo-transport-belt in a
    Pyanodons DB) as buildable. Returns None if no known tier both exists
    and is buildable, so the caller falls back to the static default."""
    for tier_id, _speed in sorted(
        throughput.BELT_THROUGHPUT_ITEMS_PER_SEC.items(), key=lambda kv: kv[1], reverse=True
    ):
        exists = await engine.db.fetch_one("SELECT 1 FROM names WHERE name = ?", (tier_id,))
        if not exists:
            continue
        ok, _missing = await engine._is_item_buildable(tier_id, extra_unlocked)
        if ok:
            return tier_id
    return None


async def _one_machine_rate_per_min(
    engine: ModuleType,
    item_id: str,
    *,
    extra_unlocked: frozenset[str],
    enforce_tech: bool,
    overrides: dict[str, str],
) -> tuple[float, dict] | None:
    """Rate (items/min) produced by exactly one instance of the fastest
    eligible machine for item_id's auto-picked recipe. This is what `--rate`
    defaults to when omitted — "just build one" is the natural starting
    point for a "basic setup" ask, whereas an arbitrary flat rate (e.g. the
    old default of 1/s) can demand dozens of machines for a slow recipe
    (Pyanodons fish farms: ~130 machines for 1 fish/sec) or round up to a
    full machine for a recipe that's nearly idle at that rate — neither
    reflects "one machine's worth". Returns None if no eligible
    recipe/machine exists (caller falls back to erroring out with a
    prompt to pass --rate explicitly)."""
    chosen, _candidates, _notes, _reason = await engine._pick_producer(
        item_id,
        exclude_cats=engine._DEFAULT_EXCLUDE,
        stop_cats=frozenset(),
        prefer_enabled=True,
        overrides=overrides,
        extra_unlocked=extra_unlocked,
        enforce_tech=enforce_tech,
    )
    if chosen is None:
        return None
    energy = float(chosen.get("energy") or 0.0)
    if energy <= 0:
        return None
    eff_out = engine._effective_out(chosen)

    machine = await _fastest_eligible_machine(
        engine, chosen["category"], extra_unlocked=extra_unlocked, enforce_tech=enforce_tech
    )
    if machine is None:
        return None
    speed = float(machine["crafting_speed"]) or 1.0
    # Shave a hair off the exact boundary so the machine-count math downstream
    # (which does math.ceil(batches_per_min * energy / (60 * speed))) lands on
    # exactly 1 machine instead of 2 from floating-point overshoot.
    rate_per_min = eff_out / energy * speed * 60.0 * (1 - 1e-9)
    return rate_per_min, {
        "recipe": chosen["name"],
        "category": chosen["category"],
        "machine_id": machine["name"],
        "machine_name": machine["translated_name"],
        "speed": speed,
    }


def _is_tech_locked(item_id: str, tech_locked_notes: list[str]) -> bool:
    """Whether `item_id` has a tech-locked selection note. Exact-prefix
    match, not substring — notes read "{item_id}: ...", and a naive
    substring check would false-match e.g. "water" inside a
    "geothermal-water: ..." note."""
    return any(n.startswith(f"{item_id}: ") for n in tech_locked_notes)


async def _tech_status_labels(
    engine: ModuleType, rows: list[dict], extra_unlocked: frozenset[str], aligned: bool
) -> dict[str, str]:
    """Per-recipe-name live tech-scoped status tag for display, replacing the
    DB's own static `enabled` snapshot (built at db-build time, not your
    save's research progress) — the exact gap that once led an agent (and
    me, earlier in this investigation) to reject the correct 'battery-mk01'
    recipe as "[disabled]" and pick a worse alternate instead, since
    `producers`/`recipe` never cross-referenced live research the way
    `plan`/`expand` already do internally. '' for a recipe needing no
    research at all; '[researched]' for one gated behind research you've
    *already done*; '[needs: X, Y]' for one you haven't unlocked yet.
    '[disabled, no tracked tech unlocks it]' is a distinct, worse case:
    disabled with ZERO rows in technology_recipe_unlocks — not "research
    something and it appears", just permanently disabled/orphaned as far as
    the DB can tell (the exact trap 'battery' turned out to be: it looked
    like a safe, ungated starter recipe for battery-mk01, but it's actually
    just unavailable, full stop). Empty for every recipe when live
    tech-scoping isn't available (unaligned modpack — same as `plan`/
    `expand`'s own fallback)."""
    if not aligned:
        return {}
    locked = [r["name"] for r in rows if not r["enabled"] and r["name"] not in extra_unlocked]
    needs: dict[str, list[str]] = {}
    if locked:
        tech_rows = await engine.db.fetch_all(
            f"""SELECT tru.recipe_name, t.translated_name FROM technology_recipe_unlocks tru
                JOIN technologies t ON t.name = tru.tech_name
                WHERE tru.recipe_name IN ({",".join("?" * len(locked))})""",
            tuple(locked),
        )
        for trow in tech_rows:
            needs.setdefault(trow["recipe_name"], []).append(trow["translated_name"])

    labels: dict[str, str] = {}
    for r in rows:
        name = r["name"]
        if r["enabled"]:
            labels[name] = ""
        elif name in extra_unlocked:
            labels[name] = "  [researched]"
        elif name in needs:
            labels[name] = f"  [needs: {', '.join(sorted(needs[name]))}]"
        else:
            labels[name] = "  [disabled, no tracked tech unlocks it]"
    return labels


def _collect_intermediate_ids(node: dict, out: set[str]) -> None:
    """Walk an `_expand_node` tree collecting every non-leaf item id — the
    intermediate products (chromite-sand, limestone, creosote, ...) that
    `plan_product`'s flattened `raw_inputs` bill has no visibility into,
    since they're fully consumed inside the chain rather than surfacing as
    raw inputs."""
    if node.get("leaf"):
        return
    out.add(node["id"])
    for child in node.get("ingredients", []):
        _collect_intermediate_ids(child, out)


def _print_tree(
    node: dict,
    indent: int = 0,
    *,
    alternates_map: dict[str, list[dict]] | None = None,
    show_alternates: bool = False,
) -> None:
    pad = "  " * indent
    if node.get("leaf"):
        reason = node.get("stop_reason", "")
        print(f"{pad}{node['id']} ({node['name']})  {_fmt_num(node['amount'])}  [{reason}]")
        return
    r = node["recipe"]
    print(
        f"{pad}{node['id']} ({node['name']})  {_fmt_num(node['amount'])}"
        f"  <- {r['id']} ({r['name']}) x{_fmt_num(r['batches'])}"
    )
    if show_alternates and alternates_map is not None:
        # Every OTHER candidate recipe that could have produced this node,
        # per the same alternates_map `_expand_node` already builds and every
        # other caller discards — tagged with the engine's own tag/tag_reason
        # rather than re-deriving tech status, since that's already computed
        # for this exact call's tech-scoping.
        for alt in alternates_map.get(node["id"], []):
            if alt.get("selected"):
                continue
            reason = f": {alt['tag_reason']}" if alt.get("tag_reason") else ""
            print(f"{pad}    alt: {alt['id']} ({alt['name']})  [{alt.get('tag', '')}{reason}]")
    for child in node.get("ingredients", []):
        _print_tree(
            child, indent + 1, alternates_map=alternates_map, show_alternates=show_alternates
        )


# ---------------------------------------------------------------------------
# status — modpack/live-data health check; also the no-args default
# ---------------------------------------------------------------------------


async def cmd_status(args: argparse.Namespace) -> int:
    engine = _make_engine()
    if engine is None:
        return 1
    gs = live_state.open_game_state(config.SCRIPT_OUTPUT_DIR)
    db_tech_ids = await _db_tech_ids(engine)
    align = live_state.modpack_alignment(gs, db_tech_ids, force=args.force)
    ages = gs.snapshot_ages()
    tech = gs.get_tech()
    force_data = tech.get("forces", {}).get(args.force, {})

    print(f"recipes.db     : {config.RECIPES_DB}  ({align['db_tech_count']} technologies)")
    print(f"flma live data : {config.SCRIPT_OUTPUT_DIR}")
    for name, age in ages.items():
        age_str = "never (mod not running / export disabled?)" if age is None else f"{age:.0f}s ago"
        print(f"  {name:<12}: {age_str}")
    print(
        f"force '{args.force}'   : {align['live_tech_count']} technologies known, "
        f"current research: {force_data.get('current_research') or '(idle)'}"
    )
    print()
    if align["aligned"]:
        print(
            f"modpack alignment: OK "
            f"({align['overlap_count']}/{align['live_tech_count']} live techs found in recipes.db)"
        )
        print("`plan`/`have` live-scoping and netting are meaningful for this save.")
    else:
        print(
            f"modpack alignment: MISMATCH "
            f"({align['overlap_count']}/{align['live_tech_count']} live techs found in recipes.db)"
        )
        print(
            "recipes.db appears to describe a different modpack than the live save.\n"
            "`plan`/`have` will still run, but live-scoping and netting annotations\n"
            "will be empty rather than wrong — see CLAUDE.md's factory-planner section."
        )
    if not throughput.VALUES_ARE_PYANODONS_ACCURATE:
        print(
            "\nbelt/pipe throughput constants are base/Space Age placeholders — see planner/throughput.py"
        )
    print(
        "\nnext: `recommend <product>` for the single best way to make something right now; "
        "`plan <product> --rate <n>` to design a line; `have <item>` to check current production."
    )
    return 0


# ---------------------------------------------------------------------------
# plan — the headline command
# ---------------------------------------------------------------------------


async def _apply_module_bonus_to_buildings(engine: ModuleType, buildings: list[dict]) -> list[dict]:
    """Post-process `plan_product`'s aggregated building counts for the
    module-accelerated families in module_bonus.py. plan_product's own
    per-category machine selection (recipe-mcp's engine.py, not
    reimplemented here) uses raw crafting_speed with no module assumption.
    Its building counts are already summed across every recipe step using
    that machine, each step's own math.ceil already applied — recovering the
    *exact* post-module count would mean re-walking the recipe tree
    ourselves. Dividing the aggregated (already-rounded-up) count by the
    same multiplier and rounding up again is a very close approximation, off
    by at most the number of distinct recipe steps sharing that machine
    (typically 1 for these two families) — a tiny margin next to the
    5-17x raw-speed overcount it corrects."""
    ids = [b["id"] for b in buildings if module_bonus.is_module_accelerated(b["id"])]
    if not ids:
        return buildings
    rows = await engine.db.fetch_all(
        f"SELECT name, module_slots FROM machines WHERE name IN ({','.join('?' * len(ids))})",
        tuple(ids),
    )
    slots_by_id = {r["name"]: int(r["module_slots"] or 0) for r in rows}
    adjusted: list[dict] = []
    for b in buildings:
        slots = slots_by_id.get(b["id"], 0)
        if slots > 0 and module_bonus.is_module_accelerated(b["id"]):
            multiplier = 1.0 + slots * module_bonus.MODULE_SPEED_BONUS_PER_SLOT
            adjusted.append(
                {
                    **b,
                    "crafting_speed": b["crafting_speed"] * multiplier,
                    "count": math.ceil(b["count"] / multiplier),
                    "module_accelerated": True,
                }
            )
        else:
            adjusted.append(b)
    return sorted(adjusted, key=lambda b: -b["count"])


_CAP_REFERENCE_RATE_PER_MIN = 60.0


async def _rate_for_belt_cap(
    engine: ModuleType, product_id: str, max_depth: int, scoping: dict, cap_count: float
) -> tuple[float, dict] | None:
    """Solve for the rate_per_min where the raw input needing the most
    logistics capacity (at a reference rate) needs exactly `cap_count`
    belts/pipes -- the inverse of `plan`'s normal "pick a rate, see what it
    needs" flow, for sizing against logistics capacity instead of an
    arbitrary output target. A plan_product result's raw_inputs scale
    linearly with rate_per_min for a fixed recipe selection (`_expand_node`'s
    batch math has no rounding; only the final machine-count pass ceils), so
    one reference call is enough to find the scale factor -- no need to
    search/iterate. Uses throughput.capacity_needed (belts for items, pipes
    for fluids -- see its docstring for why conflating the two badly skews
    which raw input looks like "the bottleneck") the same way the `raw
    inputs` section already prints, so the count reported here matches what
    `plan` will show at the resulting rate. Returns None if the product has
    no raw inputs to size a cap against (e.g. a fully closed loop)."""
    reference = await engine.plan_product(
        product_id, rate_per_min=_CAP_REFERENCE_RATE_PER_MIN, max_depth=max_depth, **scoping
    )
    raw_inputs = reference.get("raw_inputs") or []
    if not raw_inputs:
        return None

    def _capacity(r: dict) -> float:
        return float(throughput.capacity_needed(r["amount_per_min"] / 60.0, r["kind"])["count"])

    bottleneck = max(raw_inputs, key=_capacity)
    bottleneck_capacity = _capacity(bottleneck)
    if bottleneck_capacity <= 0:
        return None
    rate_per_min = _CAP_REFERENCE_RATE_PER_MIN * (cap_count / bottleneck_capacity)
    unit_plural = throughput.capacity_needed(
        bottleneck["amount_per_min"] / 60.0, bottleneck["kind"]
    )["unit_plural"]
    return rate_per_min, {
        "bottleneck_id": bottleneck["id"],
        "bottleneck_name": bottleneck["name"],
        "unit_plural": unit_plural,
    }


async def _plan_reuse_candidates(
    engine: ModuleType,
    args: argparse.Namespace,
    gs: live_state.GameState,
    align: dict,
    scoping: dict,
    recipe_overrides: dict[str, str],
    rate_per_min: float,
    result: dict,
    net: dict[str, float],
    stock: dict[str, int],
) -> tuple[list[tuple[str, float | None, int]], list[tuple[str, int, int]]]:
    """Existing production (for intermediate items in the chain, not just
    the flattened raw_inputs) and existing buildings (live counts of machine
    types the plan calls for) — reuse candidates to surface before
    recommending fresh capacity. Empty when live state isn't aligned."""
    if not align["aligned"]:
        return [], []
    row, _candidates = await _resolve_item(engine, args.product)
    if row is None:
        return [], []
    extra_unlocked = await engine.unlocked_recipes_for_techs(scoping.get("assume_researched"))
    stop_set = frozenset(scoping.get("stop_items", []))
    if scoping.get("auto_stop_raw", True):
        stop_set = stop_set | await engine._auto_raw_items()
    tree = await engine._expand_node(
        row["name"],
        row["kind"],
        rate_per_min,
        depth=0,
        max_depth=args.max_depth,
        exclude_cats=engine._DEFAULT_EXCLUDE,
        stop_cats=frozenset(),
        stop_items=stop_set,
        prefer_enabled=True,
        overrides=recipe_overrides,
        ancestors=frozenset(),
        totals_items={},
        totals_fluids={},
        unresolved=[],
        alternates_map={},
        selection_notes=[],
        extra_unlocked=extra_unlocked,
        enforce_tech=bool(extra_unlocked),
    )
    intermediate_ids: set[str] = set()
    _collect_intermediate_ids(tree, intermediate_ids)

    reuse_production = [
        (iid, net.get(iid), stock.get(iid, 0))
        for iid in sorted(intermediate_ids)
        if (net.get(iid) not in (None, 0.0)) or stock.get(iid, 0)
    ]
    built = live_state.building_counts(gs, force=args.force)
    reuse_buildings = [
        (b["name"], built.get(b["id"], 0), b["count"])
        for b in result["buildings"]
        if built.get(b["id"], 0) > 0
    ]
    return reuse_production, reuse_buildings


def _print_plan_verbose(
    args: argparse.Namespace,
    result: dict,
    align: dict,
    net: dict[str, float],
    stock: dict[str, int],
    reuse_production: list[tuple[str, float | None, int]],
    reuse_buildings: list[tuple[str, int, int]],
    tech_locked_notes: list[str],
) -> None:
    per_sec = result["rate_per_min"] / 60.0
    print(
        f"plan: {result['product']}  @ {_fmt_num(result['rate_per_min'])}/min ({_fmt_num(per_sec)}/s)"
    )
    if not align["aligned"]:
        print(
            "  (live tech-scoping/netting skipped — recipes.db and live save are different modpacks; see `status`)"
        )
    print()

    print(f"machines ({len(result['buildings'])}):")
    if not result["buildings"]:
        print("  (none)")
    for b in result["buildings"]:
        modules_note = "  [modules assumed]" if b.get("module_accelerated") else ""
        print(
            f"  {b['count']:>5}x  {b['name']}  (speed {_fmt_num(b['crafting_speed'])}){modules_note}"
        )

    if result["blocked_categories"]:
        print("\nblocked (no eligible machine at current tech level):")
        for b in result["blocked_categories"]:
            print(f"  {b['category']}: {b['reason']}")

    raw_inputs = result["raw_inputs"]
    print(f"\nraw inputs ({len(raw_inputs)}):")
    if not raw_inputs:
        print("  (none)")
    for r in raw_inputs[: args.top]:
        r_per_sec = r["amount_per_min"] / 60.0
        cap = throughput.capacity_needed(r_per_sec, r["kind"])
        line = (
            f"  {r['id']:<28} ({r['name']})  {_fmt_num(r['amount_per_min']):>10}/min"
            f"  ({_fmt_num(cap['count'])} {cap['unit_plural']})"
        )
        have_net = net.get(r["id"])
        have_stock = stock.get(r["id"])
        bits = []
        if have_net is not None:
            bits.append(f"net {_fmt_num(have_net)}/min live")
        if have_stock:
            bits.append(f"{have_stock} buffered")
        if bits:
            line += "  — already have: " + ", ".join(bits)
        print(line)
    if len(raw_inputs) > args.top:
        print(f"  ... {len(raw_inputs) - args.top} more (raise --top to see all)")

    if reuse_production:
        print(
            "\nexisting production (possible reuse — not netted out, "
            "check before building fresh capacity):"
        )
        for iid, n, s in reuse_production:
            bits = []
            if n:
                bits.append(f"net {_fmt_num(n)}/min live")
            if s:
                bits.append(f"{s} buffered")
            print(f"  {iid:<28} {', '.join(bits)}")

    if reuse_buildings:
        print("\nexisting buildings (possible reuse):")
        for name, have, need in reuse_buildings:
            print(f"  {name:<32} {have} built, plan calls for {need}")

    if tech_locked_notes:
        print("\ntech-locked (falling back to raw input at your current research level):")
        for note in tech_locked_notes:
            print(f"  {note}")

    if result["drills"]:
        print("\ndrills (approximate — ignores purity/productivity bonuses):")
        by_resource = Counter(d["resource"] for d in result["drills"])
        for d in result["drills"]:
            line = f"  {d['drill_count']:>5}x  {d['drill_name']}  for {d['resource']}"
            if by_resource[d["resource"]] > 1:
                # More than one extraction path exists for this raw input
                # (e.g. a steam-fed patch vs. a fluid-free rock deposit) —
                # tag which resource_category each option is so it's clear
                # these are alternatives, not a combined requirement.
                line += f"  [{d['resource_category']}]"
            if d.get("required_fluid"):
                line += f"  (+{_fmt_num(d['fluid_rate_per_min'])} {d['required_fluid']}/min)"
            print(line)

    if result.get("blocked_drills"):
        print("\nblocked drills (locked at your current research level):")
        seen: set[tuple[str, str]] = set()
        for bd in result["blocked_drills"]:
            key = (bd["resource"], bd["drill_id"])
            if key in seen:
                continue
            seen.add(key)
            needs = ", ".join(bd["requires_research"]) or "unknown research"
            print(
                f"  {bd['drill_name']}  for {bd['resource']}  [{bd['resource_category']}]"
                f"  needs: {needs}"
            )

    if not throughput.VALUES_ARE_PYANODONS_ACCURATE:
        print(
            "\nnote: belt counts use base/Space Age throughput constants "
            "(not yet filled in for Pyanodons — see planner/throughput.py)"
        )

    print(
        f"\nnext: `expand {result['product']} --rate {_fmt_num(result['rate_per_min'] / 60.0)} "
        f"--unit per-sec` for the full BOM tree; `have <item>` to check current production "
        "of a specific input."
    )


def _print_plan_compact(
    args: argparse.Namespace,
    result: dict,
    align: dict,
    net: dict[str, float],
    reuse_production: list[tuple[str, float | None, int]],
    reuse_buildings: list[tuple[str, int, int]],
    tech_locked_notes: list[str],
) -> None:
    """One line per section instead of one line per item — same underlying
    data as the verbose output, for a consumer (typically an agent) that
    just needs the headline numbers, not a full read-every-row report."""
    per_sec = result["rate_per_min"] / 60.0
    print(
        f"plan: {result['product']} @ {_fmt_num(result['rate_per_min'])}/min ({_fmt_num(per_sec)}/s)"
    )
    if not align["aligned"]:
        print("(unaligned modpack — tech-scoping/netting skipped, see `status`)")

    machines = ", ".join(f"{b['count']}x {b['name']}" for b in result["buildings"]) or "(none)"
    print(f"machines: {machines}")

    raw_inputs = result["raw_inputs"]

    def _raw_str(r: dict) -> str:
        tag = " [tech-locked]" if _is_tech_locked(r["id"], tech_locked_notes) else ""
        return f"{r['id']} ({r['name']}) {_fmt_num(r['amount_per_min'])}/min{tag}"

    raw = ", ".join(_raw_str(r) for r in raw_inputs) or "(none)"
    print(f"raw inputs: {raw}")

    if reuse_production:
        prod = ", ".join(f"{iid}({_fmt_num(n or 0)}/min live)" for iid, n, _s in reuse_production)
        print(f"reuse candidates (production): {prod}")
    if reuse_buildings:
        bld = ", ".join(
            f"{name}({have} built/{need} needed)" for name, have, need in reuse_buildings
        )
        print(f"reuse candidates (buildings): {bld}")

    if result["drills"]:
        drills = ", ".join(
            f"{d['drill_count']}x {d['drill_name']}({d['resource']})" for d in result["drills"]
        )
        print(f"drills: {drills}")

    flags = []
    if tech_locked_notes:
        flags.append(f"tech-locked={len(tech_locked_notes)}")
    if result.get("blocked_drills"):
        flags.append(f"blocked-drills={len(result['blocked_drills'])}")
    if result["blocked_categories"]:
        flags.append(f"blocked-categories={len(result['blocked_categories'])}")
    if not throughput.VALUES_ARE_PYANODONS_ACCURATE:
        flags.append("belts=approximate")
    if any(b.get("module_accelerated") for b in result["buildings"]):
        flags.append("modules=assumed")
    if flags:
        print(f"flags: {', '.join(flags)}")
    print(
        f"(add --full for the per-row breakdown, or run `expand {result['product']}`, "
        "for detail/reasoning behind any of the above)"
    )


async def cmd_plan(args: argparse.Namespace) -> int:
    engine = _make_engine()
    if engine is None:
        return 1
    row, candidates = await _resolve_item(engine, args.product)
    if row is None:
        return _print_ambiguous(args.product, candidates)

    gs = live_state.open_game_state(config.SCRIPT_OUTPUT_DIR)
    db_tech_ids = await _db_tech_ids(engine)
    align = live_state.modpack_alignment(gs, db_tech_ids, force=args.force)

    scoping: dict = {}
    if align["aligned"]:
        scoping["assume_researched"] = live_state.researched_technologies(gs, force=args.force)
        scoping["only_enabled"] = True

    if args.stop_items:
        scoping["stop_items"] = [s.strip() for s in args.stop_items.split(",") if s.strip()]
    scoping["auto_stop_raw"] = args.auto_stop_raw

    recipe_overrides = _parse_recipe_overrides(args.recipe)
    if recipe_overrides:
        scoping["recipe_overrides"] = recipe_overrides

    if args.rate is not None and args.cap is not None:
        return _fail("--rate and --cap are mutually exclusive")

    if args.cap is not None:
        sizing = await _rate_for_belt_cap(engine, row["name"], args.max_depth, scoping, args.cap)
        if sizing is None:
            return _fail(
                f"'{row['name']}' has no raw inputs to size a --cap against "
                "— pass --rate explicitly"
            )
        rate_per_min, cap_info = sizing
        print(
            f"(--cap {_fmt_num(args.cap)} — bottleneck raw input "
            f"{cap_info['bottleneck_id']} ({cap_info['bottleneck_name']}) capped at "
            f"{_fmt_num(args.cap)} {cap_info['unit_plural']} -> {_fmt_num(rate_per_min)}/min)\n"
        )
    elif args.rate is None:
        extra_unlocked = await engine.unlocked_recipes_for_techs(scoping.get("assume_researched"))
        enforce_tech = scoping.get("only_enabled", False) or bool(extra_unlocked)
        sizing = await _one_machine_rate_per_min(
            engine,
            row["name"],
            extra_unlocked=extra_unlocked,
            enforce_tech=enforce_tech,
            overrides=recipe_overrides,
        )
        if sizing is None:
            return _fail(
                f"no available recipe/machine found to size a single-machine plan for "
                f"'{row['name']}' — pass --rate explicitly"
            )
        rate_per_min, sizing_info = sizing
        print(
            f"(no --rate given — sizing for 1x {sizing_info['machine_name']} running "
            f"'{sizing_info['recipe']}' -> {_fmt_num(rate_per_min)}/min)\n"
        )
    else:
        try:
            rate_per_min = _parse_rate_per_min(args.rate, args.unit)
        except ValueError as e:
            return _fail(str(e))

    result = await engine.plan_product(
        row["name"], rate_per_min=rate_per_min, max_depth=args.max_depth, **scoping
    )
    result["buildings"] = await _apply_module_bonus_to_buildings(engine, result["buildings"])

    net = live_state.net_production(gs, force=args.force) if align["aligned"] else {}
    stock = live_state.buffered_stock(gs, force=args.force) if align["aligned"] else {}

    # Surface existing production/buildings toward this plan rather than
    # silently assuming everything needs to be built from scratch — deciding
    # whether existing capacity actually covers the need (duty cycle,
    # backlog, whether it's earmarked for something else already) stays a
    # human call; this just makes the comparison visible.
    reuse_production, reuse_buildings = await _plan_reuse_candidates(
        engine, args, gs, align, scoping, recipe_overrides, rate_per_min, result, net, stock
    )
    tech_locked_notes = [n for n in result.get("selection_notes", []) if "tech-locked" in n]

    if args.full:
        _print_plan_verbose(
            args, result, align, net, stock, reuse_production, reuse_buildings, tech_locked_notes
        )
    else:
        _print_plan_compact(
            args, result, align, net, reuse_production, reuse_buildings, tech_locked_notes
        )
    return 0


# ---------------------------------------------------------------------------
# expand — full BOM tree
# ---------------------------------------------------------------------------


async def cmd_expand(args: argparse.Namespace) -> int:
    engine = _make_engine()
    if engine is None:
        return 1
    row, candidates = await _resolve_item(engine, args.product)
    if row is None:
        return _print_ambiguous(args.product, candidates)

    gs = live_state.open_game_state(config.SCRIPT_OUTPUT_DIR)
    db_tech_ids = await _db_tech_ids(engine)
    align = live_state.modpack_alignment(gs, db_tech_ids, force=args.force)
    extra_unlocked: frozenset[str] = frozenset()
    if align["aligned"]:
        researched = live_state.researched_technologies(gs, force=args.force)
        extra_unlocked = await engine.unlocked_recipes_for_techs(researched)
    else:
        print(
            "  (live tech-scoping skipped — recipes.db and live save are different modpacks; see `status`)"
        )

    recipe_overrides = _parse_recipe_overrides(args.recipe)
    if args.rate is None:
        sizing = await _one_machine_rate_per_min(
            engine,
            row["name"],
            extra_unlocked=extra_unlocked,
            enforce_tech=bool(extra_unlocked),
            overrides=recipe_overrides,
        )
        if sizing is None:
            return _fail(
                f"no available recipe/machine found to size a single-machine expand for "
                f"'{row['name']}' — pass --rate explicitly"
            )
        amount, sizing_info = sizing
        print(
            f"(no --rate given — sizing for 1x {sizing_info['machine_name']} running "
            f"'{sizing_info['recipe']}' -> {_fmt_num(amount)}/min)\n"
        )
    else:
        try:
            amount = _parse_rate_per_min(args.rate, args.unit)
        except ValueError as e:
            return _fail(str(e))
    stop_items = frozenset(s.strip() for s in (args.stop_items or "").split(",") if s.strip())
    if args.auto_stop_raw:
        stop_items = stop_items | await engine._auto_raw_items()
    totals_items: dict[str, float] = {}
    totals_fluids: dict[str, float] = {}
    selection_notes: list[str] = []
    # Kept (not thrown away like every other caller) so --alternates can
    # render it — see _print_tree.
    alternates_map: dict[str, list[dict]] = {}
    tree = await engine._expand_node(
        row["name"],
        row["kind"],
        amount,
        depth=0,
        max_depth=args.max_depth,
        exclude_cats=engine._DEFAULT_EXCLUDE,
        stop_cats=frozenset(),
        stop_items=stop_items,
        prefer_enabled=True,
        overrides=recipe_overrides,
        ancestors=frozenset(),
        totals_items=totals_items,
        totals_fluids=totals_fluids,
        unresolved=[],
        alternates_map=alternates_map,
        selection_notes=selection_notes,
        extra_unlocked=extra_unlocked,
        enforce_tech=bool(extra_unlocked),
    )

    print(
        f"expand: {row['name']} ({row['translated_name']})  "
        f"amount={_fmt_num(amount)}/min-equivalent (max_depth={args.max_depth})"
    )
    print()
    _print_tree(tree, alternates_map=alternates_map, show_alternates=args.alternates)

    raw_ids = list(totals_items) + list(totals_fluids)
    raw_names: dict[str, str] = {}
    if raw_ids:
        name_rows = await engine.db.fetch_all(
            f"SELECT name, translated_name FROM names WHERE name IN ({','.join('?' * len(raw_ids))})",
            tuple(raw_ids),
        )
        raw_names = {r["name"]: r["translated_name"] for r in name_rows}

    print(f"\nraw totals ({len(totals_items) + len(totals_fluids)}):")
    combined = sorted(totals_items.items(), key=lambda kv: -kv[1]) + sorted(
        totals_fluids.items(), key=lambda kv: -kv[1]
    )
    for k, v in combined[: args.top]:
        print(f"  {k} ({raw_names.get(k, k)}): {_fmt_num(v)}")

    tech_locked_notes = [n for n in selection_notes if "tech-locked" in n]
    if tech_locked_notes:
        print("\ntech-locked (falling back to raw input at your current research level):")
        for n in tech_locked_notes:
            print(f"  {n}")

    if len(combined) > args.top:
        print(f"  ... {len(combined) - args.top} more (raise --top to see all)")
    return 0


# ---------------------------------------------------------------------------
# options — decision-oriented menu: distinct viable ways to make a product
# ---------------------------------------------------------------------------


_OPTION_LABELS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _print_options_entry(
    label: str,
    item_id: str,
    r: dict,
    cls: dict,
    tag: str,
    tree: dict,
    totals_items: dict[str, float],
    totals_fluids: dict[str, float],
    alternates_map: dict[str, list[dict]],
    top: int,
) -> None:
    """Print one menu entry — a single viable top-level recipe for `item_id`,
    already expanded (forcing that recipe) into `tree`/`alternates_map`."""
    stages = tree_stages(tree)
    categories: set[str] = set()
    tree_categories(tree, categories)

    flags = []
    if cls["byproduct"]:
        flags.append("byproduct")
    if cls["absurd"]:
        flags.append(f"~{cls['machines_per_yardstick']} machines/yardstick")
    flag_str = f"  [{', '.join(flags)}]" if flags else ""

    stage_word = "stage" if stages == 1 else "stages"
    print(
        f"[{label}] {item_id} <- {r['name']} ({r['translated_name']})  "
        f"{stages} {stage_word}{tag}{flag_str}"
    )

    combined = sorted(totals_items.items(), key=lambda kv: -kv[1]) + sorted(
        totals_fluids.items(), key=lambda kv: -kv[1]
    )
    raw_str = ", ".join(f"{k} {_fmt_num(v)}/min" for k, v in combined[:top]) or "(none)"
    print(f"    raw: {raw_str}")
    print(f"    machines: {', '.join(sorted(categories)) or '(none)'}")

    for did, n in deeper_choices(alternates_map, item_id):
        print(f"    deeper choice: {did} ({n} viable recipes) — run `options {did}`")
    print()


async def _classify_and_expand_candidates(
    engine: ModuleType,
    item_id: str,
    item_kind: str,
    rows: list[dict],
    status: dict[str, str],
    rate_per_min: float,
    recipe_overrides: dict[str, str],
    stop_items: frozenset[str],
    extra_unlocked: frozenset[str],
    enforce_tech: bool,
    max_depth: int,
    include_byproducts: bool,
) -> tuple[list[dict], int]:
    """Classify every producer row for `item_id` (hiding byproduct/impractical
    ones unless `include_byproducts`), then force-expand each shown
    candidate's own recipe via `engine._expand_node` to get its full
    tree/raw-totals/alternates — the shared core of `cmd_options` and
    `cmd_recommend`, extracted so `recommend` doesn't re-derive this. Returns
    (shown, hidden_count) where each shown entry is {"row", "cls", "tag",
    "tree", "totals_items", "totals_fluids", "alt_map"}."""
    machine_speed_cache: dict[str, float] = {}
    shown: list[dict] = []
    hidden_count = 0
    for r in rows:
        category = r["category"]
        if category not in machine_speed_cache:
            machine = await _fastest_eligible_machine(
                engine, category, extra_unlocked=extra_unlocked, enforce_tech=enforce_tech
            )
            machine_speed_cache[category] = float(machine["crafting_speed"]) if machine else 0.0
        eff_out = engine._effective_out(
            {
                "out_amount": r["amount"],
                "amount_min": r["amount_min"],
                "amount_max": r["amount_max"],
                "probability": r["probability"],
            }
        )
        cls = classify_producer(
            recipe_id=r["name"],
            is_main_product=(r["main_product"] == item_id),
            probability=float(r["probability"] if r["probability"] is not None else 1.0),
            eff_out=eff_out,
            energy=float(r["energy"] or 0.0),
            fastest_speed=machine_speed_cache[category],
            yardstick_per_min=rate_per_min,
        )
        if cls["hidden"] and not include_byproducts:
            hidden_count += 1
            continue

        override = {**recipe_overrides, item_id: r["name"]}
        totals_items: dict[str, float] = {}
        totals_fluids: dict[str, float] = {}
        alt_map: dict[str, list[dict]] = {}
        tree = await engine._expand_node(
            item_id,
            item_kind,
            rate_per_min,
            depth=0,
            max_depth=max_depth,
            exclude_cats=engine._DEFAULT_EXCLUDE,
            stop_cats=frozenset(),
            stop_items=stop_items,
            prefer_enabled=True,
            overrides=override,
            ancestors=frozenset(),
            totals_items=totals_items,
            totals_fluids=totals_fluids,
            unresolved=[],
            alternates_map=alt_map,
            selection_notes=[],
            extra_unlocked=extra_unlocked,
            enforce_tech=enforce_tech,
        )
        shown.append(
            {
                "row": r,
                "cls": cls,
                "tag": status.get(r["name"], ""),
                "tree": tree,
                "totals_items": totals_items,
                "totals_fluids": totals_fluids,
                "alt_map": alt_map,
            }
        )
    return shown, hidden_count


async def cmd_options(args: argparse.Namespace) -> int:
    engine = _make_engine()
    if engine is None:
        return 1
    row, candidates = await _resolve_item(engine, args.product)
    if row is None:
        return _print_ambiguous(args.product, candidates)
    item_id, item_kind = row["name"], row["kind"]

    aligned, extra_unlocked = await _live_extra_unlocked(engine, args)
    enforce_tech = bool(extra_unlocked)
    if not aligned:
        print(
            "  (live tech-scoping skipped — recipes.db and live save are different modpacks; see `status`)"
        )

    recipe_overrides = _parse_recipe_overrides(args.recipe)
    stop_items = frozenset(s.strip() for s in (args.stop_items or "").split(",") if s.strip())
    if args.auto_stop_raw:
        stop_items = stop_items | await engine._auto_raw_items()

    # Unlike `plan`/`expand`'s "size for 1 machine of the auto-picked recipe"
    # default, `options` needs ONE consistent yardstick across every
    # candidate so their machine counts/raw inputs are actually comparable
    # side by side — defaulting per-candidate would make a slow recipe look
    # falsely cheap just because "1 machine of it" is a tiny rate.
    try:
        rate_per_min = _rate_per_min_or_default(args.rate, args.unit)
    except ValueError as e:
        return _fail(str(e))

    # Same columns cmd_producers already selects, so classify_producer has
    # everything it needs without a second query.
    rows = await engine.db.fetch_all(
        """SELECT r.name, r.translated_name, r.category, r.enabled, r.main_product, r.energy,
                  p.amount, p.amount_min, p.amount_max, p.probability
           FROM recipe_products p JOIN recipes r ON r.name = p.recipe_name
           WHERE p.item_name = ? ORDER BY r.enabled DESC, r.translated_name COLLATE NOCASE""",
        (item_id,),
    )
    # Void/incineration-style categories are excluded everywhere else in this
    # tool by default (engine._DEFAULT_EXCLUDE) — not real production choices.
    rows = [r for r in rows if r["category"] not in engine._DEFAULT_EXCLUDE]
    if not rows:
        print(f"nothing produces '{item_id}' (excluding void/incineration-style categories).")
        return 1

    status = await _tech_status_labels(engine, rows, extra_unlocked, aligned)

    shown, hidden_count = await _classify_and_expand_candidates(
        engine,
        item_id,
        item_kind,
        rows,
        status,
        rate_per_min,
        recipe_overrides,
        stop_items,
        extra_unlocked,
        enforce_tech,
        args.max_depth,
        args.include_byproducts,
    )

    if not shown:
        print(
            f"{item_id}: no viable (non-byproduct/practical) producer found — "
            "pass --include-byproducts to see hidden options."
        )
        return 1

    per_sec = rate_per_min / 60.0
    print(
        f"{item_id} ({row.get('translated_name', item_id)})  "
        f"— {len(shown)} viable way(s) at {_fmt_num(rate_per_min)}/min ({_fmt_num(per_sec)}/s):\n"
    )

    for i, entry in enumerate(shown):
        label = _OPTION_LABELS[i] if i < len(_OPTION_LABELS) else str(i)
        _print_options_entry(
            label,
            item_id,
            entry["row"],
            entry["cls"],
            entry["tag"],
            entry["tree"],
            entry["totals_items"],
            entry["totals_fluids"],
            entry["alt_map"],
            args.top,
        )

    if hidden_count:
        print(
            f"hidden: {hidden_count} byproduct/impractical recipe(s) (--include-byproducts to show)"
        )
    print(
        f"\nnext: `plan {item_id} --recipe {item_id}=<recipe_id> --rate <n>` to build the chosen way."
    )
    return 0


# ---------------------------------------------------------------------------
# tech — what a technology unlocks, and whether the unlocked recipes form a
# co-product recycling bundle (see planner/techbundle.py's module docstring
# for why this is scoped to one tech's unlock set rather than the whole
# recipe graph)
# ---------------------------------------------------------------------------


async def _tech_status(engine: ModuleType, tech_id: str, researched_techs: set[str]) -> str:
    """`[researched]` / `[needs: X, Y]` / `[not yet researched]` tag for a
    technology itself — mirrors `_tech_status_labels`'s tagging style for
    recipes, applied one level up to the tech."""
    if tech_id in researched_techs:
        return "  [researched]"
    prereqs = await engine.db.fetch_all(
        "SELECT prereq_name FROM technology_prerequisites WHERE tech_name = ?", (tech_id,)
    )
    missing_ids = [p["prereq_name"] for p in prereqs if p["prereq_name"] not in researched_techs]
    if not missing_ids:
        return "  [not yet researched]"
    name_rows = await engine.db.fetch_all(
        f"SELECT name, translated_name FROM technologies WHERE name IN ({','.join('?' * len(missing_ids))})",
        tuple(missing_ids),
    )
    names = {r["name"]: r["translated_name"] for r in name_rows}
    missing = [names.get(mid, mid) for mid in missing_ids]
    return f"  [needs: {', '.join(missing)}]"


async def _fetch_recipe_ios(engine: ModuleType, recipe_names: list[str]) -> dict[str, dict]:
    """Full ingredient/product lists for `recipe_names`, collapsed into
    `techbundle.RecipeIO` shape — products collapse probability into
    expected amount exactly as `engine._effective_out` does, but WITHOUT
    that function's epsilon floor (a matrix coefficient needs an exact
    structural zero for "doesn't touch this item", not a tiny nonzero;
    see techbundle.py's module docstring)."""
    placeholders = ",".join("?" * len(recipe_names))
    ing_rows = await engine.db.fetch_all(
        f"SELECT recipe_name, item_name, amount FROM recipe_ingredients WHERE recipe_name IN ({placeholders})",
        tuple(recipe_names),
    )
    prod_rows = await engine.db.fetch_all(
        f"""SELECT recipe_name, item_name, amount, amount_min, amount_max, probability
            FROM recipe_products WHERE recipe_name IN ({placeholders})""",
        tuple(recipe_names),
    )
    recipe_ios: dict[str, dict] = {
        name: {"ingredients": [], "products": []} for name in recipe_names
    }
    for r in ing_rows:
        recipe_ios[r["recipe_name"]]["ingredients"].append((r["item_name"], float(r["amount"])))
    for r in prod_rows:
        amount = r["amount"]
        if amount is None:
            amount = ((r["amount_min"] or 0.0) + (r["amount_max"] or 0.0)) / 2.0
        probability = r["probability"] if r["probability"] is not None else 1.0
        expected = float(amount) * float(probability)
        recipe_ios[r["recipe_name"]]["products"].append((r["item_name"], expected))
    return recipe_ios


async def cmd_tech(args: argparse.Namespace) -> int:
    engine = _make_engine()
    if engine is None:
        return 1
    row, candidates = await _resolve_tech(engine, args.name)
    if row is None:
        return _print_ambiguous_tech(args.name, candidates)
    tech_id, tech_name = row["name"], row["translated_name"]

    gs = live_state.open_game_state(config.SCRIPT_OUTPUT_DIR)
    db_tech_ids = await _db_tech_ids(engine)
    align = live_state.modpack_alignment(gs, db_tech_ids, force=args.force)
    researched_techs: set[str] = set()
    extra_unlocked: frozenset[str] = frozenset()
    if align["aligned"]:
        researched_list = live_state.researched_technologies(gs, force=args.force)
        researched_techs = set(researched_list)
        extra_unlocked = await engine.unlocked_recipes_for_techs(researched_list)
        status = await _tech_status(engine, tech_id, researched_techs)
    else:
        status = ""
        print(
            "  (live tech-scoping skipped — recipes.db and live save are different modpacks; see `status`)"
        )
    print(f"{tech_id} ({tech_name}){status}")

    unlock_rows = await engine.db.fetch_all(
        """SELECT r.name, r.translated_name, r.category, r.energy
           FROM technology_recipe_unlocks tru JOIN recipes r ON r.name = tru.recipe_name
           WHERE tru.tech_name = ? ORDER BY r.translated_name COLLATE NOCASE""",
        (tech_id,),
    )
    if not unlock_rows:
        print("\nunlocks: (no recipes)")
        return 0

    print(f"\nunlocks ({len(unlock_rows)} recipe(s)):")
    if len(unlock_rows) > techbundle.SIZE_CAP:
        print(f"  too many to analyze as one bundle (cap: {techbundle.SIZE_CAP}) — listing only:")
        for r in unlock_rows:
            print(f"  {r['name']:<32} ({r['translated_name']})  [{r['category']}]")
        return 0

    recipe_names = [r["name"] for r in unlock_rows]
    recipe_display = {r["name"]: r for r in unlock_rows}
    recipe_ios = await _fetch_recipe_ios(engine, recipe_names)

    components = techbundle.find_components(recipe_ios)
    singletons = sorted((c for c in components if len(c) == 1), key=lambda c: next(iter(c)))
    bundles = [c for c in components if len(c) > 1]

    for c in singletons:
        rid = next(iter(c))
        r = recipe_display[rid]
        print(f"  also unlocks: {rid} ({r['translated_name']})  [{r['category']}]")

    try:
        rate_per_min = _rate_per_min_or_default(args.rate, args.unit)
    except ValueError as e:
        return _fail(str(e))
    enforce_tech = bool(extra_unlocked)

    consume_item: str | None = None
    consume_rate_per_min = 0.0
    if args.consume:
        if "=" not in args.consume:
            return _fail(f"--consume must be ITEM=RATE, got {args.consume!r}")
        consume_item, consume_rate_str = args.consume.split("=", 1)
        consume_item = consume_item.strip()
        try:
            consume_rate_per_min = _parse_rate_per_min(consume_rate_str, args.unit)
        except ValueError as e:
            return _fail(f"--consume {e}")
    consume_matched = False

    all_item_ids: set[str] = set()
    for component in bundles:
        boundary = techbundle.classify_boundary(component, recipe_ios)
        all_item_ids |= boundary["external_outputs"] | boundary["external_inputs"]
    item_names = await _item_translated_names(engine, sorted(all_item_ids))

    def _iid(item_id: str) -> str:
        name = item_names.get(item_id)
        return f"{item_id} ({name})" if name else item_id

    def _rid(rid: str) -> str:
        name = recipe_display[rid]["translated_name"]
        return f"{rid} ({name})" if name else rid

    for component in bundles:
        boundary = techbundle.classify_boundary(component, recipe_ios)
        print(f"\ndetected bundle: {', '.join(_rid(rid) for rid in sorted(component))}")

        consuming = consume_item is not None and consume_item in boundary["external_inputs"]
        if consuming:
            consume_matched = True
            anchor = consume_item
            target_rate = -consume_rate_per_min
        else:
            anchor = args.anchor or techbundle.default_anchor(
                component, recipe_ios, boundary["external_outputs"]
            )
            if anchor is None:
                print("  (closed loop — no external output to size a plan against)")
                continue
            target_rate = rate_per_min

        result = techbundle.solve_component(component, recipe_ios, anchor, target_rate)
        if result["status"] != "solved":
            print(f"  could not combine into one blend ({result['status']}): {result['reason']}")
            print("  see these recipes individually via `options <item>` instead.")
            continue

        if consuming:
            print(
                f"  sizing to fully consume: {_iid(anchor)} @ {_fmt_num(consume_rate_per_min)}/min"
            )
        else:
            others = boundary["external_outputs"] - {anchor}
            other_note = (
                f"  (also produces: {', '.join(_iid(o) for o in sorted(others))})" if others else ""
            )
            print(f"  anchor: {_iid(anchor)} @ {_fmt_num(rate_per_min)}/min{other_note}")

        for rid in sorted(component):
            rate = result["batch_rates"][rid]
            r = recipe_display[rid]
            machine = await _fastest_eligible_machine(
                engine, r["category"], extra_unlocked=extra_unlocked, enforce_tech=enforce_tech
            )
            machine_note = ""
            energy = float(r["energy"] or 0.0)
            if machine and energy > 0:
                speed = float(machine["crafting_speed"] or 1.0)
                count = math.ceil(rate * energy / (60.0 * speed))
                machine_note = f"  ~{count}x {machine['translated_name']}"
            print(f"    {_rid(rid):<40} {_fmt_num(rate)}/min{machine_note}")

        print(
            "  (for a different target rate, re-run with --rate <n> --unit per-min/per-sec "
            "directly — do NOT multiply these counts by hand: each is already rounded up "
            "individually, so scaling the rounded counts overcounts. Scale the RATE, not the "
            "machine counts.)"
        )

        if consuming:
            print("  external outputs:")
            for item_id in sorted(boundary["external_outputs"]):
                total = sum(
                    amount * result["batch_rates"][rid]
                    for rid in component
                    for iid, amount in recipe_ios[rid]["products"]
                    if iid == item_id
                )
                print(f"    {_iid(item_id):<40} {_fmt_num(total)}/min")

        if boundary["external_inputs"]:
            print("  external inputs:")
            for item_id in sorted(boundary["external_inputs"]):
                total = sum(
                    amount * result["batch_rates"][rid]
                    for rid in component
                    for iid, amount in recipe_ios[rid]["ingredients"]
                    if iid == item_id
                )
                print(f"    {_iid(item_id):<40} {_fmt_num(total)}/min")

    if args.consume and not consume_matched:
        print(
            f"\n(--consume {consume_item}={_fmt_num(consume_rate_per_min)}/min didn't match any "
            "bundle's external inputs above — check the item id against the 'external inputs' "
            "lists printed above; fell back to --rate/--anchor sizing instead)"
        )

    return 0


async def _tech_bundle_for_candidate(
    engine: ModuleType, recipe_id: str, anchor_item: str, rate_per_min: float
) -> dict | None:
    """If `recipe_id` is unlocked by a technology whose OTHER unlocked
    recipes combine with it into a co-product recycling bundle that
    produces `anchor_item` as an external output, solve that bundle at
    `rate_per_min` and return {"tech_id", "tech_name", "component",
    "result", "raw_totals"}. Returns None if `recipe_id` has no unlocking
    tech, isn't part of a size>=2 component, that component doesn't produce
    `anchor_item`, or the bundle doesn't solve cleanly (underdetermined/
    infeasible) — in any of those cases the caller should fall back to
    `recipe_id`'s own single-recipe cost.

    A recipe can rarely (14 of ~6150 unlockable recipes, per a live count)
    have more than one unlocking tech — try each in turn, use the first
    that yields a solvable bundle."""
    tech_rows = await engine.db.fetch_all(
        "SELECT tech_name FROM technology_recipe_unlocks WHERE recipe_name = ?", (recipe_id,)
    )
    for t in tech_rows:
        tech_id = t["tech_name"]
        unlock_rows = await engine.db.fetch_all(
            """SELECT r.name FROM technology_recipe_unlocks tru JOIN recipes r ON r.name = tru.recipe_name
               WHERE tru.tech_name = ?""",
            (tech_id,),
        )
        recipe_names = [r["name"] for r in unlock_rows]
        if recipe_id not in recipe_names or len(recipe_names) > techbundle.SIZE_CAP:
            continue
        recipe_ios = await _fetch_recipe_ios(engine, recipe_names)
        component = next(
            (c for c in techbundle.find_components(recipe_ios) if recipe_id in c and len(c) > 1),
            None,
        )
        if component is None:
            continue
        boundary = techbundle.classify_boundary(component, recipe_ios)
        if anchor_item not in boundary["external_outputs"]:
            continue
        result = techbundle.solve_component(component, recipe_ios, anchor_item, rate_per_min)
        if result["status"] != "solved":
            continue
        raw_totals: dict[str, float] = {}
        for item_id in boundary["external_inputs"]:
            total = sum(
                amount * result["batch_rates"][rid]
                for rid in component
                for iid, amount in recipe_ios[rid]["ingredients"]
                if iid == item_id
            )
            raw_totals[item_id] = total
        tech_name_row = await engine.db.fetch_one(
            "SELECT translated_name FROM technologies WHERE name = ?", (tech_id,)
        )
        return {
            "tech_id": tech_id,
            "tech_name": tech_name_row["translated_name"] if tech_name_row else tech_id,
            "component": component,
            "result": result,
            "raw_totals": raw_totals,
        }
    return None


async def cmd_recommend(args: argparse.Namespace) -> int:
    engine = _make_engine()
    if engine is None:
        return 1
    row, candidates = await _resolve_item(engine, args.product)
    if row is None:
        return _print_ambiguous(args.product, candidates)
    item_id, item_kind = row["name"], row["kind"]

    aligned, extra_unlocked = await _live_extra_unlocked(engine, args)
    enforce_tech = bool(extra_unlocked)
    if not aligned:
        print(
            "  (live tech-scoping skipped — recipes.db and live save are different modpacks; see `status`)"
        )

    recipe_overrides = _parse_recipe_overrides(args.recipe)
    stop_items = frozenset(s.strip() for s in (args.stop_items or "").split(",") if s.strip())
    if args.auto_stop_raw:
        stop_items = stop_items | await engine._auto_raw_items()

    try:
        rate_per_min = _rate_per_min_or_default(args.rate, args.unit)
    except ValueError as e:
        return _fail(str(e))

    rows = await engine.db.fetch_all(
        """SELECT r.name, r.translated_name, r.category, r.enabled, r.main_product, r.energy,
                  p.amount, p.amount_min, p.amount_max, p.probability
           FROM recipe_products p JOIN recipes r ON r.name = p.recipe_name
           WHERE p.item_name = ? ORDER BY r.enabled DESC, r.translated_name COLLATE NOCASE""",
        (item_id,),
    )
    rows = [r for r in rows if r["category"] not in engine._DEFAULT_EXCLUDE]
    if not rows:
        print(f"nothing produces '{item_id}' (excluding void/incineration-style categories).")
        return 1

    status = await _tech_status_labels(engine, rows, extra_unlocked, aligned)

    # Never recommend a byproduct/impractical recipe -- include_byproducts is
    # always False here, unlike `options` which lets the caller opt in.
    shown, _hidden_count = await _classify_and_expand_candidates(
        engine,
        item_id,
        item_kind,
        rows,
        status,
        rate_per_min,
        recipe_overrides,
        stop_items,
        extra_unlocked,
        enforce_tech,
        args.max_depth,
        include_byproducts=False,
    )
    if not shown:
        print(f"{item_id}: no viable (non-byproduct/practical) producer found.")
        return 1

    summaries: list[dict] = []
    combo_by_recipe: dict[str, dict] = {}
    for entry in shown:
        r = entry["row"]
        recipe_id = r["name"]
        tag = entry["tag"]
        researched = tag in ("", "  [researched]")
        raw_totals = {**entry["totals_items"], **entry["totals_fluids"]}

        if researched:
            combo = await _tech_bundle_for_candidate(engine, recipe_id, item_id, rate_per_min)
            if combo is not None:
                raw_totals = combo["raw_totals"]
                combo_by_recipe[recipe_id] = combo

        summaries.append(
            {
                "recipe_id": recipe_id,
                "researched": researched,
                "raw_totals": raw_totals,
                "stages": tree_stages(entry["tree"]),
                "tag": tag,
            }
        )

    usable = [s for s in summaries if s["researched"]]
    locked_count = len(summaries) - len(usable)
    if not usable:
        print(
            f"{item_id}: nothing currently researched can produce it — "
            f"see `options {item_id}` for what research would unlock."
        )
        return 1

    ranked = rank_candidates(usable)
    winner = ranked[0]
    combo = combo_by_recipe.get(winner["recipe_id"])

    all_recipe_ids = {s["recipe_id"] for s in summaries}
    if combo is not None:
        all_recipe_ids |= set(combo["component"])
    recipe_names = await _recipe_translated_names(engine, sorted(all_recipe_ids))

    def _rid(rid: str) -> str:
        name = recipe_names.get(rid)
        return f"{rid} ({name})" if name else rid

    def _raw_str(totals: dict[str, float]) -> str:
        return (
            ", ".join(
                f"{k} {_fmt_num(v)}/min" for k, v in sorted(totals.items(), key=lambda kv: -kv[1])
            )
            or "(none)"
        )

    per_sec = rate_per_min / 60.0
    print(
        f"{item_id} ({row.get('translated_name', item_id)}) — recommended at "
        f"{_fmt_num(rate_per_min)}/min ({_fmt_num(per_sec)}/s):\n"
    )

    if combo is not None:
        recipe_list = ", ".join(_rid(rid) for rid in sorted(combo["component"]))
        print(f'recommended: {recipe_list}  (combo via "{combo["tech_name"]}")')
        print(f"  {_raw_str(winner['raw_totals'])}")
        for rid in sorted(combo["component"]):
            print(f"    {_rid(rid):<40} {_fmt_num(combo['result']['batch_rates'][rid])}/min")
        print(
            "  (this IS the build — `plan`/`expand` can't represent a blended multi-recipe "
            "combo yet)"
        )
        print(
            f'\nnext: `tech "{combo["tech_name"]}" --rate {_fmt_num(rate_per_min)} --unit per-min` '
            "for machine counts at this exact rate — do NOT multiply this command's counts by "
            "hand for a different rate, re-run it with the new --rate instead (each count is "
            "already rounded up individually, so scaling the rounded counts overcounts)."
        )
    else:
        stage_word = "stage" if winner["stages"] == 1 else "stages"
        print(
            f"recommended: {_rid(winner['recipe_id'])}  "
            f"({winner['stages']} {stage_word}){winner['tag']}"
        )
        print(f"  {_raw_str(winner['raw_totals'])}")
        print(
            f"\nnext: `plan {item_id} --recipe {item_id}={winner['recipe_id']} --rate <n>` "
            "to build it."
        )

    ungated = [s for s in ranked if s["tag"] == ""]
    if ungated and ungated[0]["recipe_id"] != winner["recipe_id"]:
        runner = ungated[0]
        print(f"\nrunner-up (no research needed): {_rid(runner['recipe_id'])}")
        print(f"  {_raw_str(runner['raw_totals'])}")

    if locked_count:
        print(
            f"\n{locked_count} further option(s) need research not yet done — "
            f"see `options {item_id}` for all of them."
        )

    return 0


# ---------------------------------------------------------------------------
# recipe / producers / consumers — thin recipe lookups
# ---------------------------------------------------------------------------


async def _print_one_recipe(
    engine: ModuleType, name: str, extra_unlocked: frozenset[str], aligned: bool
) -> bool:
    """Print full detail for one recipe. Returns False (having already
    printed the reason) if `name` didn't resolve to exactly one recipe."""
    db = engine.db
    row = await db.fetch_one("SELECT * FROM recipes WHERE name = ?", (name,))
    if row is None:
        row = await db.fetch_one(
            "SELECT * FROM recipes WHERE translated_name = ? COLLATE NOCASE", (name,)
        )
    if row is None:
        candidates = await db.fetch_all(
            """SELECT name, translated_name FROM recipes
               WHERE name LIKE ? COLLATE NOCASE OR translated_name LIKE ? COLLATE NOCASE
               ORDER BY translated_name COLLATE NOCASE LIMIT 10""",
            (f"%{name}%", f"%{name}%"),
        )
        if not candidates:
            print(f"no recipe found matching '{name}'.")
            return False
        if len(candidates) > 1:
            print(f"'{name}' is ambiguous — candidates:")
            for c in candidates:
                print(f"  {c['name']:<30} {c['translated_name']}")
            return False
        row = await db.fetch_one("SELECT * FROM recipes WHERE name = ?", (candidates[0]["name"],))

    ingredients = await db.fetch_all(
        "SELECT item_name, amount FROM recipe_ingredients WHERE recipe_name = ? ORDER BY position",
        (row["name"],),
    )
    products = await db.fetch_all(
        """SELECT item_name, amount, amount_min, amount_max, probability
           FROM recipe_products WHERE recipe_name = ? ORDER BY position""",
        (row["name"],),
    )

    ing_prod_ids = [i["item_name"] for i in ingredients] + [p["item_name"] for p in products]
    item_names: dict[str, str] = {}
    if ing_prod_ids:
        name_rows = await db.fetch_all(
            f"SELECT name, translated_name FROM names WHERE name IN ({','.join('?' * len(ing_prod_ids))})",
            tuple(ing_prod_ids),
        )
        item_names = {r["name"]: r["translated_name"] for r in name_rows}

    status = (await _tech_status_labels(engine, [row], extra_unlocked, aligned)).get(
        row["name"], ""
    )

    print(f"{row['name']}  ({row['translated_name']}){status}")
    print(
        f"  category: {row['category']}   craft time: {row['energy']}s   enabled: {bool(row['enabled'])}"
        f"   main product: {row['main_product'] or '(none)'}"
    )
    print("  ingredients:")
    for i in ingredients:
        item_id = i["item_name"]
        print(f"    {_fmt_num(i['amount'])}x {item_id} ({item_names.get(item_id, item_id)})")
    print("  products:")
    craft_time = row["energy"]
    for p in products:
        amt = (
            _fmt_num(p["amount"])
            if p["amount"] is not None
            else f"{p['amount_min']}-{p['amount_max']}"
        )
        prob = f" @ {p['probability'] * 100:.0f}%" if p["probability"] not in (None, 1.0) else ""
        rate = _rate_hint(p["amount"], p["probability"], craft_time)
        item_id = p["item_name"]
        print(f"    {amt}x {item_id} ({item_names.get(item_id, item_id)}){prob}{rate}")
    return True


async def cmd_recipe(args: argparse.Namespace) -> int:
    engine = _make_engine()
    if engine is None:
        return 1
    aligned, extra_unlocked = await _live_extra_unlocked(engine, args)
    if not aligned:
        print(
            "  (live tech-scoping skipped — recipes.db and live save are different modpacks; see `status`)"
        )
    ok = True
    for i, name in enumerate(args.name):
        if i > 0:
            print("---")
        ok = await _print_one_recipe(engine, name, extra_unlocked, aligned) and ok
    return 0 if ok else 1


async def _item_translated_name(engine: ModuleType, item_id: str) -> str:
    row = await engine.db.fetch_one("SELECT translated_name FROM names WHERE name = ?", (item_id,))
    return row["translated_name"] if row else item_id


async def _item_translated_names(engine: ModuleType, item_ids: list[str]) -> dict[str, str]:
    """Batch id -> translated_name lookup for items/fluids — same purpose
    as `_recipe_translated_names`, for the item ids `tech`'s external
    inputs/outputs rows print."""
    if not item_ids:
        return {}
    rows = await engine.db.fetch_all(
        f"SELECT name, translated_name FROM names WHERE name IN ({','.join('?' * len(item_ids))})",
        tuple(item_ids),
    )
    return {r["name"]: r["translated_name"] for r in rows}


async def _recipe_translated_names(engine: ModuleType, recipe_ids: list[str]) -> dict[str, str]:
    """Batch id -> translated_name lookup for recipes, so combo/bundle
    output can show `id (translated name)` the same way `producers`/
    `consumers`/`recipe` already do — recipe ids are kept as the primary,
    unambiguous identifier (two recipes can share one translated name, e.g.
    Pyanodons' "grade-1-copper-crush"/"grade-2-copper" both translate to
    "Copper (grade 2)"), the translated name is purely a UX add-on."""
    if not recipe_ids:
        return {}
    rows = await engine.db.fetch_all(
        f"SELECT name, translated_name FROM recipes WHERE name IN ({','.join('?' * len(recipe_ids))})",
        tuple(recipe_ids),
    )
    return {r["name"]: r["translated_name"] for r in rows}


async def _live_extra_unlocked(
    engine: ModuleType, args: argparse.Namespace
) -> tuple[bool, frozenset[str]]:
    """(aligned, extra_unlocked) for the given --force — the same live
    tech-scoping `plan`/`expand` already use, shared here so `producers`/
    `consumers`/`recipe` can show real availability instead of the DB's
    stale `enabled` snapshot."""
    gs = live_state.open_game_state(config.SCRIPT_OUTPUT_DIR)
    db_tech_ids = await _db_tech_ids(engine)
    align = live_state.modpack_alignment(gs, db_tech_ids, force=args.force)
    if not align["aligned"]:
        return False, frozenset()
    researched = live_state.researched_technologies(gs, force=args.force)
    return True, await engine.unlocked_recipes_for_techs(researched)


async def cmd_producers(args: argparse.Namespace) -> int:
    engine = _make_engine()
    if engine is None:
        return 1
    rows = await engine.db.fetch_all(
        """SELECT r.name, r.translated_name, r.category, r.enabled, r.main_product, r.energy,
                  p.amount, p.amount_min, p.amount_max, p.probability
           FROM recipe_products p JOIN recipes r ON r.name = p.recipe_name
           WHERE p.item_name = ? ORDER BY r.enabled DESC, r.translated_name COLLATE NOCASE""",
        (args.item,),
    )
    if not rows:
        print(
            f"nothing produces '{args.item}' (use the exact internal id — try `expand {args.item}` to check)."
        )
        return 1
    aligned, extra_unlocked = await _live_extra_unlocked(engine, args)
    status = await _tech_status_labels(engine, rows, extra_unlocked, aligned)
    if not aligned:
        print(
            "  (live tech-scoping skipped — recipes.db and live save are different modpacks; see `status`)"
        )
    item_name = await _item_translated_name(engine, args.item)
    print(f"producers of {args.item} ({item_name}) ({len(rows)}):")
    for r in rows:
        amt = (
            _fmt_num(r["amount"])
            if r["amount"] is not None
            else f"{r['amount_min']}-{r['amount_max']}"
        )
        # main_product == item_id means this is the recipe's actual purpose,
        # not a probabilistic/secondary byproduct of something else — the
        # auto-picker in `plan`/`expand` now prefers these (see engine.py's
        # _pick_producer Tier 1.5).
        main = "  [main product]" if r["main_product"] == args.item else ""
        rate = _rate_hint(r["amount"], r["probability"], r["energy"])
        print(
            f"  {r['name']:<32} ({r['translated_name']})  {amt}x  ({r['category']})"
            f"{rate}{status.get(r['name'], '')}{main}"
        )
    return 0


async def cmd_consumers(args: argparse.Namespace) -> int:
    engine = _make_engine()
    if engine is None:
        return 1
    rows = await engine.db.fetch_all(
        """SELECT r.name, r.translated_name, r.category, r.enabled, i.amount
           FROM recipe_ingredients i JOIN recipes r ON r.name = i.recipe_name
           WHERE i.item_name = ? ORDER BY r.enabled DESC, r.translated_name COLLATE NOCASE""",
        (args.item,),
    )
    if not rows:
        print(
            f"nothing consumes '{args.item}' (use the exact internal id — try `expand {args.item}` to check)."
        )
        return 1
    aligned, extra_unlocked = await _live_extra_unlocked(engine, args)
    status = await _tech_status_labels(engine, rows, extra_unlocked, aligned)
    if not aligned:
        print(
            "  (live tech-scoping skipped — recipes.db and live save are different modpacks; see `status`)"
        )
    item_name = await _item_translated_name(engine, args.item)
    print(f"consumers of {args.item} ({item_name}) ({len(rows)}):")
    for r in rows:
        print(
            f"  {r['name']:<32} ({r['translated_name']})  {_fmt_num(r['amount'])}x  ({r['category']})"
            f"{status.get(r['name'], '')}"
        )
    return 0


# ---------------------------------------------------------------------------
# have — live-only: what do I already produce/store
# ---------------------------------------------------------------------------


async def cmd_have(args: argparse.Namespace) -> int:
    gs = live_state.open_game_state(config.SCRIPT_OUTPUT_DIR)
    net = live_state.net_production(gs, force=args.force)
    stock = live_state.buffered_stock(gs, force=args.force)
    ok = True
    for item in args.item:
        if item not in net and item not in stock:
            print(
                f"no live data for '{item}' on force '{args.force}' (not produced/consumed or buffered)."
            )
            print(
                "next: check the exact item id, or `producers <item>`/`consumers <item>` to find it."
            )
            ok = False
            continue
        n = net.get(item, 0.0)
        s = stock.get(item, 0)
        verb = "producing" if n > 0 else "consuming" if n < 0 else "net-neutral on"
        print(
            f"{item}: {verb} {_fmt_num(abs(n))}/min, {s} buffered in logistics, force '{args.force}'"
        )
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# belts — reverse throughput sizing: N belts -> achievable rate
# ---------------------------------------------------------------------------


async def cmd_belts(args: argparse.Namespace) -> int:
    """Pure throughput-constant math (works with no recipes.db/live data at
    all), but when --tier is omitted it first tries live tech-scoping to
    pick the fastest tier the current save can actually build, falling back
    to the static base/starter tier (throughput.DEFAULT_BELT_TIER_ORDER[0])
    whenever that data isn't available — so an unresearched game still gets
    a correct default instead of the old silent fastest-tier assumption."""
    tier = args.tier
    tier_note: str | None = None
    if tier is None and config.RECIPES_DB.exists():
        engine = _make_engine()
        if engine is not None:
            aligned, extra_unlocked = await _live_extra_unlocked(engine, args)
            if aligned:
                tier = await _fastest_buildable_belt_tier(engine, extra_unlocked)
                if tier is not None:
                    tier_note = (
                        f"(no --tier given — assumed {tier}, the fastest belt tier your "
                        "current save can build. Pass --tier explicitly to override.)"
                    )
    try:
        result = throughput.rate_from_belts(args.count, tier=tier)
    except ValueError as e:
        return _fail(str(e))
    per_sec = result["items_per_sec"]
    per_min = per_sec * 60.0
    print(
        f"{_fmt_num(args.count)}x {result['tier']}  =  {_fmt_num(per_sec)}/s  =  {_fmt_num(per_min)}/min"
    )
    if tier_note is not None:
        print(tier_note)
    elif args.tier is None:
        print(
            f"(no --tier given and live tech-scoping unavailable — assumed the base/starter "
            f"tier, {result['tier']}. Pass --tier explicitly, e.g. --tier fast-transport-belt "
            "for a researched faster belt.)"
        )
    if not result["accurate"]:
        print(
            "(belt throughput constants are base/Space Age placeholders — see planner/throughput.py)"
        )
    print(
        f"\nnext: `plan <product> --rate {_fmt_num(per_sec)}` to size a line for this input rate."
    )
    return 0


# ---------------------------------------------------------------------------
# argument parsing / entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="planner",
        description=(
            "flma factory planner — live flma game state x recipe-mcp recipe data\n\n"
            'For an open-ended "how do I make X" question, start with\n'
            "`recommend <product>` — it picks the single best current way to make\n"
            "something. `options`/`producers`/`recipe`/`tech` are for comparing\n"
            "alternatives or inspecting a specific candidate once you already know\n"
            "you need to override `recommend`'s pick; `plan`/`expand` build out the\n"
            "chosen recipe into a sized production line."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    p_status = sub.add_parser(
        "status", help="modpack/live-data health check (default with no args)"
    )
    p_status.add_argument(
        "--force", default="player", help="Factorio force to scope to (default: player)"
    )

    p_options = sub.add_parser(
        "options",
        help=(
            "compare every viable way to make a product side by side (the decision "
            "menu behind `plan`/`expand`) — for a single best answer, use `recommend` instead"
        ),
    )
    p_options.add_argument("product", help="item/fluid id or human name")
    p_options.add_argument(
        "--rate",
        default=None,
        help=(
            "comparison yardstick, applied identically to every option so their raw "
            "inputs/machines are comparable side by side (default: 60/min = 1/sec). A "
            "bare number is interpreted per --unit; suffix it directly instead (e.g. "
            "`15/s`, `900/min`) to set the unit inline and ignore --unit. Unlike "
            "`plan`/`expand`, this does NOT default to '1 machine of the auto-picked "
            "recipe' — different candidate recipes run at different speeds, so a "
            "per-candidate default would make slow recipes look falsely cheap."
        ),
    )
    p_options.add_argument(
        "--unit",
        choices=["per-sec", "per-min"],
        default="per-sec",
        help="unit for a --rate/--consume value with no /s or /min suffix of its own",
    )
    p_options.add_argument("--max-depth", type=int, default=6)
    p_options.add_argument("--top", type=int, default=8, help="max raw inputs to show per option")
    p_options.add_argument(
        "--force", default="player", help="Factorio force to scope tech-status to (default: player)"
    )
    p_options.add_argument(
        "--stop-items",
        default=None,
        help="comma-separated item ids to treat as raw inputs the user can already supply",
    )
    p_options.add_argument(
        "--no-auto-stop-raw",
        dest="auto_stop_raw",
        action="store_false",
        default=True,
        help="see `plan --no-auto-stop-raw` — same real-mined-resource default here",
    )
    p_options.add_argument(
        "--recipe",
        default=None,
        help="comma-separated ITEM=RECIPE overrides applied below the top-level choice",
    )
    p_options.add_argument(
        "--include-byproducts",
        action="store_true",
        help=(
            "also list recipes where the product is a low-probability byproduct "
            "(not the recipe's main_product) or would need an absurd machine "
            "count at the comparison yardstick — hidden by default"
        ),
    )

    p_plan = sub.add_parser("plan", help="design a production line for a target rate")
    p_plan.add_argument("product", help="item/fluid id or human name")
    p_plan.add_argument(
        "--rate",
        default=None,
        help=(
            "target rate. A bare number is interpreted per --unit; suffix it directly "
            "instead (e.g. `15/s`, `900/min`) to set the unit inline and ignore --unit. "
            "If omitted, sizes the plan for exactly 1 of the top-level recipe's fastest "
            "eligible machine instead of an arbitrary rate — the natural 'basic setup' "
            "starting point ('just build one')."
        ),
    )
    p_plan.add_argument(
        "--unit",
        choices=["per-sec", "per-min"],
        default="per-sec",
        help="unit for a --rate value with no /s or /min suffix of its own",
    )
    p_plan.add_argument(
        "--cap",
        type=float,
        default=None,
        help=(
            "solve for the output rate where the raw input needing the most logistics "
            "capacity (auto-picked -- whichever raw input needs the most belts if it's "
            "an item, or pipes if it's a fluid, at a reference rate) needs exactly this "
            "many belts/pipes (e.g. 1, or 0.5 for half a belt) -- 'how fast can I run "
            "this without needing more than N belts/pipes of my worst input', instead of "
            "picking an arbitrary output rate first and finding out logistics needs "
            "second. Mutually exclusive with --rate."
        ),
    )
    p_plan.add_argument("--max-depth", type=int, default=6)
    p_plan.add_argument("--top", type=int, default=15, help="max raw inputs to show (default 15)")
    p_plan.add_argument(
        "--force", default="player", help="Factorio force to scope live-state to (default: player)"
    )
    p_plan.add_argument(
        "--stop-items",
        default=None,
        help=(
            "comma-separated item ids to treat as raw inputs and stop expanding "
            "(e.g. iron-ore,copper-ore). Pyanodons has alternate synthetic recipes "
            "for many ores; without this the chain can expand deep into unrelated "
            "byproduct chains instead of stopping at the ore — see `expand` if a "
            "plan's raw_inputs look wrong to sanity-check the chosen recipe chain."
        ),
    )
    p_plan.add_argument(
        "--no-auto-stop-raw",
        dest="auto_stop_raw",
        action="store_false",
        default=True,
        help=(
            "by default, real mineable/harvestable map resources (water, ores, "
            "stone, coal, confirmed-simple flora, ...) already stop expansion "
            "without needing --stop-items — pass this to see the full synthetic "
            "byproduct-recipe expansion for those too"
        ),
    )
    p_plan.add_argument(
        "--recipe",
        default=None,
        help=(
            "comma-separated ITEM=RECIPE overrides forcing a specific recipe per item "
            "(e.g. sand=gravel-to-sand). Use `producers <item>` to see candidates — "
            "rows tagged [main product] are usually the one you want; the auto-picker "
            "already prefers those, so this is for the remaining surprises."
        ),
    )
    p_plan.add_argument(
        "--full",
        action="store_true",
        help=(
            "full per-row breakdown (one line per machine/raw input/etc.) instead of the "
            "default one-line-per-section summary — use when something in the compact "
            "output looks wrong and you need the detail behind it"
        ),
    )

    p_expand = sub.add_parser("expand", help="full bill-of-materials tree for a product")
    p_expand.add_argument("product", help="item/fluid id or human name")
    p_expand.add_argument(
        "--rate",
        default=None,
        help=(
            "target rate. A bare number is interpreted per --unit; suffix it directly "
            "instead (e.g. `15/s`, `900/min`) to set the unit inline and ignore --unit. "
            "If omitted, sizes for exactly 1 of the top-level recipe's fastest eligible "
            "machine instead of an arbitrary rate."
        ),
    )
    p_expand.add_argument(
        "--unit",
        choices=["per-sec", "per-min"],
        default="per-sec",
        help="unit for a --rate value with no /s or /min suffix of its own",
    )
    p_expand.add_argument("--max-depth", type=int, default=6)
    p_expand.add_argument("--top", type=int, default=15, help="max raw totals to show (default 15)")
    p_expand.add_argument(
        "--force", default="player", help="Factorio force to scope live-state to (default: player)"
    )
    p_expand.add_argument(
        "--stop-items",
        default=None,
        help="comma-separated item ids to treat as raw and stop expanding (e.g. iron-ore,copper-ore)",
    )
    p_expand.add_argument(
        "--no-auto-stop-raw",
        dest="auto_stop_raw",
        action="store_false",
        default=True,
        help=(
            "by default, real mineable/harvestable map resources already stop "
            "expansion without needing --stop-items — pass this to see the full "
            "expansion for those too"
        ),
    )
    p_expand.add_argument(
        "--recipe",
        default=None,
        help="comma-separated ITEM=RECIPE overrides forcing a specific recipe per item",
    )
    p_expand.add_argument(
        "--alternates",
        action="store_true",
        help=(
            "under each node, also list its other viable candidate recipes "
            "(tagged available/tech_locked/excluded/stop_category) instead of "
            "only the one auto-selected — see `options` for a higher-level "
            "menu of top-level choices instead of this inline per-node view"
        ),
    )

    p_recipe = sub.add_parser(
        "recipe",
        help=(
            "full detail for one or more recipes you've already identified as "
            "candidates — use `recommend`/`producers` first to find the id(s)"
        ),
    )
    p_recipe.add_argument("name", nargs="+", help="recipe id(s) or human name(s)")
    p_recipe.add_argument(
        "--force", default="player", help="Factorio force to scope tech-status to (default: player)"
    )

    p_producers = sub.add_parser(
        "producers",
        help=(
            "what recipes produce this item (exact id) — for a single best answer "
            "use `recommend` instead; this is for manual comparison"
        ),
    )
    p_producers.add_argument("item")
    p_producers.add_argument(
        "--force", default="player", help="Factorio force to scope tech-status to (default: player)"
    )

    p_consumers = sub.add_parser("consumers", help="what recipes consume this item (exact id)")
    p_consumers.add_argument("item")
    p_consumers.add_argument(
        "--force", default="player", help="Factorio force to scope tech-status to (default: player)"
    )

    p_have = sub.add_parser(
        "have", help="live net production + buffered stock for one or more items (exact id)"
    )
    p_have.add_argument("item", nargs="+", help="item id(s)")
    p_have.add_argument(
        "--force", default="player", help="Factorio force to scope to (default: player)"
    )

    p_belts = sub.add_parser(
        "belts", help="convert a belt-lane count to an achievable item rate (for `plan --rate`)"
    )
    p_belts.add_argument("count", type=float, help="number of belt lanes")
    p_belts.add_argument(
        "--tier",
        default=None,
        help=(
            "belt tier id — transport-belt (vanilla 'yellow', 15/s), "
            "fast-transport-belt ('red', 30/s), express-transport-belt ('blue', 45/s), "
            "turbo-transport-belt (Space Age, 60/s, also called 'green'). If the user "
            "names a belt by color/tier (e.g. 'a yellow belt'), pass the matching id "
            "explicitly — default with no --tier is the fastest tier your current save "
            f"can actually build (live tech-scoped), or the base/starter tier "
            f"({throughput.DEFAULT_BELT_TIER_ORDER[0]}) if live tech state isn't available."
        ),
    )
    p_belts.add_argument(
        "--force",
        default="player",
        help="Factorio force to tech-scope the default tier to (default: player)",
    )

    p_tech = sub.add_parser(
        "tech",
        help=(
            "what a technology unlocks, and whether the unlocked recipes combine into "
            "a recycling bundle — `recommend` already calls this for you; use directly "
            "only to inspect a bundle's math"
        ),
    )
    p_tech.add_argument("name", help="technology id or human name")
    p_tech.add_argument(
        "--rate",
        default=None,
        help=(
            "target rate for a detected bundle's anchor OUTPUT -- NOT the same thing as "
            "a raw-input/consumption rate you're trying to fully use up (e.g. from "
            "`belts`); the output:input ratio isn't 1:1, so don't plug an input rate in "
            "here directly -- use --consume instead for that case. A bare number is "
            "interpreted per --unit; suffix it directly instead (e.g. `15/s`, "
            "`900/min`) to set the unit inline and ignore --unit. Default: 60/min "
            "(1/sec) -- unlike `plan`/`expand`, a multi-recipe blend has no single "
            "'one machine' to size against."
        ),
    )
    p_tech.add_argument(
        "--unit",
        choices=["per-sec", "per-min"],
        default="per-sec",
        help="unit for a --rate/--consume value with no /s or /min suffix of its own",
    )
    p_tech.add_argument(
        "--anchor",
        default=None,
        help=(
            "force which external-output item to size a detected bundle against, "
            "when it has more than one (default: the deepest/most-downstream one)"
        ),
    )
    p_tech.add_argument(
        "--consume",
        default=None,
        metavar="ITEM=RATE",
        help=(
            "size the bundle so ITEM (a raw/external INPUT, e.g. copper-ore) is fully "
            "consumed at RATE instead of hitting an output target -- the answer to "
            "'I have this many belts/units of a raw input, what do I need to consume "
            "all of it?' without first having to work out the input:output ratio by "
            "hand. RATE is interpreted per --unit unless it carries its own /s or /min "
            "suffix (e.g. `copper-ore=15/s`, matching `belts`' own output format "
            "directly). Overrides --rate/--anchor for any bundle that has ITEM as an "
            "external input."
        ),
    )
    p_tech.add_argument(
        "--force", default="player", help="Factorio force to scope tech-status to (default: player)"
    )

    p_recommend = sub.add_parser(
        "recommend",
        help=(
            "START HERE for open-ended asks — the single best way to make a "
            "product right now — synthesizes `options` + `tech`"
        ),
    )
    p_recommend.add_argument("product", help="item/fluid id or human name")
    p_recommend.add_argument(
        "--rate",
        default=None,
        help=(
            "comparison yardstick, same default/semantics as `options` (60/min). A bare "
            "number is interpreted per --unit; suffix it directly instead (e.g. `15/s`, "
            "`900/min`) to set the unit inline and ignore --unit."
        ),
    )
    p_recommend.add_argument(
        "--unit",
        choices=["per-sec", "per-min"],
        default="per-sec",
        help="unit for a --rate value with no /s or /min suffix of its own",
    )
    p_recommend.add_argument("--max-depth", type=int, default=6)
    p_recommend.add_argument(
        "--force", default="player", help="Factorio force to scope tech-status to (default: player)"
    )
    p_recommend.add_argument(
        "--stop-items",
        default=None,
        help="comma-separated item ids to treat as raw inputs the user can already supply",
    )
    p_recommend.add_argument(
        "--no-auto-stop-raw",
        dest="auto_stop_raw",
        action="store_false",
        default=True,
        help="see `plan --no-auto-stop-raw` — same real-mined-resource default here",
    )
    p_recommend.add_argument(
        "--recipe",
        default=None,
        help="comma-separated ITEM=RECIPE overrides applied below the top-level choice",
    )

    return parser


_HANDLERS = {
    "status": cmd_status,
    "options": cmd_options,
    "plan": cmd_plan,
    "expand": cmd_expand,
    "recipe": cmd_recipe,
    "producers": cmd_producers,
    "consumers": cmd_consumers,
    "have": cmd_have,
    "belts": cmd_belts,
    "tech": cmd_tech,
    "recommend": cmd_recommend,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "status"
    if command == "status" and not hasattr(args, "force"):
        args.force = "player"
    handler = _HANDLERS[command]
    return asyncio.run(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
