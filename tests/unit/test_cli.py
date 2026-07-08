"""Unit tests for planner/cli.py helpers that don't need a live DB/game
state — pure string/data formatting logic."""

from __future__ import annotations

import pytest
from planner.cli import (
    _apply_mining_productivity_to_drills,
    _fastest_buildable_belt_tier,
    _is_tech_locked,
    _parse_rate_per_min,
    _print_tree,
    _rate_for_belt_cap,
    _rate_per_min_or_default,
)

pytestmark = pytest.mark.unit


class TestIsTechLocked:
    def test_matches_exact_item(self) -> None:
        notes = [
            "ore-chromium: only tech-locked producer(s) available (requires: X) — treating as raw"
        ]
        assert _is_tech_locked("ore-chromium", notes) is True

    def test_no_false_positive_on_substring(self) -> None:
        # "water" is a substring of "geothermal-water" — a naive `in` check
        # over the whole note text would wrongly tag plain water as
        # tech-locked too.
        notes = [
            "geothermal-water: only tech-locked producer(s) available (requires: X) — treating as raw"
        ]
        assert _is_tech_locked("water", notes) is False
        assert _is_tech_locked("geothermal-water", notes) is True

    def test_no_notes(self) -> None:
        assert _is_tech_locked("sand", []) is False


