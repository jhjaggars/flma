"""flma factory-planner CLI.

Combines recipe-mcp's calculation engine (machine-count math, recipe
expansion — see planner/_recipe_mcp_loader.py) with flma's live game state
(planner/live_state.py) to answer "how do I build a line for X at rate Y,
and what do I already have toward it" — without an MCP server or Hermes.

Usage:
    uv run python -m planner.cli                         # status (default)
    uv run python -m planner.cli plan "processing unit" --rate 10
    uv run python -m planner.cli expand iron-plate --rate 5
    uv run python -m planner.cli recipe electronic-circuit
    uv run python -m planner.cli producers iron-plate
    uv run python -m planner.cli consumers iron-plate
    uv run python -m planner.cli have iron-plate

See .claude/skills/factory-planner/SKILL.md for the workflows this backs, and
CLAUDE.md's factory-planner section for the modpack-alignment caveat.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from types import ModuleType

from planner import config, live_state, throughput
from planner._recipe_mcp_loader import load_async_database_class, load_engine


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def _fmt_num(n: float) -> str:
    if abs(n - round(n)) < 1e-6:
        return str(int(round(n)))
    return f"{n:.2f}"


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


def _is_tech_locked(item_id: str, tech_locked_notes: list[str]) -> bool:
    """Whether `item_id` has a tech-locked selection note. Exact-prefix
    match, not substring — notes read "{item_id}: ...", and a naive
    substring check would false-match e.g. "water" inside a
    "geothermal-water: ..." note."""
    return any(n.startswith(f"{item_id}: ") for n in tech_locked_notes)


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


def _print_tree(node: dict, indent: int = 0) -> None:
    pad = "  " * indent
    if node.get("leaf"):
        reason = node.get("stop_reason", "")
        print(f"{pad}{node['id']}  {_fmt_num(node['amount'])}  [{reason}]")
        return
    r = node["recipe"]
    print(f"{pad}{node['id']}  {_fmt_num(node['amount'])}  <- {r['id']} x{_fmt_num(r['batches'])}")
    for child in node.get("ingredients", []):
        _print_tree(child, indent + 1)


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
        "\nnext: `plan <product> --rate <n>` to design a line; `have <item>` to check current production."
    )
    return 0


# ---------------------------------------------------------------------------
# plan — the headline command
# ---------------------------------------------------------------------------


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
        print(f"  {b['count']:>5}x  {b['name']}  (speed {_fmt_num(b['crafting_speed'])})")

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
        belts = throughput.belts_needed(r_per_sec)
        line = (
            f"  {r['id']:<28} {_fmt_num(r['amount_per_min']):>10}/min"
            f"  ({_fmt_num(belts['belts'])} {belts['tier']} belts)"
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
        f"\nnext: `expand {result['product']} --rate {args.rate} --unit {args.unit}` "
        f"for the full BOM tree; `have <item>` to check current production of a specific input."
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
        return f"{r['id']} {_fmt_num(r['amount_per_min'])}/min{tag}"

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
    gs = live_state.open_game_state(config.SCRIPT_OUTPUT_DIR)
    db_tech_ids = await _db_tech_ids(engine)
    align = live_state.modpack_alignment(gs, db_tech_ids, force=args.force)

    rate_per_min = args.rate * 60.0 if args.unit == "per-sec" else args.rate

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

    result = await engine.plan_product(
        args.product, rate_per_min=rate_per_min, max_depth=args.max_depth, **scoping
    )

    if "error" in result:
        return _fail(result["error"])
    if result.get("ambiguous"):
        return _print_ambiguous(args.product, result["candidates"])

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

    amount = args.rate * 60.0 if args.unit == "per-sec" else args.rate
    stop_items = frozenset(s.strip() for s in (args.stop_items or "").split(",") if s.strip())
    if args.auto_stop_raw:
        stop_items = stop_items | await engine._auto_raw_items()
    totals_items: dict[str, float] = {}
    totals_fluids: dict[str, float] = {}
    selection_notes: list[str] = []
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
        overrides=_parse_recipe_overrides(args.recipe),
        ancestors=frozenset(),
        totals_items=totals_items,
        totals_fluids=totals_fluids,
        unresolved=[],
        alternates_map={},
        selection_notes=selection_notes,
        extra_unlocked=extra_unlocked,
        enforce_tech=bool(extra_unlocked),
    )

    print(
        f"expand: {row['name']}  amount={_fmt_num(amount)}/min-equivalent (max_depth={args.max_depth})"
    )
    print()
    _print_tree(tree)

    print(f"\nraw totals ({len(totals_items) + len(totals_fluids)}):")
    combined = sorted(totals_items.items(), key=lambda kv: -kv[1]) + sorted(
        totals_fluids.items(), key=lambda kv: -kv[1]
    )
    for k, v in combined[: args.top]:
        print(f"  {k}: {_fmt_num(v)}")

    tech_locked_notes = [n for n in selection_notes if "tech-locked" in n]
    if tech_locked_notes:
        print("\ntech-locked (falling back to raw input at your current research level):")
        for n in tech_locked_notes:
            print(f"  {n}")

    if len(combined) > args.top:
        print(f"  ... {len(combined) - args.top} more (raise --top to see all)")
    return 0


# ---------------------------------------------------------------------------
# recipe / producers / consumers — thin recipe lookups
# ---------------------------------------------------------------------------


async def _print_one_recipe(engine: ModuleType, name: str) -> bool:
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

    print(f"{row['name']}  ({row['translated_name']})")
    print(
        f"  category: {row['category']}   craft time: {row['energy']}s   enabled: {bool(row['enabled'])}"
        f"   main product: {row['main_product'] or '(none)'}"
    )
    print("  ingredients:")
    for i in ingredients:
        print(f"    {_fmt_num(i['amount'])}x {i['item_name']}")
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
        print(f"    {amt}x {p['item_name']}{prob}{rate}")
    return True


async def cmd_recipe(args: argparse.Namespace) -> int:
    engine = _make_engine()
    if engine is None:
        return 1
    ok = True
    for i, name in enumerate(args.name):
        if i > 0:
            print("---")
        ok = await _print_one_recipe(engine, name) and ok
    return 0 if ok else 1


async def cmd_producers(args: argparse.Namespace) -> int:
    engine = _make_engine()
    if engine is None:
        return 1
    rows = await engine.db.fetch_all(
        """SELECT r.name, r.category, r.enabled, r.main_product, r.energy,
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
    print(f"producers of {args.item} ({len(rows)}):")
    for r in rows:
        amt = (
            _fmt_num(r["amount"])
            if r["amount"] is not None
            else f"{r['amount_min']}-{r['amount_max']}"
        )
        flag = "" if r["enabled"] else "  [disabled]"
        # main_product == item_id means this is the recipe's actual purpose,
        # not a probabilistic/secondary byproduct of something else — the
        # auto-picker in `plan`/`expand` now prefers these (see engine.py's
        # _pick_producer Tier 1.5).
        main = "  [main product]" if r["main_product"] == args.item else ""
        rate = _rate_hint(r["amount"], r["probability"], r["energy"])
        print(f"  {r['name']:<32} {amt}x  ({r['category']}){rate}{flag}{main}")
    return 0


