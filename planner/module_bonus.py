"""Assumed module acceleration for specific Pyanodons building families.

recipes.db's `machines.crafting_speed` column is the raw, no-module Factorio
value (recipe-mcp's build_db.py writes `entity["crafting_speed"]["normal"]`
straight through). Neither recipes.json nor recipes.db carries any data on
module *bonus magnitude*: no module items exist in the RecipeExporter dump,
and `machines.module_slots` (present but otherwise unused downstream) only
records slot counts, not what a filled slot is worth. For a couple of
specific Pyanodons building families this matters a lot in practice: Moondrop
greenhouses and Auog paddocks are conventionally always run fully
module-populated, and machine counts computed from the raw, no-module speed
(as recipe-mcp's own `plan_product` does) overstate requirements by 5-17x —
546 greenhouses for a modest battery line, when a module-filled line needs a
small fraction of that.

Confirmed by the player, not derivable from any exported file: the module
effect for these two families is +100% crafting speed per module slot,
additive. This module is the single place that assumption lives — same
"seeded placeholder, not real game data" pattern as planner/throughput.py's
belt/pipe constants.
"""

from __future__ import annotations

# Machine-id prefixes this assumption applies to. Deliberately narrow rather
# than every machine with module_slots > 0 — a broader "assume modules
# everywhere" mode would need per-family bonus figures we don't have yet.
MODULE_ACCELERATED_PREFIXES: tuple[str, ...] = (
    "moondrop-greenhouse-",
    "auog-paddock-",
)

MODULE_SPEED_BONUS_PER_SLOT: float = 1.0  # +100%/slot, additive


def is_module_accelerated(machine_id: str) -> bool:
    return machine_id.startswith(MODULE_ACCELERATED_PREFIXES)


def effective_speed(machine_id: str, crafting_speed: float, module_slots: int) -> float:
    """crafting_speed, boosted assuming every module slot is filled with a
    speed module — only for the families in MODULE_ACCELERATED_PREFIXES;
    unchanged otherwise (including machines that have module_slots but fall
    outside those families, since we don't have their bonus figures)."""
    if module_slots > 0 and is_module_accelerated(machine_id):
        return crafting_speed * (1.0 + module_slots * MODULE_SPEED_BONUS_PER_SLOT)
    return crafting_speed
