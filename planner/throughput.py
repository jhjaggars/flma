"""Belt/pipe throughput constants.

Neither recipes.json nor the SQLite DB built from it carries belt speed,
pipe throughput, or any items-per-second capacity figure — confirmed by
scanning the raw dump (see the factory-planner plan's DB-schema research:
zero hits for "throughput"/"belt_speed"/"items_per_second", and no belt/pipe
entity types are exported at all). Belt/pipe math has to use hardcoded
constants, so this module is deliberately the *only* place they live.

**These values are seeded for base game / Space Age tiers.** The committed
recipes.json (and therefore recipes.db, by the project's current decision)
is a **Pyanodons** dump. Pyanodons uses different belt tiers and its
`py-transport-belt-capacity-N` research techs multiply belt throughput —
neither is reflected here yet. Fill in real Pyanodons figures before
trusting belt counts for a Pyanodons game; until then, `plan`'s belt output
should be treated as a rough placeholder, not a real bill of materials.
"""

from __future__ import annotations

from typing import Any

# id -> items/sec per belt lane, base/Space Age tiers.
BELT_THROUGHPUT_ITEMS_PER_SEC: dict[str, float] = {
    "transport-belt": 15.0,
    "fast-transport-belt": 30.0,
    "express-transport-belt": 45.0,
    "turbo-transport-belt": 60.0,  # Space Age
}

# Fastest-first — used to pick a default tier when the caller doesn't specify
# one. Also documents belt-tier ids in priority order for `belts_needed`.
DEFAULT_BELT_TIER_ORDER: list[str] = [
    "turbo-transport-belt",
    "express-transport-belt",
    "fast-transport-belt",
    "transport-belt",
]

# Fluids/sec per pipe segment — Factorio's pipe throughput is distance- and
# topology-dependent (drops off over long runs, pumps restore it), so this is
# a single rough per-segment figure, not a tiered table like belts. Treat any
# belt/pipe count from this module as an order-of-magnitude estimate.
PIPE_THROUGHPUT_FLUID_PER_SEC: float = 1200.0

# Set to True once real Pyanodons belt-tier and pipe figures have been filled
# in above. Surfaced by `plan`/`status` so the CLI's belt-count section is
# clearly labeled instead of silently implying base-game accuracy.
VALUES_ARE_PYANODONS_ACCURATE: bool = False


def belts_needed(items_per_sec: float, tier: str | None = None) -> dict[str, Any]:
    """How many belt lanes of `tier` (default: fastest known tier) are needed
    to carry `items_per_sec`. Returns a dict rather than a bare number so
    callers can surface which tier/throughput was assumed."""
    tier = tier or DEFAULT_BELT_TIER_ORDER[0]
    per_belt = BELT_THROUGHPUT_ITEMS_PER_SEC.get(tier)
    if per_belt is None:
        raise ValueError(
            f"unknown belt tier '{tier}'; known tiers: {sorted(BELT_THROUGHPUT_ITEMS_PER_SEC)}"
        )
    return {
        "tier": tier,
        "items_per_sec_per_belt": per_belt,
        "belts": items_per_sec / per_belt if per_belt else 0.0,
        "accurate": VALUES_ARE_PYANODONS_ACCURATE,
    }


def pipes_needed(fluid_per_sec: float) -> dict[str, Any]:
    """Rough pipe-segment count for a fluid flow. See module docstring — this
    is a single coarse figure, not a real pipe/pump network model."""
    return {
        "fluid_per_sec_per_pipe": PIPE_THROUGHPUT_FLUID_PER_SEC,
        "pipes": fluid_per_sec / PIPE_THROUGHPUT_FLUID_PER_SEC,
        "accurate": VALUES_ARE_PYANODONS_ACCURATE,
    }
