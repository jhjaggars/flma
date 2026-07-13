"""Unit tests for planner/recommend.py's rank_candidates — pure, no DB or
live game state (mirrors test_options.py/test_techbundle.py convention)."""

from __future__ import annotations

import pytest
from planner.recommend import rank_candidates

pytestmark = pytest.mark.unit


class TestRankCandidates:
    def test_researched_beats_unresearched_regardless_of_cost(self) -> None:
        cheap_but_locked = {
            "recipe_id": "locked",
            "researched": False,
            "raw_totals": {"ore": 10.0},
            "stages": 1,
        }
        expensive_but_usable = {
            "recipe_id": "usable",
            "researched": True,
            "raw_totals": {"ore": 999.0},
            "stages": 3,
        }
        ranked = rank_candidates([cheap_but_locked, expensive_but_usable])
        assert ranked[0]["recipe_id"] == "usable"

    def test_fewer_raw_item_types_wins_among_researched(self) -> None:
        one_raw = {
            "recipe_id": "simple",
            "researched": True,
            "raw_totals": {"ore": 500.0},
            "stages": 1,
        }
        many_raw = {
            "recipe_id": "complex",
            "researched": True,
            "raw_totals": {"tar": 50.0, "creosote": 20.0, "sand": 10.0},
            "stages": 5,
        }
        ranked = rank_candidates([many_raw, one_raw])
        assert ranked[0]["recipe_id"] == "simple"

    def test_lower_total_quantity_breaks_raw_type_tie(self) -> None:
        cheaper = {
            "recipe_id": "cheaper",
            "researched": True,
            "raw_totals": {"ore": 300.0},
            "stages": 2,
        }
        pricier = {
            "recipe_id": "pricier",
            "researched": True,
            "raw_totals": {"ore": 375.0},
            "stages": 2,
        }
        ranked = rank_candidates([pricier, cheaper])
        assert ranked[0]["recipe_id"] == "cheaper"

    def test_fewer_stages_breaks_remaining_tie(self) -> None:
        simple = {
            "recipe_id": "simple",
            "researched": True,
            "raw_totals": {"ore": 300.0},
            "stages": 1,
        }
        complicated = {
            "recipe_id": "complicated",
            "researched": True,
            "raw_totals": {"ore": 300.0},
            "stages": 4,
        }
        ranked = rank_candidates([complicated, simple])
        assert ranked[0]["recipe_id"] == "simple"

    def test_copper_style_combo_beats_plain_ungated_option(self) -> None:
        """Mirrors the real numbers: a bundle-adjusted combo (300 ore/min)
        beats the plain single-recipe option (480 ore/min), even though the
        plain option needs no research at all -- both are 'researched'
        (ungated == usable now) here, so cost decides."""
        combo = {
            "recipe_id": "copper-plate-4+grade-1-copper-crush+grade-2-copper",
            "researched": True,
            "raw_totals": {"copper-ore": 300.0},
            "stages": 2,
        }
        plain = {
            "recipe_id": "copper-plate",
            "researched": True,
            "raw_totals": {"copper-ore": 480.0},
            "stages": 1,
        }
        ranked = rank_candidates([plain, combo])
        assert ranked[0]["recipe_id"] == combo["recipe_id"]

    def test_stable_sort_preserves_order_on_full_tie(self) -> None:
        a = {"recipe_id": "a", "researched": True, "raw_totals": {"ore": 100.0}, "stages": 1}
        b = {"recipe_id": "b", "researched": True, "raw_totals": {"ore": 100.0}, "stages": 1}
        assert [c["recipe_id"] for c in rank_candidates([a, b])] == ["a", "b"]
