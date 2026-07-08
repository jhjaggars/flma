"""Unit tests for planner/live_state.py's net_production: sign correctness
(input = produced, output = consumed) and the rates-only behavior (no
silent fallback to lifetime cumulative totals when the mod snapshot predates
the per-minute rate fields)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from planner import live_state
from src.game_state import GameState

pytestmark = pytest.mark.unit


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def write_ndjson(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def _add_event(entity_id: int, name: str, force: str = "player") -> dict:
    return {
        "t": entity_id,
        "op": "add",
        "entity": {
            "id": entity_id,
            "name": name,
            "type": "mining-drill",
            "surface": "nauvis",
            "position": {"x": 0.0, "y": 0.0},
            "force": force,
        },
    }


def _production_with_rates(surface_items: dict, surface_fluids: dict | None = None) -> dict:
    return {
        "tick": 1,
        "forces": {
            "player": {
                "surfaces": {
                    "nauvis": {
                        "items": surface_items,
                        "fluids": surface_fluids
                        or {
                            "input_counts": {},
                            "output_counts": {},
                            "input_rates_per_min": {},
                            "output_rates_per_min": {},
                        },
                    }
                }
            }
        },
    }


class TestNetProduction:
    def test_net_is_input_minus_output(self, tmp_path: Path) -> None:
        # In LuaFlowStatistics, input_counts = what the force *produced*,
        # output_counts = what it *consumed*. A force producing 120/min of
        # iron-plate and consuming 30/min of it (e.g. some feeds back into
        # another recipe) should show net = +90/min, not -90/min.
        write_json(
            tmp_path / "production.json",
            _production_with_rates(
                {
                    "input_counts": {"iron-plate": 12000},
                    "output_counts": {"iron-plate": 3000},
                    "input_rates_per_min": {"iron-plate": 120.0},
                    "output_rates_per_min": {"iron-plate": 30.0},
                }
            ),
        )
        gs = GameState(tmp_path, min_refresh_interval=0)
        net = live_state.net_production(gs)
        assert net["iron-plate"] == pytest.approx(90.0)

    def test_pure_consumption_is_negative(self, tmp_path: Path) -> None:
        write_json(
            tmp_path / "production.json",
            _production_with_rates(
                {
                    "input_counts": {},
                    "output_counts": {"iron-ore": 6000},
                    "input_rates_per_min": {},
                    "output_rates_per_min": {"iron-ore": 60.0},
                }
            ),
        )
        gs = GameState(tmp_path, min_refresh_interval=0)
        net = live_state.net_production(gs)
        assert net["iron-ore"] == pytest.approx(-60.0)

    def test_missing_rate_fields_returns_empty_rather_than_using_cumulative_totals(
        self, tmp_path: Path
    ) -> None:
        # Older mod snapshot: only the lifetime cumulative totals are
        # present, no *_rates_per_min fields. Falling back to cumulative
        # totals here would silently mislabel a lifetime sum as a per-minute
        # rate, so this must report "no data" (empty) instead of a wrong
        # number.
        write_json(
            tmp_path / "production.json",
            _production_with_rates(
                {
                    "input_counts": {"iron-plate": 12000},
                    "output_counts": {"iron-plate": 3000},
                }
            ),
        )
        gs = GameState(tmp_path, min_refresh_interval=0)
        net = live_state.net_production(gs)
        assert net == {}

    def test_sums_across_surfaces(self, tmp_path: Path) -> None:
        data = _production_with_rates(
            {
                "input_counts": {"iron-plate": 100},
                "output_counts": {},
                "input_rates_per_min": {"iron-plate": 10.0},
                "output_rates_per_min": {},
            }
        )
        data["forces"]["player"]["surfaces"]["vulcanus"] = {
            "items": {
                "input_counts": {"iron-plate": 50},
                "output_counts": {},
                "input_rates_per_min": {"iron-plate": 5.0},
                "output_rates_per_min": {},
            },
            "fluids": {
                "input_counts": {},
                "output_counts": {},
                "input_rates_per_min": {},
                "output_rates_per_min": {},
            },
        }
        write_json(tmp_path / "production.json", data)
        gs = GameState(tmp_path, min_refresh_interval=0)
        net = live_state.net_production(gs)
        assert net["iron-plate"] == pytest.approx(15.0)


class TestBuildingCounts:
    def test_counts_by_name_for_force(self, tmp_path: Path) -> None:
        write_ndjson(
            tmp_path / "buildings.ndjson",
            [
                _add_event(1, "jaw-crusher"),
                _add_event(2, "jaw-crusher"),
                _add_event(3, "stone-furnace", force="enemy"),
            ],
        )
        gs = GameState(tmp_path, min_refresh_interval=0)
        counts = live_state.building_counts(gs, force="player")
        assert counts == {"jaw-crusher": 2}

    def test_removed_entity_excluded(self, tmp_path: Path) -> None:
        write_ndjson(
            tmp_path / "buildings.ndjson",
            [
                _add_event(1, "jaw-crusher"),
                {"t": 2, "op": "remove", "id": 1},
            ],
        )
        gs = GameState(tmp_path, min_refresh_interval=0)
        counts = live_state.building_counts(gs, force="player")
        assert counts == {}

    def test_no_buildings_file_returns_empty(self, tmp_path: Path) -> None:
        gs = GameState(tmp_path, min_refresh_interval=0)
        counts = live_state.building_counts(gs, force="player")
        assert counts == {}


class TestMiningDrillProductivityBonus:
    def test_reads_the_live_value(self, tmp_path: Path) -> None:
        write_json(
            tmp_path / "tech.json",
            {"tick": 1, "forces": {"player": {"mining_drill_productivity_bonus": 0.2}}},
        )
        gs = GameState(tmp_path, min_refresh_interval=0)
        assert live_state.mining_drill_productivity_bonus(gs) == pytest.approx(0.2)

    def test_missing_field_defaults_to_zero(self, tmp_path: Path) -> None:
        # Older mod builds (pre-0.3.2) never write this field at all.
        write_json(tmp_path / "tech.json", {"tick": 1, "forces": {"player": {}}})
        gs = GameState(tmp_path, min_refresh_interval=0)
        assert live_state.mining_drill_productivity_bonus(gs) == 0.0

    def test_no_tech_file_defaults_to_zero(self, tmp_path: Path) -> None:
        gs = GameState(tmp_path, min_refresh_interval=0)
        assert live_state.mining_drill_productivity_bonus(gs) == 0.0

    def test_unknown_force_defaults_to_zero(self, tmp_path: Path) -> None:
        write_json(
            tmp_path / "tech.json",
            {"tick": 1, "forces": {"player": {"mining_drill_productivity_bonus": 0.2}}},
        )
        gs = GameState(tmp_path, min_refresh_interval=0)
        assert live_state.mining_drill_productivity_bonus(gs, force="enemy") == 0.0
