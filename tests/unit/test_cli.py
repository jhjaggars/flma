"""Unit tests for planner/cli.py helpers that don't need a live DB/game
state — pure string/data formatting logic."""

from __future__ import annotations

import pytest
from planner.cli import (
    _fastest_buildable_belt_tier,
    _is_tech_locked,
    _parse_rate_per_min,
    _print_tree,
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
