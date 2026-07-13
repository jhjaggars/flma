"""Unit tests for planner/recipedb/build_db.py (vendored from recipe-mcp)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from planner.recipedb.build_db import build

pytestmark = pytest.mark.unit


MINIMAL_RECIPES_JSON = {
    "game_version": "2.0.0",
    "items": {
        "wood": {"name": "wood", "type": "item", "translated_name": "Wood"},
        "wooden-chest": {"name": "wooden-chest", "type": "item", "translated_name": "Wooden chest"},
        "ash": {"name": "ash", "type": "item", "translated_name": "Ash"},
    },
    "fluids": {
        "water": {"name": "water", "translated_name": "Water"},
    },
    "recipes": {
        "wooden-chest": {
            "name": "wooden-chest",
            "category": "crafting",
            "group": "logistics",
            "subgroup": "storage",
            "energy": 0.5,
            "enabled": True,
            "order": "a[items]-a[wooden-chest]",
            "translated_name": "Wooden chest",
            "main_product": {"type": "item", "name": "wooden-chest", "amount": 1},
            "ingredients": [
                {"type": "item", "name": "wood", "amount": 2},
            ],
            "products": [
                {"type": "item", "name": "wooden-chest", "amount": 1, "probability": 1.0},
            ],
        },
        "wood-incineration": {
            "name": "wood-incineration",
            "category": "py-incineration",
            "group": "py-industry",
            "subgroup": "waste",
            "energy": 1.0,
            "enabled": True,
            "order": "",
            "translated_name": "Wood (incineration)",
            "main_product": None,
            "ingredients": [
                {"type": "item", "name": "wood", "amount": 1},
            ],
            "products": [
                {
                    "type": "item",
                    "name": "ash",
                    "probability": 1.0,
                    "amount_min": 2,
                    "amount_max": 5,
                },
            ],
        },
    },
    "groups": {},
    "entities": {},
}


@pytest.fixture
def recipes_json(tmp_path: Path) -> str:
    path = tmp_path / "recipes.json"
    path.write_text(json.dumps(MINIMAL_RECIPES_JSON))
    return str(path)


@pytest.fixture
def built_db(recipes_json: str, tmp_path: Path) -> str:
    db_path = str(tmp_path / "recipes.db")
    build(recipes_json, db_path)
    return db_path


def test_build_creates_db(built_db: str) -> None:
    assert Path(built_db).exists()


def test_recipe_count(built_db: str) -> None:
    conn = sqlite3.connect(built_db)
    count = conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
    conn.close()
    assert count == 2


def test_names_count(built_db: str) -> None:
    conn = sqlite3.connect(built_db)
    count = conn.execute("SELECT COUNT(*) FROM names").fetchone()[0]
    conn.close()
    # 3 items + 1 fluid
    assert count == 4


def test_ingredient_row(built_db: str) -> None:
    conn = sqlite3.connect(built_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM recipe_ingredients WHERE recipe_name = 'wooden-chest'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["item_name"] == "wood"
    assert row["amount"] == 2.0


def test_product_with_amount(built_db: str) -> None:
    conn = sqlite3.connect(built_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM recipe_products WHERE recipe_name = 'wooden-chest'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["item_name"] == "wooden-chest"
    assert row["amount"] == 1.0
    assert row["amount_min"] is None


def test_product_with_amount_min_max(built_db: str) -> None:
    conn = sqlite3.connect(built_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM recipe_products WHERE recipe_name = 'wood-incineration'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["amount"] is None
    assert row["amount_min"] == 2.0
    assert row["amount_max"] == 5.0


def test_main_product(built_db: str) -> None:
    conn = sqlite3.connect(built_db)
    row = conn.execute(
        "SELECT main_product FROM recipes WHERE name = 'wooden-chest'"
    ).fetchone()
    conn.close()
    assert row[0] == "wooden-chest"


def test_null_main_product(built_db: str) -> None:
    conn = sqlite3.connect(built_db)
    row = conn.execute(
        "SELECT main_product FROM recipes WHERE name = 'wood-incineration'"
    ).fetchone()
    conn.close()
    assert row[0] is None


def test_translated_name_fallback(built_db: str) -> None:
    """Items without a translated_name should fall back to raw name."""
    # All items in our fixture have translated_name, but check the names table directly
    conn = sqlite3.connect(built_db)
    row = conn.execute("SELECT translated_name FROM names WHERE name = 'wood'").fetchone()
    conn.close()
    assert row[0] == "Wood"


def test_old_json_leaves_power_tables_empty_not_erroring(built_db: str) -> None:
    """MINIMAL_RECIPES_JSON predates fuel_value/generator export (empty
    entities, no fuel_value on items) -- build() must tolerate that rather
    than raising, leaving the new tables present but empty."""
    conn = sqlite3.connect(built_db)
    for table in ("fuels", "generators", "machine_fuel_categories"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert count == 0, f"{table} should be empty for pre-fuel-export JSON"
    conn.close()


POWER_RECIPES_JSON = {
    "game_version": "2.0.0",
    "items": {
        "coal": {
            "name": "coal",
            "type": "item",
            "translated_name": "Coal",
            "fuel_category": "chemical",
            "fuel_value": 8000000,
        },
        "wood": {"name": "wood", "type": "item", "translated_name": "Wood"},
    },
    "fluids": {
        "pressured-steam": {"name": "pressured-steam", "translated_name": "Pressurized steam"},
    },
    "recipes": {},
    "groups": {},
    "entities": {
        "py-coal-powerplant-mk01": {
            "name": "py-coal-powerplant-mk01",
            "type": "assembling-machine",
            "translated_name": "Coal powerplant MK 01",
            "group": "production",
            "subgroup": "energy",
            "crafting_categories": ["coal-powerplant"],
            "crafting_speed": {"normal": 1.0},
            "energy_consumption": 10000000,
            "energy_source": "burner",
            "fuel_categories": ["chemical"],
            "burner_effectivity": 1.0,
        },
        "steam-turbine-mk01": {
            "name": "steam-turbine-mk01",
            "type": "generator",
            "translated_name": "Steam turbine MK01",
            "group": "production",
            "subgroup": "energy",
            "max_power_output": 7880000,
            "fluid_usage_per_sec": 60,
            "effectivity": 1,
            "maximum_temperature": 1000,
            "input_fluid": "pressured-steam",
        },
        "electric-mining-drill": {
            "name": "electric-mining-drill",
            "type": "mining-drill",
            "translated_name": "Electric mining drill",
            "resource_categories": ["basic-solid"],
            "mining_speed": 0.5,
            "energy_consumption": 90000,
            "energy_source": "electric",
        },
        "burner-mining-drill": {
            "name": "burner-mining-drill",
            "type": "mining-drill",
            "translated_name": "Burner mining drill",
            "resource_categories": ["basic-solid"],
            "mining_speed": 0.25,
            "energy_consumption": 150000,
            "energy_source": "burner",
            "fuel_categories": ["chemical"],
            "burner_effectivity": 1.0,
        },
        "multiblade-turbine-mk01": {
            "name": "multiblade-turbine-mk01",
            "type": "electric-energy-interface",
            "translated_name": "Multiblade turbine MK01",
        },
    },
}


@pytest.fixture
def power_recipes_json(tmp_path: Path) -> str:
    path = tmp_path / "power_recipes.json"
    path.write_text(json.dumps(POWER_RECIPES_JSON))
    return str(path)


@pytest.fixture
def built_power_db(power_recipes_json: str, tmp_path: Path) -> str:
    db_path = str(tmp_path / "power_recipes.db")
    build(power_recipes_json, db_path)
    return db_path


def test_fuels_loaded_from_items(built_power_db: str) -> None:
    conn = sqlite3.connect(built_power_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM fuels WHERE name = 'coal'").fetchone()
    conn.close()
    assert row is not None
    assert row["kind"] == "item"
    assert row["fuel_category"] == "chemical"
    assert row["fuel_value"] == 8000000.0


def test_items_without_fuel_value_are_excluded_from_fuels(built_power_db: str) -> None:
    conn = sqlite3.connect(built_power_db)
    row = conn.execute("SELECT * FROM fuels WHERE name = 'wood'").fetchone()
    conn.close()
    assert row is None


def test_generator_loaded(built_power_db: str) -> None:
    conn = sqlite3.connect(built_power_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM generators WHERE name = 'steam-turbine-mk01'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["max_power_output"] == 7880000.0
    assert row["fluid_usage_per_sec"] == 60.0
    assert row["input_fluid"] == "pressured-steam"
    assert row["effectivity"] == 1.0
    assert row["maximum_temperature"] == 1000.0


def test_electric_energy_interface_excluded_from_generators(built_power_db: str) -> None:
    """Wind-turbine-style scripted power sources have no static prototype
    power figure -- must not be picked up by the generator loader."""
    conn = sqlite3.connect(built_power_db)
    row = conn.execute(
        "SELECT * FROM generators WHERE name = 'multiblade-turbine-mk01'"
    ).fetchone()
    conn.close()
    assert row is None


def test_burner_machine_effectivity_and_fuel_category(built_power_db: str) -> None:
    conn = sqlite3.connect(built_power_db)
    conn.row_factory = sqlite3.Row
    machine = conn.execute(
        "SELECT * FROM machines WHERE name = 'py-coal-powerplant-mk01'"
    ).fetchone()
    fuel_cat = conn.execute(
        "SELECT * FROM machine_fuel_categories WHERE machine_name = 'py-coal-powerplant-mk01'"
    ).fetchone()
    conn.close()
    assert machine is not None
    assert machine["burner_effectivity"] == 1.0
    assert machine["energy_source"] == "burner"
    assert fuel_cat is not None
    assert fuel_cat["fuel_category"] == "chemical"


def test_electric_machine_has_no_burner_effectivity(built_power_db: str) -> None:
    conn = sqlite3.connect(built_power_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM mining_drills WHERE name = 'electric-mining-drill'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["burner_effectivity"] is None
    assert row["energy_source"] == "electric"


def test_burner_drill_effectivity_and_fuel_category(built_power_db: str) -> None:
    conn = sqlite3.connect(built_power_db)
    conn.row_factory = sqlite3.Row
    drill = conn.execute(
        "SELECT * FROM mining_drills WHERE name = 'burner-mining-drill'"
    ).fetchone()
    fuel_cat = conn.execute(
        "SELECT * FROM machine_fuel_categories WHERE machine_name = 'burner-mining-drill'"
    ).fetchone()
    conn.close()
    assert drill is not None
    assert drill["burner_effectivity"] == 1.0
    assert fuel_cat is not None
    assert fuel_cat["fuel_category"] == "chemical"


def test_fuel_and_burn_rate_join(built_power_db: str) -> None:
    """The join a `planner power` command performs: machine's fuel_categories
    -> matching fuels -> burn_rate = energy_consumption / (fuel_value * effectivity)."""
    conn = sqlite3.connect(built_power_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT m.energy_consumption, m.burner_effectivity, f.fuel_value
           FROM machines m
           JOIN machine_fuel_categories mfc ON mfc.machine_name = m.name
           JOIN fuels f ON f.fuel_category = mfc.fuel_category
           WHERE m.name = 'py-coal-powerplant-mk01' AND f.name = 'coal'"""
    ).fetchone()
    conn.close()
    assert row is not None
    burn_per_sec = row["energy_consumption"] / (row["fuel_value"] * row["burner_effectivity"])
    assert burn_per_sec == pytest.approx(1.25)
