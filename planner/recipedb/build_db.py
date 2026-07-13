"""Build the SQLite recipes database from recipes.json.

Vendored from recipe-mcp's `src/build_db.py` (see `planner/recipedb/__init__.py`
for provenance) — the schema authority `engine.py`'s queries target.

Run via `make build-db`, or directly:
    python -m planner.recipedb.build_db <input_json> <output_db>

Creates a normalized schema with these tables:
  names                     -- unified item+fluid name → translated_name lookup
  recipes                   -- one row per recipe
  recipe_ingredients        -- normalized ingredient rows
  recipe_products           -- normalized product rows (with probability/amount_min/max)
  technologies              -- one row per technology
  technology_prerequisites  -- tech prerequisite edges
  technology_recipe_unlocks -- tech → recipe unlock edges
  technology_ingredients    -- science pack costs per technology
  machines                  -- crafting machines (assemblers, furnaces, rocket silos)
  machine_crafting_categories -- machine → category many-to-many
  mining_drills             -- mining drill entities
  drill_resource_categories -- drill → resource category many-to-many
  resources                 -- mineable resource entities (ore patches)
  fuels                     -- items/fluids with a fuel_value (fuel_category, fuel_value)
  machine_fuel_categories   -- burner machine/drill → accepted fuel category many-to-many
  generators                -- fluid-driven electricity generators (steam-engine, steam-turbine, ...)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS names (
    name            TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,  -- 'item' or 'fluid'
    translated_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipes (
    name            TEXT PRIMARY KEY,
    translated_name TEXT NOT NULL,
    category        TEXT NOT NULL,
    group_name      TEXT NOT NULL DEFAULT '',
    subgroup        TEXT NOT NULL DEFAULT '',
    energy          REAL NOT NULL DEFAULT 0.0,
    enabled         INTEGER NOT NULL DEFAULT 1,
    order_string    TEXT NOT NULL DEFAULT '',
    main_product    TEXT         -- name of the main product item/fluid, or NULL
);

CREATE TABLE IF NOT EXISTS recipe_ingredients (
    recipe_name TEXT NOT NULL,
    position    INTEGER NOT NULL,
    item_name   TEXT NOT NULL,
    item_type   TEXT NOT NULL,  -- 'item' or 'fluid'
    amount      REAL NOT NULL,
    PRIMARY KEY (recipe_name, position)
);

CREATE TABLE IF NOT EXISTS recipe_products (
    recipe_name TEXT NOT NULL,
    position    INTEGER NOT NULL,
    item_name   TEXT NOT NULL,
    item_type   TEXT NOT NULL,  -- 'item' or 'fluid'
    amount      REAL,           -- NULL when using amount_min/max
    amount_min  REAL,
    amount_max  REAL,
    probability REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (recipe_name, position)
);

CREATE TABLE IF NOT EXISTS technologies (
    name                TEXT PRIMARY KEY,
    translated_name     TEXT NOT NULL,
    enabled             INTEGER NOT NULL DEFAULT 1,
    researched          INTEGER NOT NULL DEFAULT 0,
    unit_count          INTEGER,     -- NULL for infinite technologies
    unit_count_formula  TEXT,        -- set for infinite technologies
    unit_energy         REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS technology_prerequisites (
    tech_name   TEXT NOT NULL,
    prereq_name TEXT NOT NULL,
    PRIMARY KEY (tech_name, prereq_name)
);

CREATE TABLE IF NOT EXISTS technology_recipe_unlocks (
    tech_name   TEXT NOT NULL,
    recipe_name TEXT NOT NULL,
    PRIMARY KEY (tech_name, recipe_name)
);

CREATE TABLE IF NOT EXISTS technology_ingredients (
    tech_name   TEXT NOT NULL,
    position    INTEGER NOT NULL,
    item_name   TEXT NOT NULL,
    amount      REAL NOT NULL,
    PRIMARY KEY (tech_name, position)
);

CREATE TABLE IF NOT EXISTS machines (
    name                TEXT PRIMARY KEY,
    translated_name     TEXT NOT NULL,
    type                TEXT NOT NULL,
    crafting_speed      REAL NOT NULL DEFAULT 0.0,
    energy_consumption  REAL,
    energy_source       TEXT,
    burner_effectivity  REAL,
    module_slots        INTEGER NOT NULL DEFAULT 0,
    group_name          TEXT NOT NULL DEFAULT '',
    subgroup            TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS machine_crafting_categories (
    machine_name TEXT NOT NULL,
    category     TEXT NOT NULL,
    PRIMARY KEY (machine_name, category)
);

CREATE TABLE IF NOT EXISTS mining_drills (
    name                TEXT PRIMARY KEY,
    translated_name     TEXT NOT NULL,
    mining_speed        REAL NOT NULL DEFAULT 0.0,
    energy_consumption  REAL,
    energy_source       TEXT,
    burner_effectivity  REAL,
    module_slots        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS drill_resource_categories (
    drill_name        TEXT NOT NULL,
    resource_category TEXT NOT NULL,
    PRIMARY KEY (drill_name, resource_category)
);

CREATE TABLE IF NOT EXISTS resources (
    name              TEXT PRIMARY KEY,
    translated_name   TEXT NOT NULL,
    resource_category TEXT NOT NULL DEFAULT '',
    mining_time       REAL NOT NULL DEFAULT 1.0,
    required_fluid    TEXT,
    fluid_amount      REAL,
    product_name      TEXT
);

CREATE TABLE IF NOT EXISTS fuels (
    name              TEXT PRIMARY KEY,
    translated_name   TEXT NOT NULL,
    kind              TEXT NOT NULL,  -- 'item' or 'fluid'
    fuel_category     TEXT,
    fuel_value        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS machine_fuel_categories (
    machine_name  TEXT NOT NULL,  -- joins against machines.name or mining_drills.name
    fuel_category TEXT NOT NULL,
    PRIMARY KEY (machine_name, fuel_category)
);

CREATE TABLE IF NOT EXISTS generators (
    name                 TEXT PRIMARY KEY,
    translated_name      TEXT NOT NULL,
    max_power_output     REAL,
    fluid_usage_per_sec  REAL,
    input_fluid          TEXT,
    effectivity          REAL,
    maximum_temperature  REAL
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_ing_item        ON recipe_ingredients (item_name);
CREATE INDEX IF NOT EXISTS idx_prod_item       ON recipe_products (item_name);
CREATE INDEX IF NOT EXISTS idx_ing_recipe      ON recipe_ingredients (recipe_name);
CREATE INDEX IF NOT EXISTS idx_prod_recipe     ON recipe_products (recipe_name);
CREATE INDEX IF NOT EXISTS idx_recipe_cat      ON recipes (category);
CREATE INDEX IF NOT EXISTS idx_recipe_grp      ON recipes (group_name);
CREATE INDEX IF NOT EXISTS idx_recipe_name     ON recipes (translated_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_names_trans     ON names (translated_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_tech_name       ON technologies (translated_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_tech_unlock_rec ON technology_recipe_unlocks (recipe_name);
CREATE INDEX IF NOT EXISTS idx_tech_prereq     ON technology_prerequisites (prereq_name);
CREATE INDEX IF NOT EXISTS idx_tech_ing        ON technology_ingredients (tech_name);
CREATE INDEX IF NOT EXISTS idx_machine_cat     ON machine_crafting_categories (category);
CREATE INDEX IF NOT EXISTS idx_machine_trans   ON machines (translated_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_drill_rescat    ON drill_resource_categories (resource_category);
CREATE INDEX IF NOT EXISTS idx_resource_cat    ON resources (resource_category);
CREATE INDEX IF NOT EXISTS idx_resource_prod   ON resources (product_name);
CREATE INDEX IF NOT EXISTS idx_fuels_trans     ON fuels (translated_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_fuels_category  ON fuels (fuel_category);
CREATE INDEX IF NOT EXISTS idx_mfc_category    ON machine_fuel_categories (fuel_category);
CREATE INDEX IF NOT EXISTS idx_generators_trans ON generators (translated_name COLLATE NOCASE);
"""


