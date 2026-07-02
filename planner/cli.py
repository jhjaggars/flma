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

    result = await engine.plan_product(
        args.product, rate_per_min=rate_per_min, max_depth=args.max_depth, **scoping
    )

    if "error" in result:
        return _fail(result["error"])
    if result.get("ambiguous"):
        return _print_ambiguous(args.product, result["candidates"])

    net = live_state.net_production(gs, force=args.force) if align["aligned"] else {}
    stock = live_state.buffered_stock(gs, force=args.force) if align["aligned"] else {}

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

    tech_locked_notes = [n for n in result.get("selection_notes", []) if "tech-locked" in n]
    if tech_locked_notes:
        print("\ntech-locked (falling back to raw input at your current research level):")
        for n in tech_locked_notes:
            print(f"  {n}")

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
        overrides={},
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


async def cmd_recipe(args: argparse.Namespace) -> int:
    engine = _make_engine()
    if engine is None:
        return 1
    db = engine.db
    row = await db.fetch_one("SELECT * FROM recipes WHERE name = ?", (args.name,))
    if row is None:
        row = await db.fetch_one(
            "SELECT * FROM recipes WHERE translated_name = ? COLLATE NOCASE", (args.name,)
        )
    if row is None:
        candidates = await db.fetch_all(
            """SELECT name, translated_name FROM recipes
               WHERE name LIKE ? COLLATE NOCASE OR translated_name LIKE ? COLLATE NOCASE
               ORDER BY translated_name COLLATE NOCASE LIMIT 10""",
            (f"%{args.name}%", f"%{args.name}%"),
        )
        if not candidates:
            print(f"no recipe found matching '{args.name}'.")
            return 1
        if len(candidates) > 1:
            print(f"'{args.name}' is ambiguous — candidates:")
            for c in candidates:
                print(f"  {c['name']:<30} {c['translated_name']}")
            return 1
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
    )
    print("  ingredients:")
    for i in ingredients:
        print(f"    {_fmt_num(i['amount'])}x {i['item_name']}")
    print("  products:")
    for p in products:
        amt = (
            _fmt_num(p["amount"])
            if p["amount"] is not None
            else f"{p['amount_min']}-{p['amount_max']}"
        )
        prob = f" @ {p['probability'] * 100:.0f}%" if p["probability"] not in (None, 1.0) else ""
        print(f"    {amt}x {p['item_name']}{prob}")
    return 0


async def cmd_producers(args: argparse.Namespace) -> int:
    engine = _make_engine()
    if engine is None:
        return 1
    rows = await engine.db.fetch_all(
        """SELECT r.name, r.category, r.enabled, p.amount, p.amount_min, p.amount_max
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
        print(f"  {r['name']:<32} {amt}x  ({r['category']}){flag}")
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

    p_recipe = sub.add_parser("recipe", help="full detail for one recipe")
    p_recipe.add_argument("name", help="recipe id or human name")

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
