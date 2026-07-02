"""Live-state helpers for the factory planner: net production, buffered
logistics stock, currently-researched technologies, and modpack-alignment
detection — all derived from flma's `GameState` (src/game_state.py), read
directly off disk. No MCP server or Hermes involved.

None of this net-production math exists elsewhere in the codebase —
src/server.py's `get_production_stats` only passes the raw per-surface
`input_counts`/`output_counts` straight through (see the flma exploration
notes in the factory-planner plan); this module is what actually subtracts
them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.game_state import GameState


def open_game_state(script_output_dir: Path) -> GameState:
    """Construct a GameState and force an immediate read (a one-shot CLI
    process has no warm cache to rely on)."""
    gs = GameState(script_output_dir)
    gs.refresh(force=True)
    return gs


def researched_technologies(gs: GameState, force: str = "player") -> list[str]:
    """Tech ids currently researched by `force`, per the live tech.json."""
    tech = gs.get_tech()
    force_data = tech.get("forces", {}).get(force, {})
    technologies = force_data.get("technologies", {})
    return [name for name, t in technologies.items() if t.get("researched")]


def net_production(gs: GameState, force: str = "player") -> dict[str, float]:
    """Net production rate (input - output) per item/fluid id, in units per
    minute, summed across all of `force`'s surfaces. Positive = net surplus
    being produced right now; negative = net consumption (importing/
    depleting). Items and fluids share one namespace by id, which is safe —
    the two never collide in Factorio's data model.

    In Factorio's LuaFlowStatistics, `input_counts` is what the force
    *produced* and `output_counts` is what it *consumed* (matches the
    left/right split in the in-game production statistics GUI) — so net is
    input minus output, not the other way around.

    Uses the mod's `input_rates_per_min`/`output_rates_per_min` fields (real
    per-minute flow rates over roughly the last 60s), not the cumulative
    `input_counts`/`output_counts` totals — those are lifetime-since-game-start
    sums, not rates, and would make this function's "/min" units a lie. If a
    surface's snapshot predates those fields (older mod build), its
    contribution is skipped entirely rather than silently falling back to the
    cumulative totals — returning {} for `force` in that case — since mixing
    lifetime totals into a "net /min" figure would be a wrong number, not a
    degraded one.
    """
    production = gs.get_production()
    net: dict[str, float] = {}
    force_data = production.get("forces", {}).get(force, {})
    for surface_data in force_data.get("surfaces", {}).values():
        for kind in ("items", "fluids"):
            kind_data = surface_data.get(kind) or {}
            if "input_rates_per_min" not in kind_data or "output_rates_per_min" not in kind_data:
                continue
            for name, rate in (kind_data.get("input_rates_per_min") or {}).items():
                net[name] = net.get(name, 0.0) + rate
            for name, rate in (kind_data.get("output_rates_per_min") or {}).items():
                net[name] = net.get(name, 0.0) - rate
    return net


def buffered_stock(gs: GameState, force: str = "player") -> dict[str, int]:
    """Total buffered item/fluid counts across all of `force`'s logistic
    networks (all surfaces combined)."""
    logistics = gs.get_logistics()
    totals: dict[str, int] = {}
    for network in logistics.get("forces", {}).get(force, []):
        for entry in network.get("contents", []):
            name = entry.get("name")
            if name is None:
                continue
            totals[name] = totals.get(name, 0) + entry.get("count", 0)
    return totals


def modpack_alignment(
    gs: GameState, db_tech_ids: set[str], force: str = "player"
) -> dict[str, Any]:
    """Compare the live save's tech ids against the recipe DB's tech ids to
    detect whether they describe the same modpack.

    Live tech-scoping and live-production netting are only meaningful when
    they do — item/tech ids match by internal name *within* a modpack, not
    across modpacks (see CLAUDE.md's factory-planner section: the committed
    recipes.json is Pyanodons, this machine's live save may be Space Age or
    anything else). `aligned=False` doesn't mean anything is broken; it means
    "don't trust the live-netting/live-scoping annotations below, they will
    all correctly say 'no match'".

    Uses Jaccard similarity (|overlap| / |union|), not overlap-over-live-count.
    Different modpacks built on the same Factorio base share a large vanilla
    tech core, so overlap/live_count alone reads misleadingly high (verified
    empirically: Space Age live vs. the committed Pyanodons dump shares 199 of
    275 live techs — 72% recall — purely from shared vanilla ids, even though
    the packs are clearly different: 0 of the live techs are `py`-prefixed,
    and the dump has 700+ techs the live save never defines at all). Jaccard
    penalizes exactly that "each side also has a large pack-specific set"
    case, since the union includes the db's un-matched techs too.
    """
    tech = gs.get_tech()
    force_data = tech.get("forces", {}).get(force, {})
    live_tech_ids = set(force_data.get("technologies", {}).keys())
    overlap = live_tech_ids & db_tech_ids
    union = live_tech_ids | db_tech_ids
    jaccard = (len(overlap) / len(union)) if union else 0.0
    return {
        "aligned": jaccard > 0.5,  # same modpack should be near-total overlap, not a coin flip
        "live_tech_count": len(live_tech_ids),
        "db_tech_count": len(db_tech_ids),
        "overlap_count": len(overlap),
        "overlap_ratio": jaccard,
    }