def _load_names(conn: sqlite3.Connection, data: dict[str, Any]) -> int:
    """Populate the names table from items + fluids."""
    rows = []
    for name, item in data.get("items", {}).items():
        translated = item.get("translated_name") or name
        rows.append((name, "item", translated))
    for name, fluid in data.get("fluids", {}).items():
        translated = fluid.get("translated_name") or name
        rows.append((name, "fluid", translated))
    conn.executemany(
        "INSERT OR REPLACE INTO names (name, kind, translated_name) VALUES (?,?,?)",
        rows,
    )
    return len(rows)


def _load_recipes(conn: sqlite3.Connection, recipes: dict[str, Any]) -> tuple[int, int, int]:
    """Populate recipes, recipe_ingredients, recipe_products tables."""
    recipe_rows = []
    ingredient_rows = []
    product_rows = []

    for recipe in recipes.values():
        name = recipe.get("name", "")
        if not name:
            continue

        main_product_name = None
        mp = recipe.get("main_product")
        if mp and isinstance(mp, dict):
            main_product_name = mp.get("name")

        recipe_rows.append(
            (
                name,
                recipe.get("translated_name") or name,
                recipe.get("category") or "",
                recipe.get("group") or "",
                recipe.get("subgroup") or "",
                float(recipe.get("energy") or 0.0),
                1 if recipe.get("enabled", True) else 0,
                recipe.get("order") or "",
                main_product_name,
            )
        )

        for pos, ing in enumerate(recipe.get("ingredients", [])):
            iname = ing.get("name", "")
            if not iname:
                continue
            ingredient_rows.append(
                (
                    name,
                    pos,
                    iname,
                    ing.get("type", "item"),
                    float(ing.get("amount", 0)),
                )
            )

        for pos, prod in enumerate(recipe.get("products", [])):
            pname = prod.get("name", "")
            if not pname:
                continue
            # Products use either amount OR amount_min+amount_max
            amount = prod.get("amount")
            amount_min = prod.get("amount_min")
            amount_max = prod.get("amount_max")
            product_rows.append(
                (
                    name,
                    pos,
                    pname,
                    prod.get("type", "item"),
                    float(amount) if amount is not None else None,
                    float(amount_min) if amount_min is not None else None,
                    float(amount_max) if amount_max is not None else None,
                    float(prod.get("probability", 1.0)),
                )
            )

    conn.executemany(
        """INSERT OR REPLACE INTO recipes
           (name, translated_name, category, group_name, subgroup,
            energy, enabled, order_string, main_product)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        recipe_rows,
    )
    conn.executemany(
        """INSERT OR REPLACE INTO recipe_ingredients
           (recipe_name, position, item_name, item_type, amount)
           VALUES (?,?,?,?,?)""",
        ingredient_rows,
    )
    conn.executemany(
        """INSERT OR REPLACE INTO recipe_products
           (recipe_name, position, item_name, item_type,
            amount, amount_min, amount_max, probability)
           VALUES (?,?,?,?,?,?,?,?)""",
        product_rows,
    )
    return len(recipe_rows), len(ingredient_rows), len(product_rows)


def _load_technologies(
    conn: sqlite3.Connection, technologies: dict[str, Any]
) -> tuple[int, int, int, int]:
    """Populate technologies, technology_prerequisites, technology_recipe_unlocks, technology_ingredients."""
    tech_rows = []
    prereq_rows = []
    unlock_rows = []
    ing_rows = []

    for tech in technologies.values():
        name = tech.get("name", "")
        if not name:
            continue
        tech_rows.append(
            (
                name,
                tech.get("translated_name") or name,
                1 if tech.get("enabled", True) else 0,
                1 if tech.get("researched", False) else 0,
                tech.get("unit_count"),
                tech.get("unit_count_formula"),
                float(tech.get("unit_energy") or 0.0),
            )
        )
        for prereq in tech.get("prerequisites", []):
            prereq_rows.append((name, prereq))
        for recipe in tech.get("recipes_unlocked", []):
            unlock_rows.append((name, recipe))
        for pos, ing in enumerate(tech.get("unit_ingredients", [])):
            ing_rows.append((name, pos, ing["name"], float(ing["amount"])))

    conn.executemany(
        """INSERT OR REPLACE INTO technologies
           (name, translated_name, enabled, researched, unit_count, unit_count_formula, unit_energy)
           VALUES (?,?,?,?,?,?,?)""",
        tech_rows,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO technology_prerequisites (tech_name, prereq_name) VALUES (?,?)",
        prereq_rows,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO technology_recipe_unlocks (tech_name, recipe_name) VALUES (?,?)",
        unlock_rows,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO technology_ingredients (tech_name, position, item_name, amount) VALUES (?,?,?,?)",
        ing_rows,
    )
    return len(tech_rows), len(prereq_rows), len(unlock_rows), len(ing_rows)


def _load_machines(conn: sqlite3.Connection, entities: dict[str, Any]) -> tuple[int, int, int]:
    """Populate machines + machine_crafting_categories from entity data.

    A machine is any entity whose crafting_categories list is non-empty
    (assembling-machines, furnaces, rocket-silos).  Beacons and boilers have
    no crafting_categories and are silently skipped.
    """
    machine_rows = []
    category_rows = []
    fuel_cat_rows = []

    for entity in entities.values():
        categories = entity.get("crafting_categories") or []
        if not categories:
            continue
        name = entity.get("name", "")
        if not name:
            continue
        speed_raw = entity.get("crafting_speed") or {}
        speed = float(speed_raw.get("normal", 0.0)) if isinstance(speed_raw, dict) else 0.0
        machine_rows.append(
            (
                name,
                entity.get("translated_name") or name,
                entity.get("type") or "",
                speed,
                entity.get("energy_consumption"),
                entity.get("energy_source"),
                entity.get("burner_effectivity"),
                int(entity.get("module_inventory_size") or 0),
                entity.get("group") or "",
                entity.get("subgroup") or "",
            )
        )
        for cat in categories:
            category_rows.append((name, cat))
        for fuel_cat in entity.get("fuel_categories") or []:
            fuel_cat_rows.append((name, fuel_cat))

    conn.executemany(
        """INSERT OR REPLACE INTO machines
           (name, translated_name, type, crafting_speed, energy_consumption,
            energy_source, burner_effectivity, module_slots, group_name, subgroup)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        machine_rows,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO machine_crafting_categories (machine_name, category) VALUES (?,?)",
        category_rows,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO machine_fuel_categories (machine_name, fuel_category) VALUES (?,?)",
        fuel_cat_rows,
    )
    return len(machine_rows), len(category_rows), len(fuel_cat_rows)


