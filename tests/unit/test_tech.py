"""Tests for planner/recipedb/engine.py's tech-scoping functions (vendored
from recipe-mcp): `unlocked_recipes_for_techs`, `_techs_unlocking`, and
`plan_product`'s use of them via `assume_researched`.

recipe-mcp's own version of this test suite asserted against four MCP tools
defined directly in its `server.py` (`find_technologies_unlocking_recipe`,
`find_recipes_unlocked_by_technology`, `get_research_requirements`,
`list_researchable_technologies`) — pure-SQL lookups that don't touch
`engine.py` at all and that flma doesn't vendor (planner's own `cli.py`
implements the tech lookups it needs directly, see `_db_tech_ids` and the
`tech`/`recommend` commands). Rather than reimplementing that unvendored
server.py logic just to have tests for it, this port targets the actual
engine functions flma's planner calls (`engine.unlocked_recipes_for_techs`
in a dozen call sites across `cli.py`, `engine._techs_unlocking` inside
`_pick_producer`) using the same recipe/tech fixture graph.
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

# Recipe / tech graph:
#   z-item  <- recipe-z  (enabled=false, unlocked by tech-z)
#   y-item  <- recipe-y  (enabled=false, unlocked by tech-y)
#   x-item  <- recipe-x  (enabled=true,  no unlock tech — initially available)
#   w-item  <- (no recipe)
#
#   tech-z: count=50, prereqs=[tech-y], unlocks=[recipe-z]
#            ingredients: [automation-science-pack:1, logistic-science-pack:1]
#   tech-y: count=25, prereqs=[],      unlocks=[recipe-y]
#            ingredients: [automation-science-pack:1]
TECH_RECIPES_JSON = {
    "game_version": "2.0.0",
    "items": {
        "z-item": {"name": "z-item", "type": "item", "translated_name": "Z Item"},
        "y-item": {"name": "y-item", "type": "item", "translated_name": "Y Item"},
        "x-item": {"name": "x-item", "type": "item", "translated_name": "X Item"},
        "w-item": {"name": "w-item", "type": "item", "translated_name": "W Item"},
        "automation-science-pack": {
            "name": "automation-science-pack",
            "type": "item",
            "translated_name": "Automation science pack",
        },
        "logistic-science-pack": {
            "name": "logistic-science-pack",
            "type": "item",
            "translated_name": "Logistic science pack",
        },
    },
    "fluids": {},
    "recipes": {
        "recipe-z": {
            "name": "recipe-z",
            "category": "crafting",
            "group": "test",
            "subgroup": "test",
            "energy": 1.0,
            "enabled": False,
            "order": "",
            "translated_name": "Recipe Z",
            "main_product": {"type": "item", "name": "z-item", "amount": 1},
            "ingredients": [{"type": "item", "name": "y-item", "amount": 1}],
            "products": [{"type": "item", "name": "z-item", "amount": 1, "probability": 1.0}],
        },
        "recipe-y": {
            "name": "recipe-y",
            "category": "crafting",
            "group": "test",
            "subgroup": "test",
            "energy": 1.0,
            "enabled": False,
            "order": "",
            "translated_name": "Recipe Y",
            "main_product": {"type": "item", "name": "y-item", "amount": 1},
            "ingredients": [{"type": "item", "name": "x-item", "amount": 2}],
            "products": [{"type": "item", "name": "y-item", "amount": 1, "probability": 1.0}],
        },
        "recipe-x": {
            "name": "recipe-x",
            "category": "crafting",
            "group": "test",
            "subgroup": "test",
            "energy": 1.0,
            "enabled": True,
            "order": "",
            "translated_name": "Recipe X",
            "main_product": {"type": "item", "name": "x-item", "amount": 1},
            "ingredients": [{"type": "item", "name": "w-item", "amount": 1}],
            "products": [{"type": "item", "name": "x-item", "amount": 1, "probability": 1.0}],
        },
    },
    "technologies": {
        "tech-z": {
            "name": "tech-z",
            "translated_name": "Tech Z",
            "enabled": True,
            "researched": False,
            "prerequisites": ["tech-y"],
            "recipes_unlocked": ["recipe-z"],
            "unit_count": 50,
            "unit_count_formula": None,
            "unit_energy": 30.0,
            "unit_ingredients": [
                {"name": "automation-science-pack", "amount": 1},
                {"name": "logistic-science-pack", "amount": 1},
            ],
        },
        "tech-y": {
            "name": "tech-y",
            "translated_name": "Tech Y",
            "enabled": True,
            "researched": False,
            "prerequisites": [],
            "recipes_unlocked": ["recipe-y"],
            "unit_count": 25,
            "unit_count_formula": None,
            "unit_energy": 30.0,
            "unit_ingredients": [
                {"name": "automation-science-pack", "amount": 1},
            ],
        },
    },
    "groups": {},
    "entities": {},
}


@pytest.fixture
def test_db(tmp_path: Path) -> AsyncDatabase:
    json_path = tmp_path / "recipes.json"
    json_path.write_text(json.dumps(TECH_RECIPES_JSON))
    db_path = str(tmp_path / "recipes.db")
    build(str(json_path), db_path)
    return AsyncDatabase(db_path)


@pytest.fixture(autouse=True)
def patch_db(test_db: AsyncDatabase):
    with patch("planner.recipedb.engine.db", test_db):
        yield


# ---------------------------------------------------------------------------
# engine.unlocked_recipes_for_techs — planner/cli.py's main tech-scoping call
# ---------------------------------------------------------------------------


async def test_unlocked_recipes_for_single_tech() -> None:
    """A single tech id resolves to the recipe(s) it unlocks."""
    result = await engine.unlocked_recipes_for_techs(["tech-z"])
    assert result == frozenset({"recipe-z"})


async def test_unlocked_recipes_union_across_techs() -> None:
    """Multiple assumed-researched techs union their unlocked recipes."""
    result = await engine.unlocked_recipes_for_techs(["tech-y", "tech-z"])
    assert result == frozenset({"recipe-y", "recipe-z"})


async def test_unlocked_recipes_empty_for_no_techs() -> None:
    """None/empty input returns an empty frozenset, not an error."""
    assert await engine.unlocked_recipes_for_techs(None) == frozenset()
    assert await engine.unlocked_recipes_for_techs([]) == frozenset()


async def test_unlocked_recipes_unknown_tech_contributes_nothing() -> None:
    """An unrecognized tech id is silently ignored, not an error."""
    result = await engine.unlocked_recipes_for_techs(["no-such-tech"])
    assert result == frozenset()


# ---------------------------------------------------------------------------
# engine._techs_unlocking — used by _pick_producer for tech_locked notes
# ---------------------------------------------------------------------------


async def test_techs_unlocking_reports_translated_name() -> None:
    """The reverse lookup returns translated tech names, not ids."""
    result = await engine._techs_unlocking(["recipe-z"])
    assert result == ["Tech Z"]


async def test_techs_unlocking_multiple_recipes_sorted() -> None:
    """Multiple recipes' unlocking techs come back de-duplicated and sorted."""
    result = await engine._techs_unlocking(["recipe-y", "recipe-z"])
    assert result == ["Tech Y", "Tech Z"]


