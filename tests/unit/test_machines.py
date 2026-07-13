"""Tests for planner/recipedb/engine.py's `plan_product` (vendored from
recipe-mcp) — machine/drill selection and count math.

recipe-mcp's own version of this test suite additionally covered four
pure-SQL MCP tools (`find_machines_for_category`, `get_machine`,
`get_recipe`, `find_drills_for_resource_category`) defined directly in
`server.py` with no `engine.py` involvement at all — flma doesn't vendor
those; `planner/cli.py`'s own `producers`/`consumers`/`power`/`recipe`
commands run their own equivalent SQL directly (see `tests/unit/test_cli.py`
for their coverage) rather than reusing recipe-mcp's server-side lookups.
This port keeps only the `plan_factory` tests, retargeted onto
`engine.plan_product` directly — `plan_factory` was itself just a thin
pass-through to it (see recipe-mcp's `server.py` module docstring), so the
math under test is unchanged.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from unittest.mock import patch

import pytest
from planner.recipedb import engine
from planner.recipedb.build_db import build
from planner.recipedb.db import AsyncDatabase

pytestmark = pytest.mark.unit

# Fixture data:
#
# Recipes:
#   iron-plate  <- smelt-iron   (smelting,  energy=3.2, 2×iron-ore → 1×iron-plate)
#   iron-gear   <- iron-gears   (crafting,  energy=0.5, 2×iron-plate → 1×iron-gear)
#
# Entities:
#   stone-furnace (furnace, smelting, speed=1.0)
#   electric-furnace (furnace, smelting, speed=2.0)
#   assembling-machine-1 (assembling-machine, crafting, speed=0.5)
#   assembling-machine-2 (assembling-machine, crafting, speed=0.75)
#   basic-mining-drill   (mining-drill, resource_categories=[basic-solid], speed=0.5)
#   iron-ore             (resource, category=basic-solid, mining_time=1.0, product=iron-ore)

MACHINES_JSON: dict = {
    "game_version": "2.0",
    "items": {
        "iron-plate": {"name": "iron-plate", "type": "item", "translated_name": "Iron Plate"},
        "iron-gear": {"name": "iron-gear", "type": "item", "translated_name": "Iron Gear"},
        "iron-ore": {"name": "iron-ore", "type": "item", "translated_name": "Iron Ore"},
        "ore-lead-test": {
            "name": "ore-lead-test",
            "type": "item",
            "translated_name": "Lead Ore (test)",
        },
        "lead-plate-test": {
            "name": "lead-plate-test",
            "type": "item",
            "translated_name": "Lead Plate (test)",
        },
        "stone-furnace": {
            "name": "stone-furnace",
            "type": "item",
            "translated_name": "Stone Furnace",
        },
        "electric-furnace": {
            "name": "electric-furnace",
            "type": "item",
            "translated_name": "Electric Furnace",
        },
        "assembling-machine-1": {
            "name": "assembling-machine-1",
            "type": "item",
            "translated_name": "Assembling Machine 1",
        },
        "assembling-machine-2": {
            "name": "assembling-machine-2",
            "type": "item",
            "translated_name": "Assembling Machine 2",
        },
    },
    "fluids": {
        "salt-water": {"name": "salt-water", "translated_name": "Salt Water"},
    },
    "recipes": {
        "smelt-iron": {
            "name": "smelt-iron",
            "category": "smelting",
            "group": "test",
            "subgroup": "test",
            "energy": 3.2,
            "enabled": True,
            "order": "",
            "translated_name": "Smelt Iron",
            "main_product": {"type": "item", "name": "iron-plate", "amount": 1},
            "ingredients": [{"type": "item", "name": "iron-ore", "amount": 2}],
            "products": [{"type": "item", "name": "iron-plate", "amount": 1, "probability": 1.0}],
        },
        "iron-gears": {
            "name": "iron-gears",
            "category": "crafting",
            "group": "test",
            "subgroup": "test",
            "energy": 0.5,
            "enabled": True,
            "order": "",
            "translated_name": "Iron Gears",
            "main_product": {"type": "item", "name": "iron-gear", "amount": 1},
            "ingredients": [
                {"type": "item", "name": "iron-plate", "amount": 2},
                {"type": "fluid", "name": "salt-water", "amount": 1},
            ],
            "products": [{"type": "item", "name": "iron-gear", "amount": 1, "probability": 1.0}],
        },
        "smelt-lead-test": {
            "name": "smelt-lead-test",
            "category": "smelting",
            "group": "test",
            "subgroup": "test",
            "energy": 1.0,
            "enabled": True,
            "order": "",
            "translated_name": "Smelt Lead (test)",
            "main_product": {"type": "item", "name": "lead-plate-test", "amount": 1},
            "ingredients": [{"type": "item", "name": "ore-lead-test", "amount": 1}],
            "products": [
                {"type": "item", "name": "lead-plate-test", "amount": 1, "probability": 1.0}
            ],
        },
        # Build recipes for machines
        "craft-stone-furnace": {
            "name": "craft-stone-furnace",
            "category": "crafting",
            "group": "test",
            "subgroup": "test",
            "energy": 0.5,
            "enabled": True,
            "order": "",
            "translated_name": "Craft Stone Furnace",
            "main_product": {"type": "item", "name": "stone-furnace", "amount": 1},
            "ingredients": [],
            "products": [
                {"type": "item", "name": "stone-furnace", "amount": 1, "probability": 1.0}
            ],
        },
        "craft-electric-furnace": {
            "name": "craft-electric-furnace",
            "category": "crafting",
            "group": "test",
            "subgroup": "test",
            "energy": 5.0,
            "enabled": False,
            "order": "",
            "translated_name": "Craft Electric Furnace",
            "main_product": {"type": "item", "name": "electric-furnace", "amount": 1},
            "ingredients": [],
            "products": [
                {"type": "item", "name": "electric-furnace", "amount": 1, "probability": 1.0}
            ],
        },
    },
    "technologies": {},
    "entities": {
        "stone-furnace": {
            "name": "stone-furnace",
            "type": "furnace",
            "group": "test",
            "subgroup": "test",
            "translated_name": "Stone Furnace",
            "crafting_speed": {"normal": 1.0},
            "crafting_categories": ["smelting"],
            "energy_consumption": 90000,
            "energy_source": "burner",
            "module_inventory_size": 0,
        },
        "electric-furnace": {
            "name": "electric-furnace",
            "type": "furnace",
            "group": "test",
            "subgroup": "test",
            "translated_name": "Electric Furnace",
            "crafting_speed": {"normal": 2.0},
            "crafting_categories": ["smelting"],
            "energy_consumption": 180000,
            "energy_source": "electric",
            "module_inventory_size": 2,
        },
        "assembling-machine-1": {
            "name": "assembling-machine-1",
            "type": "assembling-machine",
            "group": "test",
            "subgroup": "test",
            "translated_name": "Assembling Machine 1",
            "crafting_speed": {"normal": 0.5},
            "crafting_categories": ["crafting"],
            "energy_consumption": 75000,
            "energy_source": "electric",
            "module_inventory_size": 0,
        },
        "assembling-machine-2": {
            "name": "assembling-machine-2",
            "type": "assembling-machine",
            "group": "test",
            "subgroup": "test",
            "translated_name": "Assembling Machine 2",
            "crafting_speed": {"normal": 0.75},
            "crafting_categories": ["crafting"],
            "energy_consumption": 150000,
            "energy_source": "electric",
            "module_inventory_size": 2,
        },
        # Mining drill
        "basic-mining-drill": {
            "name": "basic-mining-drill",
            "type": "mining-drill",
            "translated_name": "Basic Mining Drill",
            "resource_categories": ["basic-solid"],
            "mining_speed": 0.5,
            "energy_consumption": 90000,
            "energy_source": "electric",
            "module_inventory_size": 0,
        },
        # Resource
        "iron-ore": {
            "name": "iron-ore",
            "type": "resource",
            "translated_name": "Iron Ore",
            "resource_category": "basic-solid",
            "mining_time": 1.0,
            "required_fluid": None,
            "fluid_amount": None,
            "product_name": "iron-ore",
        },
        # Fluid mining drill + fluid resource (raw fluid input, not item)
        "offshore-pump-test": {
            "name": "offshore-pump-test",
            "type": "mining-drill",
            "translated_name": "Offshore Pump (test)",
            "resource_categories": ["basic-fluid"],
            "mining_speed": 1.0,
            "energy_consumption": 0,
            "energy_source": "electric",
            "module_inventory_size": 0,
        },
        "salt-water": {
            "name": "salt-water",
            "type": "resource",
            "translated_name": "Salt Water",
            "resource_category": "basic-fluid",
            "mining_time": 1.0,
            "required_fluid": None,
            "fluid_amount": None,
            "product_name": "salt-water",
        },
        # Drill + resource requiring an INPUT fluid to mine (regression fixture
        # for the fluid_amount/10 fix -- Factorio's raw prototype field is
        # confirmed 10x the real per-mining-operation consumption; see
        # engine.py's comment on fluid_rate_per_min).
        "fluid-drill-test": {
            "name": "fluid-drill-test",
            "type": "mining-drill",
            "translated_name": "Fluid Drill (test)",
            "resource_categories": ["basic-with-fluid-test"],
            "mining_speed": 1.0,
            "energy_consumption": 90000,
            "energy_source": "electric",
            "module_inventory_size": 0,
        },
        "ore-lead-test": {
            "name": "ore-lead-test",
            "type": "resource",
            "translated_name": "Lead Ore (test)",
            "resource_category": "basic-with-fluid-test",
            "mining_time": 1.0,
            "required_fluid": "acetylene-test",
            "fluid_amount": 100.0,
            "product_name": "ore-lead-test",
        },
    },
    "groups": {},
}


@pytest.fixture
def test_db(tmp_path: Path) -> AsyncDatabase:
    json_path = tmp_path / "recipes.json"
    json_path.write_text(json.dumps(MACHINES_JSON))
    db_path = str(tmp_path / "recipes.db")
    build(str(json_path), db_path)
    return AsyncDatabase(db_path)


@pytest.fixture(autouse=True)
def patch_db(test_db: AsyncDatabase):
    with patch("planner.recipedb.engine.db", test_db):
        yield


# ---------------------------------------------------------------------------
# plan_product (recipe-mcp's plan_factory MCP tool was a thin pass-through)
# ---------------------------------------------------------------------------


async def test_plan_product_buildings_present() -> None:
    """plan_product returns buildings for a 2-step chain."""
    result = await engine.plan_product("iron-gear", rate_per_min=60.0)
    assert "buildings" in result
    # Should have smelting + crafting steps
    building_ids = {b["id"] for b in result["buildings"]}
    # At least one assembler (crafting) and one furnace (smelting)
    assert len(building_ids) >= 1


async def test_plan_product_raw_inputs() -> None:
    """raw_inputs includes iron-ore (the raw leaf of the chain)."""
    result = await engine.plan_product("iron-gear", rate_per_min=60.0)
    raw_ids = {r["id"] for r in result["raw_inputs"]}
    assert "iron-ore" in raw_ids


async def test_plan_product_fastest_machine_chosen() -> None:
    """For smelting category, electric-furnace (speed=2.0) is chosen over stone-furnace."""
    result = await engine.plan_product("iron-plate", rate_per_min=60.0)
    building_ids = {b["id"] for b in result["buildings"]}
    assert "electric-furnace" in building_ids
    assert "stone-furnace" not in building_ids


async def test_plan_product_available_machines_filter() -> None:
    """available_machines restricts machine selection."""
    result = await engine.plan_product(
        "iron-plate",
        rate_per_min=60.0,
        available_machines=["stone-furnace"],
    )
    building_ids = {b["id"] for b in result["buildings"]}
    assert "stone-furnace" in building_ids
    assert "electric-furnace" not in building_ids


async def test_plan_product_machine_count_math() -> None:
    """Machine count = ceil(batches_per_min × energy / (60 × speed)).

    For iron-plate at 60/min:
      recipe energy = 3.2s, output = 1/batch → batches = 60/min
      electric-furnace speed = 2.0
      count = ceil(60 × 3.2 / (60 × 2.0)) = ceil(1.6) = 2
    """
    result = await engine.plan_product("iron-plate", rate_per_min=60.0)
    ef = next(b for b in result["buildings"] if b["id"] == "electric-furnace")
    expected = math.ceil(60 * 3.2 / (60 * 2.0))
    assert ef["count"] == expected


async def test_plan_product_drill_counts() -> None:
    """Drill counts are computed for mineable raw inputs."""
    result = await engine.plan_product("iron-gear", rate_per_min=60.0)
    # iron-ore is mined → should have a drills entry
    # drills only populated when resources table has data (it does in fixture)
    drill_resources = {d["resource"] for d in result["drills"]}
    assert "iron-ore" in drill_resources


async def test_plan_product_fluid_drill_counts() -> None:
    """Drill counts are also computed for raw *fluid* inputs, not just items.

    iron-gears consumes salt-water (a fluid) as well as iron-plate; salt-water
    has a resources-table entry (offshore-pump-test drill, basic-fluid
    category). Regression test for a bug where the drill-lookup loop only
    iterated totals_items and silently dropped every fluid raw input.
    """
    result = await engine.plan_product("iron-gear", rate_per_min=60.0)
    drill_resources = {d["resource"]: d for d in result["drills"]}
    assert "salt-water" in drill_resources
    assert drill_resources["salt-water"]["drill_id"] == "offshore-pump-test"


async def test_plan_product_fluid_rate_divides_raw_fluid_amount_by_ten() -> None:
    """fluid_rate_per_min = amount_per_min * fluid_amount / 10, not amount_per_min
    * fluid_amount.

    Confirmed empirically in a real Pyanodons save (checked in-game across
    four different ores -- lead, zinc, chromium, titanium -- all matching
    exactly), and explained by Factorio dev Rseding91: "A 'mining operation'
    on a drill is 10 ores so it requires 0.1 fluid for 10 ores. On the ore
    itself it requires only 0.1 / 10 fluid per individual ore" -- the raw
    `MinableProperties.fluid_amount` prototype value is defined per 10-ore
    batch, not per single mining operation.
    https://forums.factorio.com/viewtopic.php?p=688574

    Before this fix, fluid_rate_per_min used the raw value directly,
    overstating required-fluid consumption by 10x for every fluid-requiring
    resource.
    """
    result = await engine.plan_product("lead-plate-test", rate_per_min=60.0)
    drill_resources = {d["resource"]: d for d in result["drills"]}
    assert "ore-lead-test" in drill_resources
    drill = drill_resources["ore-lead-test"]
    assert drill["required_fluid"] == "acetylene-test"
    # 60 ore-lead-test/min needed, fluid_amount=100 raw -> 100/10=10 per ore.
    assert drill["fluid_rate_per_min"] == pytest.approx(600.0)


async def test_plan_product_blocked_when_no_machine() -> None:
    """Recipes whose category has no machine produce a blocked entry."""
    # Use available_machines=[] to guarantee no machine is eligible
    result = await engine.plan_product(
        "iron-plate",
        rate_per_min=60.0,
        available_machines=[],
    )
    assert len(result["blocked_categories"]) > 0


async def test_plan_product_unknown_product() -> None:
    """Unknown product returns error."""
    result = await engine.plan_product("no-such-item-xyz")
    assert "error" in result