async def cmd_consumers(args: argparse.Namespace) -> int:
    engine = _make_engine()
    if engine is None:
        return 1
    rows = await engine.db.fetch_all(
        """SELECT r.name, r.category, r.enabled, i.amount
           FROM recipe_ingredients i JOIN recipes r ON r.name = i.recipe_name
           WHERE i.item_name = ? ORDER BY r.enabled DESC, r.translated_name COLLATE NOCASE""",
        (args.item,),
    )
    if not rows:
        print(
            f"nothing consumes '{args.item}' (use the exact internal id — try `expand {args.item}` to check)."
        )
        return 1
    print(f"consumers of {args.item} ({len(rows)}):")
    for r in rows:
        flag = "" if r["enabled"] else "  [disabled]"
        print(f"  {r['name']:<32} {_fmt_num(r['amount'])}x  ({r['category']}){flag}")
    return 0


# ---------------------------------------------------------------------------
# have — live-only: what do I already produce/store
# ---------------------------------------------------------------------------


async def cmd_have(args: argparse.Namespace) -> int:
    gs = live_state.open_game_state(config.SCRIPT_OUTPUT_DIR)
    net = live_state.net_production(gs, force=args.force)
    stock = live_state.buffered_stock(gs, force=args.force)
    if args.item not in net and args.item not in stock:
        print(
            f"no live data for '{args.item}' on force '{args.force}' (not produced/consumed or buffered)."
        )
        print("next: check the exact item id, or `producers <item>`/`consumers <item>` to find it.")
        return 1
    n = net.get(args.item, 0.0)
    s = stock.get(args.item, 0)
    verb = "producing" if n > 0 else "consuming" if n < 0 else "net-neutral on"
    print(
        f"{args.item}: {verb} {_fmt_num(abs(n))}/min, {s} buffered in logistics, force '{args.force}'"
    )
    return 0


# ---------------------------------------------------------------------------
# argument parsing / entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="planner",
        description="flma factory planner — live flma game state x recipe-mcp recipe data",
    )
    sub = parser.add_subparsers(dest="command")

    p_status = sub.add_parser(
        "status", help="modpack/live-data health check (default with no args)"
    )
    p_status.add_argument(
        "--force", default="player", help="Factorio force to scope to (default: player)"
    )

    p_plan = sub.add_parser("plan", help="design a production line for a target rate")
    p_plan.add_argument("product", help="item/fluid id or human name")
    p_plan.add_argument("--rate", type=float, default=1.0)
    p_plan.add_argument("--unit", choices=["per-sec", "per-min"], default="per-sec")
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
    p_expand.add_argument("--rate", type=float, default=1.0)
    p_expand.add_argument("--unit", choices=["per-sec", "per-min"], default="per-sec")
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

    p_recipe = sub.add_parser("recipe", help="full detail for one or more recipes")
    p_recipe.add_argument("name", nargs="+", help="recipe id(s) or human name(s)")

    p_producers = sub.add_parser("producers", help="what recipes produce this item (exact id)")
    p_producers.add_argument("item")

    p_consumers = sub.add_parser("consumers", help="what recipes consume this item (exact id)")
    p_consumers.add_argument("item")

    p_have = sub.add_parser(
        "have", help="live net production + buffered stock for an item (exact id)"
    )
    p_have.add_argument("item")
    p_have.add_argument(
        "--force", default="player", help="Factorio force to scope to (default: player)"
    )

    return parser


_HANDLERS = {
    "status": cmd_status,
    "plan": cmd_plan,
    "expand": cmd_expand,
    "recipe": cmd_recipe,
    "producers": cmd_producers,
    "consumers": cmd_consumers,
    "have": cmd_have,
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
