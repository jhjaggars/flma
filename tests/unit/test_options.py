"""Unit tests for planner/options.py — the decision-menu helpers behind
`planner options`. All pure functions operating on plain dicts/numbers, no
recipes.db or live game state needed (mirrors test_cli.py's convention for
DB-free planner logic)."""

from __future__ import annotations

import pytest
from planner.options import (
    ABSURD_MACHINE_THRESHOLD,
    classify_producer,
    deeper_choices,
    tree_categories,
    tree_stages,
)

pytestmark = pytest.mark.unit


class TestClassifyProducer:
    def test_main_product_full_yield_is_shown(self) -> None:
        """A recipe whose main_product is the item, with a normal (100%)
        yield and a sane machine count, is never hidden."""
        cls = classify_producer(
            recipe_id="copper-smelting",
            is_main_product=True,
            probability=1.0,
            eff_out=1.0,
            energy=3.2,
            fastest_speed=2.0,
            yardstick_per_min=60.0,
        )
        assert cls["byproduct"] is False
        assert cls["absurd"] is False
        assert cls["hidden"] is False

    def test_low_probability_non_main_product_is_byproduct(self) -> None:
        """Not the recipe's main product AND a probabilistic yield — exactly
        the Pyanodons byproduct-fishing case the engine's own Tier 1
        main_product preference exists to avoid picking by default."""
        cls = classify_producer(
            recipe_id="byproduct-fishing",
            is_main_product=False,
            probability=0.02,
            eff_out=0.02,
            energy=1.0,
            fastest_speed=1.0,
            yardstick_per_min=60.0,
        )
        assert cls["byproduct"] is True
        assert cls["hidden"] is True

    def test_main_product_probabilistic_is_not_byproduct(self) -> None:
        """A probabilistic yield doesn't make a recipe a byproduct source if
        the item IS this recipe's main_product (e.g. uranium processing) —
        only a non-main-product probabilistic yield counts."""
        cls = classify_producer(
            recipe_id="uranium-processing",
            is_main_product=True,
            probability=0.35,
            eff_out=0.35,
            energy=12.0,
            fastest_speed=1.0,
            yardstick_per_min=60.0,
        )
        assert cls["byproduct"] is False

    def test_slow_but_primary_recipe_is_shown_not_hidden(self) -> None:
        """A legitimate main_product recipe that's simply slow (e.g. a
        Pyanodons fish farm needing ~130 machines for 1/sec) is flagged
        `absurd` and would be hidden by default, but is never miscategorized
        as a byproduct — the two flags are independent."""
        cls = classify_producer(
            recipe_id="fish-farm",
            is_main_product=True,
            probability=1.0,
            eff_out=0.01,
            energy=1.0,
            fastest_speed=1.0,
            yardstick_per_min=60.0,
        )
        assert cls["byproduct"] is False
        assert cls["absurd"] is True
        assert cls["hidden"] is True
        assert cls["machines_per_yardstick"] > ABSURD_MACHINE_THRESHOLD

    def test_reasonable_machine_count_not_absurd(self) -> None:
        cls = classify_producer(
            recipe_id="electronic-circuit",
            is_main_product=True,
            probability=1.0,
            eff_out=1.0,
            energy=0.5,
            fastest_speed=1.25,
            yardstick_per_min=60.0,
        )
        assert cls["absurd"] is False
        assert cls["machines_per_yardstick"] <= ABSURD_MACHINE_THRESHOLD

    def test_zero_energy_or_speed_is_defensive_not_absurd(self) -> None:
        """A degenerate zero energy/speed/eff_out shouldn't blow up into a
        bogus machine count — treated as 0 machines (caller should already
        be filtering these out elsewhere, e.g. `_one_machine_rate_per_min`
        bailing on energy<=0)."""
        cls = classify_producer(
            recipe_id="degenerate",
            is_main_product=True,
            probability=1.0,
            eff_out=0.0,
            energy=0.0,
            fastest_speed=0.0,
            yardstick_per_min=60.0,
        )
        assert cls["machines_per_yardstick"] == 0
        assert cls["absurd"] is False


class TestTreeStages:
    def test_leaf_is_zero_stages(self) -> None:
        assert tree_stages({"leaf": True, "stop_reason": "stop_item"}) == 0

    def test_single_stage(self) -> None:
        node = {
            "leaf": False,
            "recipe": {"id": "r1", "category": "crafting"},
            "ingredients": [{"leaf": True, "stop_reason": "stop_item"}],
        }
        assert tree_stages(node) == 1

    def test_multi_stage_takes_deepest_branch(self) -> None:
        # target <- mid <- leaf (2 stages), and target <- leaf (1 stage) in
        # parallel — the deepest branch wins.
        deep_branch = {
            "leaf": False,
            "recipe": {"id": "r-mid", "category": "crafting"},
            "ingredients": [{"leaf": True, "stop_reason": "stop_item"}],
        }
        shallow_branch = {"leaf": True, "stop_reason": "stop_item"}
        root = {
            "leaf": False,
            "recipe": {"id": "r-top", "category": "crafting"},
            "ingredients": [deep_branch, shallow_branch],
        }
        assert tree_stages(root) == 2


class TestTreeCategories:
    def test_collects_all_categories_in_chain(self) -> None:
        leaf = {"leaf": True, "stop_reason": "stop_item"}
        mid = {
            "leaf": False,
            "recipe": {"id": "r-crush", "category": "crushing"},
            "ingredients": [leaf],
        }
        root = {
            "leaf": False,
            "recipe": {"id": "r-smelt", "category": "smelting"},
            "ingredients": [mid],
        }
        out: set[str] = set()
        tree_categories(root, out)
        assert out == {"crushing", "smelting"}

    def test_leaf_contributes_nothing(self) -> None:
        out: set[str] = set()
        tree_categories({"leaf": True, "stop_reason": "no_recipe"}, out)
        assert out == set()


class TestDeeperChoices:
    def test_finds_item_with_multiple_available_alternates(self) -> None:
        alt_map = {
            "copper-plate": [
                {"id": "copper-smelting", "tag": "available"},
                {"id": "copper-crush-smelt", "tag": "available"},
            ],
            "crushed-copper": [
                {"id": "recipe-a", "tag": "available"},
                {"id": "recipe-b", "tag": "available"},
            ],
        }
        result = deeper_choices(alt_map, top_item_id="copper-plate")
        assert result == [("crushed-copper", 2)]

    def test_excludes_top_level_item(self) -> None:
        alt_map = {
            "copper-plate": [
                {"id": "a", "tag": "available"},
                {"id": "b", "tag": "available"},
            ],
        }
        assert deeper_choices(alt_map, top_item_id="copper-plate") == []

    def test_single_available_alternate_is_not_a_choice(self) -> None:
        alt_map = {"item-x": [{"id": "only-one", "tag": "available"}]}
        assert deeper_choices(alt_map, top_item_id="root") == []

    def test_tech_locked_alternates_dont_count_as_available(self) -> None:
        alt_map = {
            "item-x": [
                {"id": "a", "tag": "available"},
                {"id": "b", "tag": "tech_locked"},
            ],
        }
        assert deeper_choices(alt_map, top_item_id="root") == []
