"""Golden-item regression tests for `_pick_producer` (planner/recipedb/engine.py,
vendored from recipe-mcp) against a real, live-exported `recipes.json` — not
the synthetic per-test fixtures used in tests/unit/. These pin down "does the
recipe-picker still choose a sensible recipe for known real items",
independent of any live save's research state (`enforce_tech=False` — every
recipe is a valid candidate, same as a bare engine call with no tech info
sees), so a future change to the selection heuristic (or a `recipes.json`
refresh from a new mod version) that regresses one of these gets caught here
instead of only being noticed by hand, the way the original sand/grade-2-zinc
bug was (recipe-mcp's own history, see GOLDEN below).

Building a real DB (tens of thousands of recipes for a modded save) is slower
than the synthetic fixtures in tests/unit/, so this lives in its own
tests/eval/ directory, excluded from `make test`/`make quick`
(noxfile.py's `tests` session only runs `tests/unit/`) — run directly via
`uv run pytest tests/eval/` or `make eval`.

The GOLDEN table below is recipe-mcp's own Pyanodons-specific regression
set — it only applies when RECIPES_JSON points at (or the live save resolves
to) that same modpack; otherwise every case is skipped rather than failed,
since a different modpack simply may not have these item/recipe ids at all.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from planner.recipedb import engine
from planner.recipedb.build_db import build
from planner.recipedb.db import AsyncDatabase

pytestmark = pytest.mark.eval


def _resolve_recipes_json() -> Path | None:
    """Find a recipes.json to build the golden DB from: RECIPES_JSON env var
    override first (e.g. recipe-mcp's own committed Pyanodons dump, to run
    this exact suite unmodified), else the live save's own export (if a
    Factorio client/server has run with the flma mod enabled), else None —
    callers should skip rather than fail when nothing is available."""
    override = os.environ.get("RECIPES_JSON")
    if override:
        path = Path(override)
        return path if path.exists() else None

    try:
        from planner.live_state import open_game_state
        from src.config import SCRIPT_OUTPUT_DIR
    except ImportError:
        return None

    try:
        gs = open_game_state(SCRIPT_OUTPUT_DIR)
    except OSError:
        return None
    return gs.recipes_path if gs.recipes_path.exists() else None


REAL_RECIPES_JSON = _resolve_recipes_json()


@pytest.fixture(scope="session")
def real_db(tmp_path_factory) -> AsyncDatabase:
    if REAL_RECIPES_JSON is None:
        pytest.skip(
            "no recipes.json available -- set RECIPES_JSON to a real export "
            "(e.g. recipe-mcp's committed dump) or run against a live save"
        )
    db_path = str(tmp_path_factory.mktemp("eval") / "recipes.db")
    build(str(REAL_RECIPES_JSON), db_path)
    return AsyncDatabase(db_path)


@pytest.fixture(autouse=True)
def patch_db(real_db: AsyncDatabase):
    with patch("planner.recipedb.engine.db", real_db):
        yield


async def _pick(item_id: str):
    chosen, _rows, notes, reason = await engine._pick_producer(
        item_id,
        exclude_cats=engine._DEFAULT_EXCLUDE,
        stop_cats=frozenset(),
        prefer_enabled=True,
        overrides={},
        extra_unlocked=frozenset(),
        enforce_tech=False,
    )
    return chosen, notes, reason


# item -> expected recipe id. No tech-scoping (enforce_tech=False) so these
# are independent of any particular save's research progress — every
# producer of the item is a candidate, same as a bare engine call sees.
# Pyanodons-specific (recipe-mcp's original regression set) — skipped
# individually if the built DB doesn't have the item at all.
GOLDEN = {
    # The actual incident this eval exists to catch: sand used to resolve to
    # grade-2-zinc, a zinc-refining recipe where sand is only a
    # 50%-probability secondary byproduct — the main_product tier now
    # prefers extract-sand, whose main_product is genuinely sand.
    "sand": "extract-sand",
    "limestone": "extract-limestone-01",
    "chromite-sand": "richdust-separation",
    "grade-1-chromite": "grade-1-chromite",  # direct name match, unaffected by the tier change
    "chromium": "chromium-01",
    "creosote": "creosote",  # direct name match
}


@pytest.mark.parametrize("item_id,expected_recipe", sorted(GOLDEN.items()))
async def test_golden_pick(item_id: str, expected_recipe: str) -> None:
    chosen, notes, reason = await _pick(item_id)
    if chosen is None and reason == "no_recipe":
        pytest.skip(f"{item_id}: not present in this recipes.db (different modpack?)")
    assert chosen is not None, f"{item_id}: expected '{expected_recipe}', got no pick ({reason})"
    assert chosen["name"] == expected_recipe, (
        f"{item_id}: expected '{expected_recipe}', got '{chosen['name']}'. Selection notes: {notes}"
    )


async def test_sand_never_regresses_to_the_original_byproduct_bug() -> None:
    """The specific incident this whole eval exists for: main_product tier
    must keep grade-2-zinc (a 50%-probability byproduct of zinc refining)
    from winning over a recipe whose actual purpose is making sand."""
    chosen, _notes, reason = await _pick("sand")
    if chosen is None and reason == "no_recipe":
        pytest.skip("sand: not present in this recipes.db (different modpack?)")
    assert chosen is not None
    assert chosen["name"] != "grade-2-zinc"


async def test_golden_picks_have_matching_main_product() -> None:
    """Every golden pick should have the item as its actual main_product,
    not just win by falling through to the alphabetical tie-break — this is
    the property the fix is supposed to guarantee for any item that has at
    least one main-product candidate."""
    for item_id in GOLDEN:
        chosen, notes, reason = await _pick(item_id)
        if chosen is None and reason == "no_recipe":
            continue  # different modpack, item doesn't exist -- nothing to check
        assert chosen is not None, f"{item_id}: no pick ({reason})"
        assert chosen["main_product"] == item_id, (
            f"{item_id}: chosen recipe '{chosen['name']}' has main_product "
            f"'{chosen['main_product']}', not '{item_id}'. Notes: {notes}"
        )
