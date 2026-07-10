"""Unit tests for planner/cli.py helpers that don't need a live DB/game
state — pure string/data formatting logic."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from planner import cli, config
from planner.cli import (
    _apply_mining_productivity_to_drills,
    _fastest_buildable_belt_tier,
    _fmt_watts,
    _is_tech_locked,
    _parse_rate_per_min,
    _print_tree,
    _rate_for_belt_cap,
    _rate_per_min_or_default,
)

pytestmark = pytest.mark.unit


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


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


class TestLiveObserveCommands:
    """Smoke tests for the research/tech-tree/production/logistics/inventory/
    buildings subcommands -- these only need GameState (config.SCRIPT_OUTPUT_DIR
    pointed at a fixture dir), no recipe DB engine."""

    def _point_at(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(config, "SCRIPT_OUTPUT_DIR", tmp_path)

    async def test_research_json_output(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._point_at(monkeypatch, tmp_path)
        write_json(
            tmp_path / "tech.json",
            {
                "tick": 1,
                "forces": {
                    "player": {
                        "current_research": "automation-2",
                        "research_progress": 0.5,
                        "research_queue": [],
                        "technologies": {},
                    }
                },
            },
        )
        args = argparse.Namespace(force="player", json=True)
        assert await cli.cmd_research(args) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["current_research"] == "automation-2"

    async def test_research_text_output_on_no_data(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._point_at(monkeypatch, tmp_path)
        args = argparse.Namespace(force="player", json=False)
        assert await cli.cmd_research(args) == 1
        assert "error" in capsys.readouterr().err

    async def test_tech_tree_status_filter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._point_at(monkeypatch, tmp_path)
        write_json(
            tmp_path / "tech.json",
            {
                "tick": 1,
                "forces": {
                    "player": {
                        "technologies": {
                            "a": {"researched": True, "enabled": True, "prerequisites": []},
                            "b": {"researched": False, "enabled": True, "prerequisites": []},
                        }
                    }
                },
            },
        )
        args = argparse.Namespace(force="player", status="researched", json=True)
        assert await cli.cmd_tech_tree(args) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["count"] == 1

    async def test_production_json_output(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._point_at(monkeypatch, tmp_path)
        write_json(
            tmp_path / "production.json",
            {
                "tick": 1,
                "forces": {
                    "player": {
                        "surfaces": {
                            "nauvis": {
                                "items": {
                                    "input_counts": {},
                                    "output_counts": {},
                                    "input_rates_per_min": {"iron-plate": 120.0},
                                    "output_rates_per_min": {},
                                },
                                "fluids": {"input_counts": {}, "output_counts": {}},
                            }
                        }
                    }
                },
            },
        )
        args = argparse.Namespace(force="player", surface=None, kind="both", json=False)
        assert await cli.cmd_production(args) == 0
        out = capsys.readouterr().out
        assert "iron-plate" in out

    async def test_buildings_counts_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._point_at(monkeypatch, tmp_path)
        args = argparse.Namespace(
            name=None, type=None, surface=None, force=None, list=False, limit=100, json=True
        )
        assert await cli.cmd_buildings(args) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["total"] == 0


class TestFmtWatts:
    def test_none_is_unknown(self) -> None:
        assert _fmt_watts(None) == "?"

    def test_watts_below_1000(self) -> None:
        assert _fmt_watts(500) == "500 W"

    def test_kilowatts(self) -> None:
        assert _fmt_watts(90000) == "90 kW"

    def test_megawatts(self) -> None:
        assert _fmt_watts(7880000) == "7.88 MW"


class _FakePowerDB:
    """Duck-typed stand-in for recipe-mcp's engine.db — only the query
    shapes `_resolve_power_entity`/`cmd_power` actually issue against
    machines/mining_drills/generators/fuels/machine_fuel_categories."""

    _TABLES = ("machines", "mining_drills", "generators")

    def __init__(
        self,
        machines: list[dict] | None = None,
        mining_drills: list[dict] | None = None,
        generators: list[dict] | None = None,
        fuels: list[dict] | None = None,
        machine_fuel_categories: list[dict] | None = None,
    ) -> None:
        self.machines = machines or []
        self.mining_drills = mining_drills or []
        self.generators = generators or []
        self.fuels = fuels or []
        self.machine_fuel_categories = machine_fuel_categories or []

    def _rows_for(self, query: str) -> list[dict]:
        for table in self._TABLES:
            if f"FROM {table}" in query:
                return getattr(self, table)
        raise AssertionError(f"unrecognized table in query: {query}")

    async def fetch_one(self, query: str, params: tuple = ()) -> dict | None:
        rows = self._rows_for(query)
        (value,) = params
        if "translated_name = ?" in query:
            for row in rows:
                if row["translated_name"].lower() == value.lower():
                    return row
            return None
        for row in rows:
            if row["name"] == value:
                return row
        return None

    async def fetch_all(self, query: str, params: tuple) -> list[dict]:
        if "JOIN fuels" in query:
            (machine_name,) = params
            categories = {
                r["fuel_category"]
                for r in self.machine_fuel_categories
                if r["machine_name"] == machine_name
            }
            matches = [f for f in self.fuels if f["fuel_category"] in categories]
            return sorted(matches, key=lambda f: f["translated_name"].lower())
        rows = self._rows_for(query)
        needle, _ = params
        needle = needle.strip("%").lower()
        matches = [
            {"name": r["name"], "translated_name": r["translated_name"]}
            for r in rows
            if needle in r["name"].lower() or needle in r["translated_name"].lower()
        ]
        return matches[:10]


class _FakePowerEngine:
    def __init__(self, db: _FakePowerDB) -> None:
        self.db = db


COAL_POWERPLANT = {
    "name": "py-coal-powerplant-mk01",
    "translated_name": "Coal powerplant MK 01",
    "energy_consumption": 10_000_000.0,
    "energy_source": "burner",
    "burner_effectivity": 1.0,
}
ELECTRIC_DRILL = {
    "name": "electric-mining-drill",
    "translated_name": "Electric mining drill",
    "energy_consumption": 90_000.0,
    "energy_source": "electric",
    "burner_effectivity": None,
}
STEAM_TURBINE = {
    "name": "steam-turbine-mk01",
    "translated_name": "Steam turbine MK01",
    "max_power_output": 7_880_000.0,
    "fluid_usage_per_sec": 60.0,
    "input_fluid": "pressured-steam",
    "effectivity": 1.0,
    "maximum_temperature": 1000.0,
}
COAL_FUEL = {
    "name": "coal",
    "translated_name": "Coal",
    "fuel_category": "chemical",
    "fuel_value": 8_000_000.0,
}


class TestCmdPower:
    def _engine(self) -> _FakePowerEngine:
        return _FakePowerEngine(
            _FakePowerDB(
                machines=[COAL_POWERPLANT],
                mining_drills=[ELECTRIC_DRILL],
                generators=[STEAM_TURBINE],
                fuels=[COAL_FUEL],
                machine_fuel_categories=[
                    {"machine_name": "py-coal-powerplant-mk01", "fuel_category": "chemical"}
                ],
            )
        )

    async def test_burner_machine_prints_burn_rate(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli, "_make_engine", lambda: self._engine())
        args = argparse.Namespace(entity="py-coal-powerplant-mk01")
        assert await cli.cmd_power(args) == 0
        out = capsys.readouterr().out
        assert "10 MW" in out
        assert "coal" in out
        # 10,000,000 W / (8,000,000 J * 1.0 effectivity) = 1.25/s = 75/min
        assert "1.25/s" in out
        assert "75/min" in out

    async def test_generator_prints_output_and_fluid_usage(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli, "_make_engine", lambda: self._engine())
        args = argparse.Namespace(entity="steam-turbine-mk01")
        assert await cli.cmd_power(args) == 0
        out = capsys.readouterr().out
        assert "7.88 MW" in out
        assert "pressured-steam" in out
        assert "60/s" in out

    async def test_electric_machine_has_no_fuel_section(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli, "_make_engine", lambda: self._engine())
        args = argparse.Namespace(entity="electric-mining-drill")
        assert await cli.cmd_power(args) == 0
        out = capsys.readouterr().out
        assert "90 kW" in out
        assert "electric" in out
        assert "fuel" not in out

    async def test_fuzzy_single_match_resolves(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli, "_make_engine", lambda: self._engine())
        args = argparse.Namespace(entity="turbine")
        assert await cli.cmd_power(args) == 0
        out = capsys.readouterr().out
        assert "steam-turbine-mk01" in out

    async def test_no_match_returns_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli, "_make_engine", lambda: self._engine())
        args = argparse.Namespace(entity="nonexistent-thing")
        assert await cli.cmd_power(args) == 1
        assert "no machine, drill, or generator found" in capsys.readouterr().out

    async def test_ambiguous_match_returns_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        engine = _FakePowerEngine(
            _FakePowerDB(
                machines=[
                    COAL_POWERPLANT,
                    {**COAL_POWERPLANT, "name": "py-coal-powerplant-mk02"},
                ]
            )
        )
        monkeypatch.setattr(cli, "_make_engine", lambda: engine)
        args = argparse.Namespace(entity="coal-powerplant")
        assert await cli.cmd_power(args) == 1
        assert "ambiguous" in capsys.readouterr().out

    async def test_burner_with_no_eligible_fuels_shown_gracefully(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """DB predates fuel export (empty fuels/machine_fuel_categories) --
        must not crash, just say so."""
        engine = _FakePowerEngine(_FakePowerDB(machines=[COAL_POWERPLANT]))
        monkeypatch.setattr(cli, "_make_engine", lambda: engine)
        args = argparse.Namespace(entity="py-coal-powerplant-mk01")
        assert await cli.cmd_power(args) == 0
        assert "no eligible fuels found" in capsys.readouterr().out