def _load_extraction(
    conn: sqlite3.Connection, entities: dict[str, Any]
) -> tuple[int, int, int, int]:
    """Populate mining_drills + drill_resource_categories + resources.

    Requires the mod to export mining-drill and resource entity types (Phase 0
    mod edit). Tables are left empty if the JSON predates that change — all
    downstream tools are written to tolerate empty extraction tables.
    """
    drill_rows = []
    drill_cat_rows = []
    resource_rows = []
    fuel_cat_rows = []

    for entity in entities.values():
        etype = entity.get("type", "")
        name = entity.get("name", "")
        if not name:
            continue

        if etype == "mining-drill":
            res_cats = entity.get("resource_categories") or []
            drill_rows.append(
                (
                    name,
                    entity.get("translated_name") or name,
                    float(entity.get("mining_speed") or 0.0),
                    entity.get("energy_consumption"),
                    entity.get("energy_source"),
                    entity.get("burner_effectivity"),
                    int(entity.get("module_inventory_size") or 0),
                )
            )
            for rc in res_cats:
                drill_cat_rows.append((name, rc))
            for fuel_cat in entity.get("fuel_categories") or []:
                fuel_cat_rows.append((name, fuel_cat))

        elif etype == "resource":
            resource_rows.append(
                (
                    name,
                    entity.get("translated_name") or name,
                    entity.get("resource_category") or "",
                    float(entity.get("mining_time") or 1.0),
                    entity.get("required_fluid"),
                    entity.get("fluid_amount"),
                    entity.get("product_name"),
                )
            )

    conn.executemany(
        """INSERT OR REPLACE INTO mining_drills
           (name, translated_name, mining_speed, energy_consumption,
            energy_source, burner_effectivity, module_slots)
           VALUES (?,?,?,?,?,?,?)""",
        drill_rows,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO drill_resource_categories (drill_name, resource_category) VALUES (?,?)",
        drill_cat_rows,
    )
    conn.executemany(
        """INSERT OR REPLACE INTO resources
           (name, translated_name, resource_category, mining_time,
            required_fluid, fluid_amount, product_name)
           VALUES (?,?,?,?,?,?,?)""",
        resource_rows,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO machine_fuel_categories (machine_name, fuel_category) VALUES (?,?)",
        fuel_cat_rows,
    )
    return len(drill_rows), len(drill_cat_rows), len(resource_rows), len(fuel_cat_rows)