class TestPrintTreeAlternates:
    """`_print_tree`'s `--alternates` rendering — pure formatting over a
    hand-built tree/alternates_map, no engine or DB involved."""

    _TREE = {
        "id": "copper-plate",
        "name": "Copper plate",
        "amount": 60.0,
        "kind": "item",
        "leaf": False,
        "recipe": {"id": "copper-smelting", "name": "Copper smelting", "batches": 2.0},
        "ingredients": [
            {
                "id": "copper-ore",
                "name": "Copper ore",
                "amount": 60.0,
                "kind": "item",
                "leaf": True,
                "stop_reason": "stop_item",
            }
        ],
    }
    _ALTERNATES = {
        "copper-plate": [
            {
                "id": "copper-smelting",
                "name": "Copper smelting",
                "selected": True,
                "tag": "available",
            },
            {
                "id": "copper-crush-smelt",
                "name": "Crushed copper smelting",
                "selected": False,
                "tag": "tech_locked",
                "tag_reason": "requires research not yet completed",
            },
        ]
    }

    def test_default_omits_alternates(self, capsys: pytest.CaptureFixture[str]) -> None:
        _print_tree(self._TREE, alternates_map=self._ALTERNATES, show_alternates=False)
        out = capsys.readouterr().out
        assert "copper-crush-smelt" not in out

    def test_show_alternates_lists_non_selected_candidates(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _print_tree(self._TREE, alternates_map=self._ALTERNATES, show_alternates=True)
        out = capsys.readouterr().out
        assert "copper-crush-smelt" in out
        assert "tech_locked" in out
        assert "requires research not yet completed" in out
        # The selected candidate (the one already shown as the node's own
        # recipe) shouldn't also appear as an "alt:" line.
        alt_lines = [line for line in out.splitlines() if "alt:" in line]
        assert not any("copper-smelting" in line for line in alt_lines)

    def test_no_alternates_map_is_safe(self, capsys: pytest.CaptureFixture[str]) -> None:
        """show_alternates=True with no map provided shouldn't crash — just
        prints the tree as normal."""
        _print_tree(self._TREE, show_alternates=True)
        out = capsys.readouterr().out
        assert "copper-plate" in out


class _FakeDB:
    def __init__(self, existing_names: set[str]) -> None:
        self._existing_names = existing_names

    async def fetch_one(self, query: str, params: tuple = ()) -> dict | None:
        (name,) = params
        return {"1": 1} if name in self._existing_names else None


class _FakeEngine:
    """Duck-typed stand-in for recipe-mcp's engine module — only the pieces
    `_fastest_buildable_belt_tier` touches (engine.db.fetch_one and
    engine._is_item_buildable)."""

    def __init__(self, existing_names: set[str], buildable: set[str]) -> None:
        self.db = _FakeDB(existing_names)
        self._buildable = buildable

    async def _is_item_buildable(
        self, item_name: str, extra_unlocked: frozenset[str]
    ) -> tuple[bool, list[str]]:
        return (item_name in self._buildable, [] if item_name in self._buildable else ["some-tech"])


class TestFastestBuildableBeltTier:
    async def test_picks_fastest_buildable_tier(self) -> None:
        engine = _FakeEngine(
            existing_names={"transport-belt", "fast-transport-belt", "express-transport-belt"},
            buildable={"transport-belt", "fast-transport-belt"},
        )
        tier = await _fastest_buildable_belt_tier(engine, frozenset())
        assert tier == "fast-transport-belt"

    async def test_skips_tier_absent_from_modpack_even_if_reported_buildable(self) -> None:
        """A tier with no recipe row at all in this modpack's DB (e.g.
        Space Age's turbo-transport-belt in a Pyanodons DB) must not win
        just because `_is_item_buildable` treats "no recipe" as "always
        available" (that fallback is meant for bare starter entities, not
        items that don't exist in this modpack)."""
        engine = _FakeEngine(
            existing_names={"transport-belt"},
            buildable={"transport-belt", "turbo-transport-belt"},
        )
        tier = await _fastest_buildable_belt_tier(engine, frozenset())
        assert tier == "transport-belt"

    async def test_none_when_nothing_buildable(self) -> None:
        engine = _FakeEngine(
            existing_names={"transport-belt", "fast-transport-belt"},
            buildable=set(),
        )
        tier = await _fastest_buildable_belt_tier(engine, frozenset())
        assert tier is None


class _FakePlanProductEngine:
    """Duck-typed stand-in for recipe-mcp's engine module -- only the piece
    `_rate_for_belt_cap` touches (engine.plan_product), returning a canned
    raw_inputs list regardless of the requested rate_per_min (the real
    engine's raw_inputs scale linearly with rate; `_rate_for_belt_cap` only
    needs one reference call, so faking a single fixed response is enough
    to test its scale-factor math in isolation)."""

    def __init__(self, raw_inputs: list[dict]) -> None:
        self._raw_inputs = raw_inputs

    async def plan_product(self, product_id: str, *, rate_per_min: float, max_depth: int, **_scoping) -> dict:
        return {"raw_inputs": self._raw_inputs}


class TestRateForBeltCap:
    async def test_scales_to_cap_the_bottleneck_input(self) -> None:
        # transport-belt carries 900/min -- ore needs 5 belts, sand needs 2.
        engine = _FakePlanProductEngine(
            [
                {"id": "ore", "name": "Ore", "amount_per_min": 4500.0, "kind": "item"},
                {"id": "sand", "name": "Sand", "amount_per_min": 1800.0, "kind": "item"},
            ]
        )
        result = await _rate_for_belt_cap(engine, "battery-mk01", 6, {}, cap_count=1.0)
        assert result is not None
        rate_per_min, info = result
        assert rate_per_min == pytest.approx(12.0)
        assert info["bottleneck_id"] == "ore"
        assert info["unit_plural"] == "transport-belt belts"

    async def test_half_belt_cap(self) -> None:
        engine = _FakePlanProductEngine(
            [{"id": "ore", "name": "Ore", "amount_per_min": 900.0, "kind": "item"}]
        )
        result = await _rate_for_belt_cap(engine, "x", 6, {}, cap_count=0.5)
        assert result is not None
        rate_per_min, _info = result
        assert rate_per_min == pytest.approx(30.0)

    async def test_none_when_no_raw_inputs(self) -> None:
        engine = _FakePlanProductEngine([])
        result = await _rate_for_belt_cap(engine, "closed-loop-item", 6, {}, cap_count=1.0)
        assert result is None

    async def test_fluid_input_uses_pipes_not_belts_for_the_bottleneck_pick(self) -> None:
        """A fluid moving 900/min (15/sec) is a full transport-belt's worth
        by belt math, but pipes carry 1200/sec -- 72000/min -- so it should
        never out-rank a genuinely belt-constrained item input just because
        it was mistakenly measured in belts."""
        engine = _FakePlanProductEngine(
            [
                {"id": "water", "name": "Water", "amount_per_min": 900.0, "kind": "fluid"},
                {"id": "ore", "name": "Ore", "amount_per_min": 1800.0, "kind": "item"},
            ]
        )
        result = await _rate_for_belt_cap(engine, "x", 6, {}, cap_count=1.0)
        assert result is not None
        _rate_per_min, info = result
        assert info["bottleneck_id"] == "ore"
        assert info["unit_plural"] == "transport-belt belts"


class TestParseRatePerMin:
    def test_bare_number_uses_default_unit_per_sec(self) -> None:
        assert _parse_rate_per_min("15", "per-sec") == pytest.approx(900.0)

    def test_bare_number_uses_default_unit_per_min(self) -> None:
        assert _parse_rate_per_min("900", "per-min") == pytest.approx(900.0)

    @pytest.mark.parametrize("suffix", ["/s", "/sec", "/second"])
    def test_per_sec_suffix_overrides_default_unit(self, suffix: str) -> None:
        # default_unit is per-min here -- the suffix should still win.
        assert _parse_rate_per_min(f"15{suffix}", "per-min") == pytest.approx(900.0)

    @pytest.mark.parametrize("suffix", ["/m", "/min", "/minute"])
    def test_per_min_suffix_overrides_default_unit(self, suffix: str) -> None:
        # default_unit is per-sec here -- the suffix should still win.
        assert _parse_rate_per_min(f"900{suffix}", "per-sec") == pytest.approx(900.0)

    def test_suffix_is_case_insensitive(self) -> None:
        assert _parse_rate_per_min("15/S", "per-min") == pytest.approx(900.0)

    def test_unparseable_raises_with_original_value_in_message(self) -> None:
        with pytest.raises(ValueError, match="abc"):
            _parse_rate_per_min("abc", "per-sec")

    def test_unparseable_after_stripping_a_lookalike_suffix_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_rate_per_min("abc/min", "per-sec")


class TestRatePerMinOrDefault:
    def test_none_returns_default(self) -> None:
        assert _rate_per_min_or_default(None, "per-sec") == 60.0

    def test_none_returns_custom_default(self) -> None:
        assert _rate_per_min_or_default(None, "per-sec", default=42.0) == 42.0

    def test_value_delegates_to_parse_rate_per_min(self) -> None:
        assert _rate_per_min_or_default("15/s", "per-min") == pytest.approx(900.0)


def _drill(drill_count: int, fluid_rate_per_min: float = 0.0) -> dict:
    return {
        "resource": "ore-lead",
        "resource_category": "basic-with-fluid",
        "drill_id": "fluid-drill-mk02",
        "drill_name": "Fluid mining drill MK 02",
        "drill_count": drill_count,
        "required_fluid": "acetylene" if fluid_rate_per_min else None,
        "fluid_rate_per_min": fluid_rate_per_min,
        "approximate": True,
        "note": "Ignores purity and productivity bonuses",
    }


class TestApplyMiningProductivityToDrills:
    def test_zero_bonus_is_a_no_op(self) -> None:
        drills = [_drill(15, 90000.0)]
        assert _apply_mining_productivity_to_drills(drills, 0.0) is drills

    def test_bonus_reduces_drill_count_and_fluid_rate(self) -> None:
        # +20% yield per mining cycle -> ~1/1.2 as many drills/fluid needed
        # for the same ore/min.
        drills = [_drill(15, 90000.0)]
        adjusted = _apply_mining_productivity_to_drills(drills, 0.2)
        assert adjusted[0]["drill_count"] == 13  # ceil(15 / 1.2)
        assert adjusted[0]["fluid_rate_per_min"] == pytest.approx(75000.0)

    def test_note_records_the_applied_bonus(self) -> None:
        drills = [_drill(15)]
        adjusted = _apply_mining_productivity_to_drills(drills, 0.2)
        assert "+20%" in adjusted[0]["note"]

    def test_original_drills_list_is_untouched(self) -> None:
        drills = [_drill(15, 90000.0)]
        _apply_mining_productivity_to_drills(drills, 0.2)
        assert drills[0]["drill_count"] == 15
        assert drills[0]["fluid_rate_per_min"] == 90000.0
