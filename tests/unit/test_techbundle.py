"""Unit tests for planner/techbundle.py — co-product recycling bundle
detection and the linear solver behind `planner tech`. All pure functions
operating on plain dicts/sets, no recipes.db or live game state needed
(mirrors test_options.py's convention for DB-free planner logic).

The copper fixture mirrors the real Pyanodons "Copper processing - Stage 1"
bundle verified by hand during design:
    screen: 5x ore -> 1x g1 + 2x g2
    crush:  2x g1  -> 1x g2 + 2x stone
    smelt:  5x g2  -> 2x plate
Hand-solved for target D plate/min: screen=D, crush=D/2, smelt=D/2.
"""

from __future__ import annotations

import pytest
from planner.techbundle import (
    classify_boundary,
    default_anchor,
    find_components,
    solve_component,
    solve_linear_system,
)

pytestmark = pytest.mark.unit

COPPER_IOS = {
    "screen": {"ingredients": [("ore", 5)], "products": [("g1", 1), ("g2", 2)]},
    "crush": {"ingredients": [("g1", 2)], "products": [("g2", 1), ("stone", 2)]},
    "smelt": {"ingredients": [("g2", 5)], "products": [("plate", 2)]},
}
COPPER_COMPONENT = {"screen", "crush", "smelt"}


class TestFindComponents:
    def test_copper_bundle_is_one_component(self) -> None:
        components = find_components(COPPER_IOS)
        assert components == [COPPER_COMPONENT]

    def test_disjoint_recipes_are_separate_singletons(self) -> None:
        ios = {
            "r1": {"ingredients": [("a", 1)], "products": [("b", 1)]},
            "r2": {"ingredients": [("c", 1)], "products": [("d", 1)]},
        }
        components = find_components(ios)
        assert sorted(components, key=lambda s: sorted(s)) == [{"r1"}, {"r2"}]

    def test_mixed_connected_and_isolated_recipes(self) -> None:
        ios = dict(COPPER_IOS)
        ios["unrelated"] = {"ingredients": [("x", 1)], "products": [("y", 1)]}
        components = find_components(ios)
        assert sorted(components, key=lambda s: sorted(s)) == [
            COPPER_COMPONENT,
            {"unrelated"},
        ]


class TestClassifyBoundary:
    def test_copper_bundle_boundary(self) -> None:
        boundary = classify_boundary(COPPER_COMPONENT, COPPER_IOS)
        assert boundary["internal"] == {"g1", "g2"}
        assert boundary["external_inputs"] == {"ore"}
        assert boundary["external_outputs"] == {"stone", "plate"}

    def test_private_catalyst_is_excluded_from_all_three(self) -> None:
        """An item produced AND consumed only by the SAME single recipe
        (a catalyst) must not appear as internal (would force that recipe's
        rate to zero via a spurious row) or as external (it never crosses
        the component boundary at all)."""
        ios = {
            "r1": {
                "ingredients": [("catalyst", 1), ("ore", 5)],
                "products": [("catalyst", 1), ("plate", 2)],
            }
        }
        boundary = classify_boundary({"r1"}, ios)
        assert "catalyst" not in boundary["internal"]
        assert "catalyst" not in boundary["external_inputs"]
        assert "catalyst" not in boundary["external_outputs"]
        assert boundary["external_inputs"] == {"ore"}
        assert boundary["external_outputs"] == {"plate"}


class TestSolveLinearSystem:
    def test_unique_square_system(self) -> None:
        # x + y = 3; x - y = 1 -> x=2, y=1
        result = solve_linear_system([[1, 1], [1, -1]], [3, 1])
        assert result["status"] == "unique"
        assert result["solution"] == [2, 1]

    def test_redundant_consistent_row_is_still_unique(self) -> None:
        # x + y = 3; x - y = 1; 2x = 4 (redundant, consistent) -> x=2, y=1
        result = solve_linear_system([[1, 1], [1, -1], [2, 0]], [3, 1, 4])
        assert result["status"] == "unique"
        assert result["solution"] == [2, 1]

    def test_underdetermined(self) -> None:
        # x + y - 2z = 0; z = 5 -- 2 equations, 3 unknowns, rank 2 < 3
        result = solve_linear_system([[1, 1, -2], [0, 0, 1]], [0, 5])
        assert result["status"] == "underdetermined"
        assert result["rank"] == 2

    def test_infeasible_contradiction(self) -> None:
        # x = 0 (from combining rows); anchor forces x = 10 -> contradiction
        result = solve_linear_system([[1, -2], [1, -1], [0, 1]], [0, 0, 10])
        assert result["status"] == "infeasible"


