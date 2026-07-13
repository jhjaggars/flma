"""Tests for planner/recipedb/engine.py's `_expand_node` (vendored from
recipe-mcp), via a small test-local `_expand_recipe_chain` helper.

recipe-mcp's own version of this test suite asserted against its MCP tool
`expand_recipe_chain` (src/server.py) — a thin formatting wrapper around
`engine._expand_node` that flma does not vendor (no MCP server here). This
port keeps the same fixtures/assertions but drives `_expand_node` directly
through a local helper that reproduces just the response shaping
`expand_recipe_chain` used to do, so the tests still exercise the real
engine math, not a reimplementation of it.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from planner.recipedb import engine
from planner.recipedb.build_db import build
from planner.recipedb.db import AsyncDatabase

pytestmark = pytest.mark.unit

# Multi-level recipe graph:
#   z-item  ← recipe-z (crafting)    : 1 y-item + 10 coolant → 2 z-item
#   z-item  ← recipe-z-alt (crafting): 5 w-item → 1 z-item
#   z-item  ← recipe-z-void (py-incineration): 2 w-item → 1 z-item  [excluded by default]
#   y-item  ← recipe-y (crafting)    : 3 x-item → 1 y-item
#   iron-ore ← iron-ore-mining (mining): → 1 iron-ore  [stop_category test]
#   cycle-a ← recipe-cycle-a         : 1 cycle-b → 1 cycle-a
#   cycle-b ← recipe-cycle-b         : 1 cycle-a → 1 cycle-b
#   x-item, w-item, coolant: no producer recipes (raw terminals)
EXPAND_RECIPES_JSON = {
    "game_version": "2.0.0",
    "items": {
        "z-item": {"name": "z-item", "type": "item", "translated_name": "Z Item"},
        "y-item": {"name": "y-item", "type": "item", "translated_name": "Y Item"},
        "x-item": {"name": "x-item", "type": "item", "translated_name": "X Item"},
        "w-item": {"name": "w-item", "type": "item", "translated_name": "W Item"},
        "v-item": {"name": "v-item", "type": "item", "translated_name": "V Item"},
        "u-item": {"name": "u-item", "type": "item", "translated_name": "U Item"},
        "iron-ore": {"name": "iron-ore", "type": "item", "translated_name": "Iron ore"},
        "cycle-a": {"name": "cycle-a", "type": "item", "translated_name": "Cycle A"},
        "cycle-b": {"name": "cycle-b", "type": "item", "translated_name": "Cycle B"},
    },
    "fluids": {
        "coolant": {"name": "coolant", "translated_name": "Coolant"},
    },
    "recipes": {
        "recipe-z": {
            "name": "recipe-z",
            "category": "crafting",
            "group": "test",
            "subgroup": "test",
            "energy": 1.0,
            "enabled": True,
            "order": "",
            "translated_name": "Recipe Z",
            "main_product": {"type": "item", "name": "z-item", "amount": 2},
            "ingredients": [
                {"type": "item", "name": "y-item", "amount": 1},
                {"type": "fluid", "name": "coolant", "amount": 10},
            ],
            "products": [{"type": "item", "name": "z-item", "amount": 2, "probability": 1.0}],
        },
        "recipe-z-alt": {
            "name": "recipe-z-alt",
            "category": "crafting",
            "group": "test",
            "subgroup": "test",
            "energy": 1.0,
            "enabled": True,
            "order": "",
            "translated_name": "Recipe Z (alt)",
            "main_product": {"type": "item", "name": "z-item", "amount": 1},
            "ingredients": [
                {"type": "item", "name": "w-item", "amount": 5},
            ],
            "products": [{"type": "item", "name": "z-item", "amount": 1, "probability": 1.0}],
        },
        "recipe-z-void": {
            "name": "recipe-z-void",
            "category": "py-incineration",
            "group": "test",
            "subgroup": "test",
            "energy": 1.0,
            "enabled": True,
            "order": "",
            "translated_name": "Recipe Z (void)",
            "main_product": None,
            "ingredients": [
                {"type": "item", "name": "w-item", "amount": 2},
            ],
            "products": [{"type": "item", "name": "z-item", "amount": 1, "probability": 1.0}],
        },
        "recipe-y": {
            "name": "recipe-y",
            "category": "crafting",
            "group": "test",
            "subgroup": "test",
            "energy": 1.0,
            "enabled": True,
            "order": "",
            "translated_name": "Recipe Y",
            "main_product": {"type": "item", "name": "y-item", "amount": 1},
            "ingredients": [
                {"type": "item", "name": "x-item", "amount": 3},
            ],
            "products": [{"type": "item", "name": "y-item", "amount": 1, "probability": 1.0}],
        },
        "iron-ore-mining": {
            "name": "iron-ore-mining",
            "category": "mining",
            "group": "test",
            "subgroup": "test",
            "energy": 1.0,
            "enabled": True,
            "order": "",
            "translated_name": "Iron ore (mining)",
            "main_product": {"type": "item", "name": "iron-ore", "amount": 1},
            "ingredients": [],
            "products": [{"type": "item", "name": "iron-ore", "amount": 1, "probability": 1.0}],
        },
        "recipe-cycle-a": {
            "name": "recipe-cycle-a",
            "category": "crafting",
            "group": "test",
            "subgroup": "test",
            "energy": 1.0,
            "enabled": True,
            "order": "",
            "translated_name": "Recipe Cycle A",
            "main_product": {"type": "item", "name": "cycle-a", "amount": 1},
            "ingredients": [
                {"type": "item", "name": "cycle-b", "amount": 1},
            ],
            "products": [{"type": "item", "name": "cycle-a", "amount": 1, "probability": 1.0}],
        },
        "recipe-cycle-b": {
            "name": "recipe-cycle-b",
            "category": "crafting",
            "group": "test",
            "subgroup": "test",
            "energy": 1.0,
            "enabled": True,
            "order": "",
            "translated_name": "Recipe Cycle B",
            "main_product": {"type": "item", "name": "cycle-b", "amount": 1},
            "ingredients": [
                {"type": "item", "name": "cycle-a", "amount": 1},
            ],
            "products": [{"type": "item", "name": "cycle-b", "amount": 1, "probability": 1.0}],
        },
        # w-item has TWO producers: "a-alt-w" (alphabetically first) and "w-item"
        # (direct name match). Tests that direct-name match wins over alphabetic order.
        "a-alt-w": {
            "name": "a-alt-w",
            "category": "crafting",
            "group": "test",
            "subgroup": "test",
            "energy": 1.0,
            "enabled": True,
            "order": "",
            "translated_name": "Alt W producer",
            "main_product": None,
            "ingredients": [{"type": "item", "name": "x-item", "amount": 1}],
            "products": [{"type": "item", "name": "w-item", "amount": 1, "probability": 1.0}],
        },
        "w-item": {
            "name": "w-item",
            "category": "crafting",
            "group": "test",
            "subgroup": "test",
            "energy": 1.0,
            "enabled": True,
            "order": "",
            "translated_name": "W Item (direct recipe)",
            "main_product": {"type": "item", "name": "w-item", "amount": 1},
            "ingredients": [{"type": "item", "name": "x-item", "amount": 2}],
            "products": [{"type": "item", "name": "w-item", "amount": 1, "probability": 1.0}],
        },
        # v-item has TWO producers: "aaa-byproduct-v" (alphabetically first,
        # v-item is only a 30%-probability secondary byproduct of making
        # u-item) and "zzz-direct-v" (alphabetically last, v-item is its
        # main_product). Tests that a main-product match beats alphabetic
        # order when there's no direct name match either way. u-item is a
        # fresh, otherwise-unused item so this doesn't shadow y-item's own
        # producer selection used by other tests.
        "aaa-byproduct-v": {
            "name": "aaa-byproduct-v",
            "category": "crafting",
            "group": "test",
            "subgroup": "test",
            "energy": 1.0,
            "enabled": True,
            "order": "",
            "translated_name": "Byproduct V producer",
            "main_product": {"type": "item", "name": "u-item", "amount": 1},
            "ingredients": [{"type": "item", "name": "x-item", "amount": 1}],
            "products": [
                {"type": "item", "name": "u-item", "amount": 1, "probability": 1.0},
                {"type": "item", "name": "v-item", "amount": 1, "probability": 0.3},
            ],
        },
        "zzz-direct-v": {
            "name": "zzz-direct-v",
            "category": "crafting",
            "group": "test",
            "subgroup": "test",
            "energy": 1.0,
            "enabled": True,
            "order": "",
            "translated_name": "V Item (direct recipe)",
            "main_product": {"type": "item", "name": "v-item", "amount": 1},
            "ingredients": [{"type": "item", "name": "x-item", "amount": 1}],
            "products": [{"type": "item", "name": "v-item", "amount": 1, "probability": 1.0}],
        },
    },
    "groups": {},
    "entities": {},
}


@pytest.fixture
def test_db(tmp_path: Path) -> AsyncDatabase:
    json_path = tmp_path / "recipes.json"
    json_path.write_text(json.dumps(EXPAND_RECIPES_JSON))
    db_path = str(tmp_path / "recipes.db")
    build(str(json_path), db_path)
    return AsyncDatabase(db_path)


@pytest.fixture(autouse=True)
def patch_db(test_db: AsyncDatabase):
    with patch("planner.recipedb.engine.db", test_db):
        yield


async def _expand_recipe_chain(
    product: str,
    amount: float = 1.0,
    max_depth: int = 5,
    stop_items: list[str] | None = None,
    stop_categories: list[str] | None = None,
    exclude_categories: list[str] | None = None,
    prefer_enabled: bool = True,
    recipe_overrides: dict[str, str] | None = None,
    include_alternates: bool = True,
    compact: bool = False,
) -> dict:
    """Reproduces recipe-mcp's `expand_recipe_chain` MCP-tool response shape
    (src/server.py) on top of the vendored `engine._expand_node` — see the
    module docstring for why this lives here rather than in engine.py."""
    max_depth = max(1, min(max_depth, 15))
    exclude_cats = frozenset(
        exclude_categories if exclude_categories is not None else engine._DEFAULT_EXCLUDE
    )
    stop_cats = frozenset(stop_categories or [])
    stop_set = frozenset(stop_items or [])
    overrides: dict[str, str] = dict(recipe_overrides or {})

    name_row = await engine.db.fetch_one(
        "SELECT name, kind, translated_name FROM names WHERE name = ?", (product,)
    )
    if name_row is None:
        name_row = await engine.db.fetch_one(
            "SELECT name, kind, translated_name FROM names "
            "WHERE translated_name = ? COLLATE NOCASE",
            (product,),
        )
    if name_row is None:
        fuzzy = await engine.db.fetch_all(
            """SELECT name, kind, translated_name FROM names
               WHERE name LIKE ? COLLATE NOCASE
                  OR translated_name LIKE ? COLLATE NOCASE
               ORDER BY translated_name COLLATE NOCASE
               LIMIT 10""",
            (f"%{product}%", f"%{product}%"),
        )
        if not fuzzy:
            return {"error": f"No item or fluid found matching '{product}'."}
        if len(fuzzy) > 1:
            return {
                "ambiguous": True,
                "query": product,
                "candidates": [
                    {"id": r["name"], "kind": r["kind"], "name": r["translated_name"]}
                    for r in fuzzy
                ],
                "hint": "Pass the exact 'id' as the product parameter.",
            }
        name_row = fuzzy[0]

    item_id: str = name_row["name"]
    item_kind: str = name_row["kind"]

    totals_items: dict[str, float] = {}
    totals_fluids: dict[str, float] = {}
    unresolved: list[dict] = []
    alternates_map: dict[str, list[dict]] = {}
    selection_notes: list[str] = []

    tree = await engine._expand_node(
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

    async def _lookup_name(iid: str) -> str:
        r = await engine.db.fetch_one("SELECT translated_name FROM names WHERE name = ?", (iid,))
        return r["translated_name"] if r else iid

    totals_out = [
        {"id": iid, "name": await _lookup_name(iid), "amount": amt}
        for iid, amt in sorted(totals_items.items(), key=lambda x: -x[1])
    ]
    fluids_out = [
        {"id": fid, "name": await _lookup_name(fid), "amount": amt}
        for fid, amt in sorted(totals_fluids.items(), key=lambda x: -x[1])
    ]

    seen: dict[str, dict] = {}
    for entry in unresolved:
        seen.setdefault(entry["id"], entry)
    unresolved_out = list(seen.values())

    result: dict = {
        "totals": totals_out,
        "fluids": fluids_out,
        "unresolved": unresolved_out,
        "selection_notes": selection_notes,
        "parameters": {
            "product": item_id,
            "amount": amount,
            "max_depth": max_depth,
            "prefer_enabled": prefer_enabled,
            "exclude_categories": sorted(exclude_cats),
            "stop_categories": sorted(stop_cats),
            "stop_items": sorted(stop_set),
            "recipe_overrides": overrides,
            "compact": compact,
        },
    }
    if not compact:
        result["tree"] = tree
    if include_alternates:
        result["alternates"] = alternates_map
    return result


async def test_expand_basic_tree_structure() -> None:
    """Tree root is z-item, expanded via recipe-z, with correct batches."""
    result = await _expand_recipe_chain("z-item", amount=1.0)

    assert "tree" in result
    tree = result["tree"]
    assert tree["id"] == "z-item"
    assert tree["leaf"] is False
    assert tree["recipe"]["id"] == "recipe-z"
    # 1 z-item from recipe-z that yields 2 per batch = 0.5 batches
    assert abs(tree["recipe"]["batches"] - 0.5) < 1e-6
    assert abs(tree["recipe"]["output_per_batch"] - 2.0) < 1e-6

    ing_ids = {c["id"] for c in tree["ingredients"]}
    assert "y-item" in ing_ids
    assert "coolant" in ing_ids


async def test_expand_totals() -> None:
    """Raw leaf amounts accumulate in totals (items) and fluids (fluids)."""
    result = await _expand_recipe_chain("z-item", amount=1.0)

    # Chain: z-item ← recipe-z (0.5 batches) ← y-item (0.5) + coolant (5)
    #        y-item ← recipe-y (0.5 batches) ← x-item (1.5)
    totals_by_id = {t["id"]: t["amount"] for t in result["totals"]}
    assert abs(totals_by_id.get("x-item", 0) - 1.5) < 1e-6

    fluids_by_id = {f["id"]: f["amount"] for f in result["fluids"]}
    assert abs(fluids_by_id.get("coolant", 0) - 5.0) < 1e-6


async def test_expand_amount_scaling() -> None:
    """Requesting amount=4 scales all totals by 4×."""
    result_1 = await _expand_recipe_chain("z-item", amount=1.0)
    result_4 = await _expand_recipe_chain("z-item", amount=4.0)

    totals_1 = {t["id"]: t["amount"] for t in result_1["totals"]}
    totals_4 = {t["id"]: t["amount"] for t in result_4["totals"]}
    for iid in totals_1:
        assert abs(totals_4[iid] - totals_1[iid] * 4) < 1e-6

    fluids_1 = {f["id"]: f["amount"] for f in result_1["fluids"]}
    fluids_4 = {f["id"]: f["amount"] for f in result_4["fluids"]}
    for fid in fluids_1:
        assert abs(fluids_4[fid] - fluids_1[fid] * 4) < 1e-6


async def test_expand_excludes_incineration_by_default() -> None:
    """py-incineration recipes are skipped; recipe-z is preferred over recipe-z-void."""
    result = await _expand_recipe_chain("z-item")

    assert result["tree"]["recipe"]["id"] == "recipe-z"

    alternates = result.get("alternates", {})
    void_alt = next((a for a in alternates.get("z-item", []) if a["id"] == "recipe-z-void"), None)
    assert void_alt is not None
    assert void_alt["tag"] == "excluded"


async def test_expand_stop_items() -> None:
    """Items in stop_items become leaf nodes; their amounts go into totals."""
    result = await _expand_recipe_chain("z-item", stop_items=["y-item"])

    tree = result["tree"]
    y_node = next((c for c in tree["ingredients"] if c["id"] == "y-item"), None)
    assert y_node is not None
    assert y_node["leaf"] is True
    assert y_node["stop_reason"] == "stop_item"

    totals_by_id = {t["id"]: t["amount"] for t in result["totals"]}
    assert "y-item" in totals_by_id


async def test_expand_max_depth() -> None:
    """Nodes at max_depth are leaves with stop_reason='max_depth' and go into unresolved."""
    result = await _expand_recipe_chain("z-item", max_depth=1)

    # depth 0 = z-item (expands), depth 1 = y-item + coolant (hit limit)
    tree = result["tree"]
    y_node = next((c for c in tree["ingredients"] if c["id"] == "y-item"), None)
    assert y_node is not None
    assert y_node["leaf"] is True
    assert y_node["stop_reason"] == "max_depth"

    unresolved_ids = {u["id"] for u in result["unresolved"]}
    assert "y-item" in unresolved_ids


async def test_expand_stop_categories() -> None:
    """Items whose only producers are in stop_categories are treated as raw terminals."""
    result = await _expand_recipe_chain("iron-ore", stop_categories=["mining"])

    tree = result["tree"]
    assert tree["leaf"] is True
    assert tree["stop_reason"] == "stop_category"


async def test_expand_no_recipe_root() -> None:
    """An item with no producer recipe returns a leaf immediately."""
    result = await _expand_recipe_chain("x-item")

    tree = result["tree"]
    assert tree["leaf"] is True
    assert tree["stop_reason"] == "no_recipe"

    totals_by_id = {t["id"]: t["amount"] for t in result["totals"]}
    assert abs(totals_by_id.get("x-item", 0) - 1.0) < 1e-6


async def test_expand_cycle_detection() -> None:
    """Cycles are detected via the ancestors frozenset; the revisited node is a leaf."""
    result = await _expand_recipe_chain("cycle-a")

    # cycle-a → recipe-cycle-a → cycle-b → recipe-cycle-b → cycle-a (CYCLE)
    tree = result["tree"]
    assert tree["leaf"] is False

    cycle_b_node = next((c for c in tree["ingredients"] if c["id"] == "cycle-b"), None)
    assert cycle_b_node is not None
    assert cycle_b_node["leaf"] is False

    cycle_a_revisit = next((c for c in cycle_b_node["ingredients"] if c["id"] == "cycle-a"), None)
    assert cycle_a_revisit is not None
    assert cycle_a_revisit["leaf"] is True
    assert cycle_a_revisit["stop_reason"] == "cycle"


async def test_expand_recipe_override() -> None:
    """recipe_overrides forces selection of a specific recipe for an item."""
    result = await _expand_recipe_chain(
        "z-item",
        recipe_overrides={"z-item": "recipe-z-alt"},
    )

    tree = result["tree"]
    assert tree["recipe"]["id"] == "recipe-z-alt"

    ing_ids = {c["id"] for c in tree["ingredients"]}
    assert "w-item" in ing_ids
    assert "y-item" not in ing_ids

    # recipe-z-alt: 5 w-item → 1 z-item; w-item recipe: 2 x-item → 1 w-item
    # So 5 w-items × 2 x-item = 10 x-item total
    totals_by_id = {t["id"]: t["amount"] for t in result["totals"]}
    assert abs(totals_by_id.get("x-item", 0) - 10.0) < 1e-6


async def test_expand_include_alternates_true() -> None:
    """include_alternates=True (default) includes per-item recipe candidates."""
    result = await _expand_recipe_chain("z-item", include_alternates=True)

    assert "alternates" in result
    assert "z-item" in result["alternates"]

    selected = next((a for a in result["alternates"]["z-item"] if a["selected"]), None)
    assert selected is not None
    assert selected["id"] == "recipe-z"


async def test_expand_include_alternates_false() -> None:
    """include_alternates=False omits the alternates key."""
    result = await _expand_recipe_chain("z-item", include_alternates=False)
    assert "alternates" not in result


async def test_expand_fuzzy_name_match() -> None:
    """Product can be specified by translated name (exact translated match)."""
    result = await _expand_recipe_chain("Z Item")

    assert "tree" in result
    assert result["tree"]["id"] == "z-item"


async def test_expand_unknown_product() -> None:
    """Completely unknown product returns an error dict."""
    result = await _expand_recipe_chain("totally-nonexistent-item-xyz")
    assert "error" in result


async def test_expand_selection_notes_populated() -> None:
    """selection_notes contains transparent explanations for each recipe chosen."""
    result = await _expand_recipe_chain("z-item")

    notes = result["selection_notes"]
    assert len(notes) > 0
    all_notes = " ".join(notes)
    assert "z-item" in all_notes
    assert "recipe-z" in all_notes


async def test_expand_parameters_echoed() -> None:
    """The parameters block echoes back the resolved expansion settings."""
    result = await _expand_recipe_chain("z-item", amount=2.0, max_depth=3)

    params = result["parameters"]
    assert params["product"] == "z-item"
    assert params["amount"] == 2.0
    assert params["max_depth"] == 3
    assert "py-incineration" in params["exclude_categories"]


async def test_expand_direct_name_match_wins() -> None:
    """Recipe whose id equals the item id takes priority over alphabetic first."""
    # w-item has two producers: "a-alt-w" (alphabetically first) and "w-item" (name match).
    # The direct name match should win.
    result = await _expand_recipe_chain("w-item")

    tree = result["tree"]
    assert tree["leaf"] is False
    assert tree["recipe"]["id"] == "w-item"

    notes = " ".join(result["selection_notes"])
    assert "direct name match" in notes


async def test_expand_main_product_tier_beats_alphabetic_byproduct() -> None:
    """A recipe whose main_product is the requested item wins over an
    alphabetically-earlier recipe that only produces it as a secondary,
    probabilistic byproduct of something else — neither is a direct name
    match, so this isolates the new Tier 1.5 from Tier 1."""
    result = await _expand_recipe_chain("v-item")

    tree = result["tree"]
    assert tree["leaf"] is False
    assert tree["recipe"]["id"] == "zzz-direct-v"

    notes = " ".join(result["selection_notes"])
    assert "main product" in notes


async def test_expand_compact_omits_tree() -> None:
    """compact=True omits the 'tree' key but keeps totals, fluids, unresolved, notes."""
    result = await _expand_recipe_chain("z-item", compact=True)

    assert "tree" not in result
    assert "totals" in result
    assert "fluids" in result
    assert "unresolved" in result
    assert "selection_notes" in result
    assert result["parameters"]["compact"] is True


async def test_expand_compact_false_includes_tree() -> None:
    """compact=False (default) still includes the tree."""
    result = await _expand_recipe_chain("z-item", compact=False)
    assert "tree" in result