def _load_fuels(conn: sqlite3.Connection, data: dict[str, Any]) -> int:
    """Populate fuels from items + fluids with a positive fuel_value.

    Tables are left empty if the JSON predates fuel_value export — downstream
    tools are written to tolerate an empty fuels table.
    """
    rows = []
    for name, item in data.get("items", {}).items():
        fuel_value = item.get("fuel_value")
        if not fuel_value:
            continue
        rows.append(
            (
                name,
                item.get("translated_name") or name,
                "item",
                item.get("fuel_category"),
                float(fuel_value),
            )
        )
    for name, fluid in data.get("fluids", {}).items():
        fuel_value = fluid.get("fuel_value")
        if not fuel_value:
            continue
        rows.append(
            (
                name,
                fluid.get("translated_name") or name,
                "fluid",
                fluid.get("fuel_category"),
                float(fuel_value),
            )
        )
    conn.executemany(
        """INSERT OR REPLACE INTO fuels
           (name, translated_name, kind, fuel_category, fuel_value)
           VALUES (?,?,?,?,?)""",
        rows,
    )
    return len(rows)


def _load_generators(conn: sqlite3.Connection, entities: dict[str, Any]) -> int:
    """Populate generators from entities with type == 'generator'.

    Requires the mod to export the generator entity shape. Left empty if the
    JSON predates that change — downstream tools tolerate an empty table.
    Deliberately excludes electric-energy-interface entities (e.g. pyanodons'
    wind turbines) — flma's mod export doesn't cover those either, since their
    power output is live per-instance state, not static prototype data.
    """
    rows = []
    for entity in entities.values():
        if entity.get("type") != "generator":
            continue
        name = entity.get("name", "")
        if not name:
            continue
        rows.append(
            (
                name,
                entity.get("translated_name") or name,
                entity.get("max_power_output"),
                entity.get("fluid_usage_per_sec"),
                entity.get("input_fluid"),
                entity.get("effectivity"),
                entity.get("maximum_temperature"),
            )
        )
    conn.executemany(
        """INSERT OR REPLACE INTO generators
           (name, translated_name, max_power_output, fluid_usage_per_sec,
            input_fluid, effectivity, maximum_temperature)
           VALUES (?,?,?,?,?,?,?)""",
        rows,
    )
    return len(rows)