class TestSolveComponent:
    def test_copper_bundle_exact_blend(self) -> None:
        result = solve_component(
            COPPER_COMPONENT, COPPER_IOS, anchor_item="plate", target_rate=10.0
        )
        assert result["status"] == "solved"
        assert result["batch_rates"] == {"screen": 10.0, "crush": 5.0, "smelt": 5.0}

    def test_scales_linearly_with_target_rate(self) -> None:
        result = solve_component(
            COPPER_COMPONENT, COPPER_IOS, anchor_item="plate", target_rate=60.0
        )
        assert result["batch_rates"] == {"screen": 60.0, "crush": 30.0, "smelt": 30.0}

    def test_underdetermined_reports_no_guess(self) -> None:
        ios = {
            "r1": {"ingredients": [], "products": [("X", 1)]},
            "r2": {"ingredients": [], "products": [("X", 1)]},
            "r3": {"ingredients": [("X", 2)], "products": [("Y", 1)]},
        }
        result = solve_component({"r1", "r2", "r3"}, ios, anchor_item="Y", target_rate=10.0)
        assert result["status"] == "underdetermined"
        assert result["batch_rates"] is None

    def test_infeasible_reports_reason(self) -> None:
        ios = {
            "r1": {"ingredients": [], "products": [("X", 1), ("Y", 1)]},
            "r2": {"ingredients": [("X", 2), ("Y", 1)], "products": [("P", 1)]},
        }
        result = solve_component({"r1", "r2"}, ios, anchor_item="P", target_rate=10.0)
        assert result["status"] == "infeasible"
        assert result["batch_rates"] is None

    def test_negative_infeasible_when_loop_is_a_net_sink(self) -> None:
        """r1 spends 1 P to make 1 X; r2 spends 2 X to make 1 P -- running
        this loop forward always loses P net, so no positive blend can hit
        a positive net-P target; the unique algebraic solution is negative."""
        ios = {
            "r1": {"ingredients": [("P", 1)], "products": [("X", 1)]},
            "r2": {"ingredients": [("X", 2)], "products": [("P", 1)]},
        }
        result = solve_component({"r1", "r2"}, ios, anchor_item="P", target_rate=10.0)
        assert result["status"] == "negative_infeasible"
        assert result["batch_rates"] is None

    def test_probabilistic_yield_uses_expected_amount(self) -> None:
        """Mirrors the real Tin-processing case: r1's secondary product X
        is already collapsed to its EXPECTED amount (0.5 = raw amount 1 x
        50% probability) by the caller before it ever reaches this module."""
        ios = {
            "r1": {"ingredients": [], "products": [("A", 1), ("X", 0.5)]},
            "r2": {"ingredients": [("X", 1)], "products": [("P", 1)]},
        }
        result = solve_component({"r1", "r2"}, ios, anchor_item="P", target_rate=10.0)
        assert result["status"] == "solved"
        # 0.5*r1 = r2 (balance on X) and r2 = 10 (anchor) -> r1 = 20
        assert result["batch_rates"] == {"r1": 20.0, "r2": 10.0}

    def test_singleton_component_with_catalyst_still_solves(self) -> None:
        ios = {
            "r1": {
                "ingredients": [("catalyst", 1), ("ore", 5)],
                "products": [("catalyst", 1), ("plate", 2)],
            }
        }
        result = solve_component({"r1"}, ios, anchor_item="plate", target_rate=10.0)
        assert result["status"] == "solved"
        assert result["batch_rates"] == {"r1": 5.0}


class TestDefaultAnchor:
    def test_copper_bundle_prefers_deepest_output(self) -> None:
        # plate (depth 2, via smelt) is deeper than stone (depth 1, via crush)
        anchor = default_anchor(COPPER_COMPONENT, COPPER_IOS, {"stone", "plate"})
        assert anchor == "plate"

    def test_single_external_output_short_circuits(self) -> None:
        assert default_anchor(COPPER_COMPONENT, COPPER_IOS, {"plate"}) == "plate"

    def test_no_external_outputs_returns_none(self) -> None:
        assert default_anchor(COPPER_COMPONENT, COPPER_IOS, set()) is None

    def test_cycle_ties_break_alphabetically(self) -> None:
        """rA and rB mutually feed each other (a genuine cycle -> one SCC),
        so both their external outputs tie at the same depth; the tie must
        resolve deterministically (alphabetical) instead of crashing."""
        ios = {
            "rA": {"ingredients": [("N", 1)], "products": [("M", 1), ("out_b", 1)]},
            "rB": {"ingredients": [("M", 1)], "products": [("N", 1), ("out_a", 1)]},
        }
        anchor = default_anchor({"rA", "rB"}, ios, {"out_a", "out_b"})
        assert anchor == "out_a"
