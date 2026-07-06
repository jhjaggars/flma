"""Decision-oriented helpers for `planner options`.

`plan`/`expand` both auto-pick a single recipe per item (via recipe-mcp's
`engine._pick_producer`) and never surface the alternatives it already
computed — every candidate producer, tagged `available`/`tech_locked`/
`excluded`/`stop_category`/`selected`, sits right there in `_expand_node`'s
`alternates_map` (engine.py) and gets thrown away by every existing caller.
`options` is what actually reads that data and turns it into a menu: the
distinct viable ways to make a target, with byproduct/impractical recipes
filtered out by default, so an agent can present trade-offs and ask the user
to choose instead of reconstructing the recipe graph by hand.

Nothing here reimplements engine math (expected yield, chain-selection
tiers) — `cli.cmd_options` calls `engine._effective_out`/`engine._expand_node`
directly and passes the *results* in here as plain numbers. That keeps this
module pure and unit-testable without a live recipes.db.
"""

from __future__ import annotations

import math

# A recipe that would need more machines than this to hit the practicality
# yardstick (see `classify_producer`) is hidden by default as "absurd" — the
# canonical case is a low-probability byproduct recipe that technically
# *can* produce the target but would need a small factory just to get a
# trickle of it (e.g. Pyanodons' byproduct-fishing chains producing a scarce
# ore at ~2% probability from bulk waste material).
ABSURD_MACHINE_THRESHOLD = 50


def classify_producer(
    *,
    recipe_id: str,
    is_main_product: bool,
    probability: float,
    eff_out: float,
    energy: float,
    fastest_speed: float,
    yardstick_per_min: float = 60.0,
) -> dict:
    """Classify one candidate recipe for `item_id` as a menu entry.

    `byproduct`: the item isn't this recipe's `main_product` AND the yield is
    probabilistic (<100%) — Factorio's own signal that this is a secondary
    output of some other line, not this item's actual purpose (mirrors
    engine._pick_producer's Tier 1 main_product preference, which exists for
    exactly this reason — see that function's docstring).

    `absurd`: even on the single fastest eligible machine for this recipe's
    category, hitting `yardstick_per_min` of the item would need an
    unreasonable machine count. A recipe can be a legitimate main_product
    and still be this impractical (a genuinely slow process) — `absurd` is
    independent of `byproduct`, either one alone is enough to hide by
    default.

    `hidden` is the OR of both — the "default-hide, opt-in reveal" rule
    `cmd_options` applies unless `--include-byproducts` is passed.

    All inputs are plain numbers/booleans the caller has already derived
    from a recipe-products row and `engine._effective_out`/a machine-speed
    lookup — this function does no DB or engine access itself.
    """
    if energy <= 0 or fastest_speed <= 0 or eff_out <= 0:
        machines = 0
    else:
        batches_per_min = yardstick_per_min / eff_out
        machines = math.ceil(batches_per_min * energy / (60.0 * fastest_speed))

    byproduct = (not is_main_product) and probability < 1.0
    absurd = machines > ABSURD_MACHINE_THRESHOLD
    return {
        "recipe_id": recipe_id,
        "is_main": is_main_product,
        "machines_per_yardstick": machines,
        "byproduct": byproduct,
        "absurd": absurd,
        "hidden": byproduct or absurd,
    }


def tree_stages(node: dict) -> int:
    """Number of recipe stages from `node` down to raw/leaf terminals —
    0 for a leaf, otherwise 1 + the deepest ingredient's stage count. Used to
    give each `options` menu entry a "N stage(s)" summary without needing the
    full machine-count math `plan` does."""
    if node.get("leaf"):
        return 0
    ingredients = node.get("ingredients") or []
    return 1 + max((tree_stages(c) for c in ingredients), default=0)


def tree_categories(node: dict, out: set[str]) -> None:
    """Walk an `_expand_node` tree collecting every recipe category used —
    the machine types a given production style needs, without computing
    exact counts (that's `plan`'s job once a style is chosen)."""
    if node.get("leaf"):
        return
    out.add(node["recipe"]["category"])
    for child in node.get("ingredients", []):
        tree_categories(child, out)


def deeper_choices(
    alternates_map: dict[str, list[dict]], top_item_id: str
) -> list[tuple[str, int]]:
    """(item_id, viable_count) for every item below `top_item_id` in an
    expansion's `alternates_map` that still has more than one tech-available
    ("available"-tagged) recipe candidate — i.e. a further decision point
    `options <item_id>` could drill into. Excludes `top_item_id` itself,
    since that's the choice already presented as the menu's own options.
    Sorted by item_id for stable output."""
    out: list[tuple[str, int]] = []
    for item_id, candidates in alternates_map.items():
        if item_id == top_item_id:
            continue
        n_available = sum(1 for c in candidates if c.get("tag") == "available")
        if n_available > 1:
            out.append((item_id, n_available))
    return sorted(out)