def build(json_path: str, db_path: str) -> None:
    """Load recipes.json into a new SQLite database at db_path."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    logger.info("Loading %s …", json_path)
    with open(json_path) as f:
        data = json.load(f)

    # Remove any existing DB so we get a clean build
    Path(db_path).unlink(missing_ok=True)

    logger.info("Creating schema at %s …", db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript(DDL)

    logger.info("Loading names (items + fluids) …")
    n_names = _load_names(conn, data)

    logger.info("Loading recipes …")
    n_recipes, n_ing, n_prod = _load_recipes(conn, data.get("recipes", {}))

    logger.info("Loading technologies …")
    n_techs, n_prereqs, n_unlocks, n_tech_ings = _load_technologies(
        conn, data.get("technologies", {})
    )

    entities = data.get("entities", {})
    logger.info("Loading machines …")
    n_machines, n_machine_cats, n_machine_fuel_cats = _load_machines(conn, entities)

    logger.info("Loading mining drills and resources …")
    n_drills, n_drill_cats, n_resources, n_drill_fuel_cats = _load_extraction(conn, entities)

    logger.info("Loading fuels …")
    n_fuels = _load_fuels(conn, data)

    logger.info("Loading generators …")
    n_generators = _load_generators(conn, entities)

    logger.info("Creating indexes …")
    conn.executescript(INDEXES)

    logger.info("Vacuuming and analyzing …")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.execute("VACUUM")
    conn.execute("ANALYZE")
    conn.close()

    logger.info(
        "Done. names=%d  recipes=%d  ingredients=%d  products=%d"
        "  technologies=%d  prereqs=%d  unlocks=%d  tech_ingredients=%d"
        "  machines=%d  machine_categories=%d  drills=%d  resources=%d"
        "  fuel_categories=%d  fuels=%d  generators=%d",
        n_names,
        n_recipes,
        n_ing,
        n_prod,
        n_techs,
        n_prereqs,
        n_unlocks,
        n_tech_ings,
        n_machines,
        n_machine_cats,
        n_drills,
        n_resources,
        n_machine_fuel_cats + n_drill_fuel_cats,
        n_fuels,
        n_generators,
    )


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(
            "Usage: python -m planner.recipedb.build_db <recipes.json> <recipes.db>",
            file=sys.stderr,
        )
        sys.exit(1)
    build(sys.argv[1], sys.argv[2])