async def test_techs_unlocking_empty_input() -> None:
    assert await engine._techs_unlocking([]) == []


async def test_techs_unlocking_recipe_with_no_unlock_tech() -> None:
    """recipe-x is enabled from the start — no technology unlocks it."""
    assert await engine._techs_unlocking(["recipe-x"]) == []


# ---------------------------------------------------------------------------
# plan_product's tech-scoping integration (assume_researched -> enforce_tech)
# ---------------------------------------------------------------------------


async def test_plan_product_locks_recipe_behind_unresearched_tech() -> None:
    """With only_enabled=True and nothing assumed researched, z-item's only
    recipe (tech-locked, enabled=False) falls back to a raw input rather than
    being silently built anyway. (assume_researched=[] alone does NOT turn on
    enforcement — plan_product only scopes by tech when only_enabled=True or
    assume_researched resolves to at least one unlocked recipe, matching
    engine.plan_product's own docstring.)"""
    result = await engine.plan_product("z-item", only_enabled=True, auto_stop_raw=False)

    raw_ids = {r["id"] for r in result["raw_inputs"]}
    assert "z-item" in raw_ids
    assert any(u["id"] == "z-item" and u["reason"] == "tech_locked" for u in result["unresolved"])


async def test_plan_product_assume_researched_unlocks_the_chain() -> None:
    """Assuming tech-z (and its prereq tech-y) researched lets the full
    z-item -> y-item -> x-item chain expand instead of stopping at z-item."""
    result = await engine.plan_product(
        "z-item", assume_researched=["tech-y", "tech-z"], auto_stop_raw=False
    )

    raw_ids = {r["id"] for r in result["raw_inputs"]}
    assert "z-item" not in raw_ids
    assert "w-item" in raw_ids  # the true raw terminal of the fully-unlocked chain
    assert result["unresolved"] == []


async def test_plan_product_partial_research_still_blocks_deeper_recipe() -> None:
    """Assuming only tech-y researched (not tech-z) unlocks y-item's recipe
    but still leaves z-item's own recipe tech-locked."""
    result = await engine.plan_product(
        "z-item", assume_researched=["tech-y"], auto_stop_raw=False
    )

    raw_ids = {r["id"] for r in result["raw_inputs"]}
    assert "z-item" in raw_ids  # recipe-z still locked -> z-item itself is raw
    assert "y-item" not in raw_ids  # never reached, since z-item didn't expand
