"""Ranking logic for `planner recommend` — the single-best-plan synthesis
over `options` (distinct viable recipes for an item) and `techbundle` (does
a candidate's tech-unlock bundle combine it with a sibling recipe for zero
waste?).

`cli.cmd_recommend` does all the DB work and bundle lookups; this module
only ranks the resulting plain summaries. The ranking is a deliberately
narrow, documented heuristic — NOT a general multi-attribute optimizer
across incomparable raw-material profiles. Different candidate recipes for
the same item can need entirely different raw materials (e.g. a plain
ore-smelt vs. a multi-stage tar/creosote/sand chain), and there's no single
correct way to compare "300 copper-ore/min" against "50 tar/min + 20
creosote/min" in general — that trap was explicitly avoided when the
tech-bundle solver was scoped down from a whole-graph LP. This heuristic
only gives a good answer in the common case: alternate recipes that mostly
overlap in what raw materials they need (which is the usual case for "ways
to make the same top-level item").
"""

from __future__ import annotations

# A candidate summary: {"recipe_id": str, "researched": bool,
# "raw_totals": dict[item_id, float], "stages": int}


def rank_candidates(candidates: list[dict]) -> list[dict]:
    """Sort candidate summaries best-first:
    1. usable right now beats needing research not yet done,
    2. fewer distinct raw item types beats more (a rough proxy for "simpler
       supply chain" when profiles overlap),
    3. lower total raw quantity beats higher (only meaningful when comparing
       overlapping raw-material sets — see module docstring),
    4. fewer recipe stages breaks any remaining tie.
    Stable sort — candidates already tied on all four keys keep their
    original relative order."""

    def sort_key(c: dict) -> tuple[int, int, float, int]:
        return (
            0 if c["researched"] else 1,
            len(c["raw_totals"]),
            sum(c["raw_totals"].values()),
            c["stages"],
        )

    return sorted(candidates, key=sort_key)
