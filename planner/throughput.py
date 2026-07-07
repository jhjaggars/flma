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

# Base/starter-first — used as the static fallback default tier when the
# caller doesn't specify one AND live tech-scoping isn't available (no
# recipes.db, or DB/save modpack mismatch — see cli.py's cmd_belts). The
# base tier is the safe assumption: a player who hasn't researched faster
# belts yet still gets a correct answer, whereas defaulting to the fastest
# tier silently overstates throughput for anyone earlier in the tech tree.
# When live tech-scoping *is* available, cmd_belts picks the fastest tier
# the current save can actually build instead of using this list at all.
DEFAULT_BELT_TIER_ORDER: list[str] = [
    "transport-belt",
    "fast-transport-belt",
    "express-transport-belt",
    "turbo-transport-belt",
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
    """How many belt lanes of `tier` (default: base/starter tier — see
    DEFAULT_BELT_TIER_ORDER) are needed to carry `items_per_sec`. Returns a
    dict rather than a bare number so callers can surface which
    tier/throughput was assumed."""
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


def rate_from_belts(n_belts: float, tier: str | None = None) -> dict[str, Any]:
    """Inverse of `belts_needed`: items/sec achievable from `n_belts` lanes of
    `tier` (default: base/starter tier — see DEFAULT_BELT_TIER_ORDER). Lets a
    caller turn "how many belts of ore do you want to supply?" into a
    `--rate` for `plan`/`options` instead of only going rate-to-belts. Same
    placeholder-accuracy caveat as `belts_needed` — see module docstring."""
    tier = tier or DEFAULT_BELT_TIER_ORDER[0]
    per_belt = BELT_THROUGHPUT_ITEMS_PER_SEC.get(tier)
    if per_belt is None:
        raise ValueError(
            f"unknown belt tier '{tier}'; known tiers: {sorted(BELT_THROUGHPUT_ITEMS_PER_SEC)}"
        )
    return {
        "tier": tier,
        "items_per_sec_per_belt": per_belt,
        "items_per_sec": n_belts * per_belt,
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


def capacity_needed(amount_per_sec: float, kind: str, tier: str | None = None) -> dict[str, Any]:
    """Dispatch to belts_needed (kind="item") or pipes_needed (kind="fluid")
    -- a raw_inputs entry's own "kind" field (see engine.py's plan_product)
    says which one actually applies. Fluids move through pipes (a single
    rough per-segment figure, no tiers) rather than belts, and pipe
    throughput (1200/sec) dwarfs even the fastest belt tier (60/sec) --
    treating a fluid's amount as if it needed belts invents a fake
    constraint for something that was never going belt, and can badly
    distort anything that compares raw inputs by "how many belts/pipes does
    this need" (e.g. `plan --cap`'s bottleneck auto-pick)."""
    if kind == "fluid":
        result = pipes_needed(amount_per_sec)
        return {"count": result["pipes"], "unit_plural": "pipes", "accurate": result["accurate"]}
    result = belts_needed(amount_per_sec, tier=tier)
    return {
        "count": result["belts"],
        "unit_plural": f"{result['tier']} belts",
        "accurate": result["accurate"],
    }
