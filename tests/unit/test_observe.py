"""Unit tests for planner.observe's pure functions.

Ported from the former src/server.py MCP tool tests (test_tools.py) — same
GameState-pointed-at-tmp_path fixture pattern, but calling the plain sync
observe.* functions directly instead of async FastMCP tools.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from planner import observe
from src.game_state import GameState

pytestmark = pytest.mark.unit


def _gs(tmp_path: Path) -> GameState:
    return GameState(tmp_path, min_refresh_interval=0)


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


class TestResearchStatus:
    def test_no_data_yet(self, tmp_path: Path) -> None:
        result = observe.research_status(_gs(tmp_path))
        assert "error" in result

    def test_returns_current_research(self, tmp_path: Path) -> None:
        write_json(
            tmp_path / "tech.json",
            {
                "tick": 42,
                "forces": {
                    "player": {
                        "current_research": "automation-2",
                        "research_progress": 0.5,
                        "research_queue": ["automation-2", "logistics-2"],
                        "technologies": {},
                    }
                },
            },
        )
        result = observe.research_status(_gs(tmp_path))
        assert result["current_research"] == "automation-2"
        assert result["research_progress"] == 0.5
        assert result["research_queue"] == ["automation-2", "logistics-2"]

    def test_prefers_research_json_over_stale_tech_json(self, tmp_path: Path) -> None:
        write_json(
            tmp_path / "tech.json",
            {
                "tick": 1,
                "forces": {
                    "player": {
                        "current_research": "automation-2",
                        "research_progress": 0.1,
                        "research_queue": ["automation-2"],
                        "technologies": {},
                    }
                },
            },
        )
        write_json(
            tmp_path / "research.json",
            {
                "tick": 50,
                "forces": {
                    "player": {
                        "current_research": "automation-2",
                        "research_progress": 0.9,
                        "research_queue": ["automation-2"],
                    }
                },
            },
        )
        result = observe.research_status(_gs(tmp_path))
        assert result["research_progress"] == 0.9
        assert result["tick"] == 50

    def test_falls_back_to_tech_json_when_research_json_absent(self, tmp_path: Path) -> None:
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
        result = observe.research_status(_gs(tmp_path))
        assert result["current_research"] == "automation-2"
        assert result["research_progress"] == 0.5


class TestTechTree:
    def test_classifies_researched_available_locked(self, tmp_path: Path) -> None:
        write_json(
            tmp_path / "tech.json",
            {
                "tick": 1,
                "forces": {
                    "player": {
                        "technologies": {
                            "automation": {
                                "researched": True,
                                "enabled": True,
                                "prerequisites": [],
                            },
                            "automation-2": {
                                "researched": False,
                                "enabled": True,
                                "prerequisites": ["automation"],
                            },
                            "automation-3": {
                                "researched": False,
                                "enabled": True,
                                "prerequisites": ["automation-2"],
                            },
                            "cheating": {
                                "researched": False,
                                "enabled": False,
                                "prerequisites": [],
                            },
                        }
                    }
                },
            },
        )
        result = observe.tech_tree(_gs(tmp_path))
        by_name = {t["name"]: t["status"] for t in result["technologies"]}
        assert by_name["automation"] == "researched"
        assert by_name["automation-2"] == "available"  # prereq (automation) researched
        assert by_name["automation-3"] == "locked"  # prereq (automation-2) not researched
        assert by_name["cheating"] == "locked"  # not enabled at all

    def test_status_filter(self, tmp_path: Path) -> None:
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
        result = observe.tech_tree(_gs(tmp_path), status="researched")
        assert result["count"] == 1
        assert result["technologies"][0]["name"] == "a"


class TestProductionStats:
    def test_defaults_to_nauvis(self, tmp_path: Path) -> None:
        write_json(
            tmp_path / "production.json",
            {
                "tick": 1,
                "forces": {
                    "player": {
                        "surfaces": {
                            "nauvis": {
                                "items": {"input_counts": {}, "output_counts": {"iron-plate": 120}},
                                "fluids": {"input_counts": {}, "output_counts": {}},
                            }
                        }
                    }
                },
            },
        )
        result = observe.production_stats(_gs(tmp_path))
        assert result["surface"] == "nauvis"
        assert result["items"]["output_counts"]["iron-plate"] == 120

    def test_unknown_force(self, tmp_path: Path) -> None:
        write_json(tmp_path / "production.json", {"tick": 1, "forces": {}})
        result = observe.production_stats(_gs(tmp_path), force="enemy")
        assert "error" in result

    def test_passes_through_rate_fields_alongside_cumulative_counts(
        self, tmp_path: Path
    ) -> None:
        write_json(
            tmp_path / "production.json",
            {
                "tick": 1,
                "forces": {
                    "player": {
                        "surfaces": {
                            "nauvis": {
                                "items": {
                                    "input_counts": {"iron-plate": 1200},
                                    "output_counts": {"iron-ore": 900},
                                    "input_rates_per_min": {"iron-plate": 120.0},
                                    "output_rates_per_min": {"iron-ore": 90.0},
                                },
                                "fluids": {"input_counts": {}, "output_counts": {}},
                            }
                        }
                    }
                },
            },
        )
        result = observe.production_stats(_gs(tmp_path))
        assert result["items"]["input_counts"]["iron-plate"] == 1200
        assert result["items"]["input_rates_per_min"]["iron-plate"] == 120.0


class TestLogistics:
    def test_no_data_yet(self, tmp_path: Path) -> None:
        result = observe.logistics(_gs(tmp_path))
        assert "error" in result

    def test_returns_networks_for_force(self, tmp_path: Path) -> None:
        write_json(
            tmp_path / "logistics.json",
            {
                "tick": 1,
                "forces": {
                    "player": [
                        {"network_id": 1, "surface": "nauvis", "contents": []},
                        {"network_id": 2, "surface": "vulcanus", "contents": []},
                    ]
                },
            },
        )
        result = observe.logistics(_gs(tmp_path))
        assert result["network_count"] == 2

    def test_filters_by_surface(self, tmp_path: Path) -> None:
        write_json(
            tmp_path / "logistics.json",
            {
                "tick": 1,
                "forces": {
                    "player": [
                        {"network_id": 1, "surface": "nauvis", "contents": []},
                        {"network_id": 2, "surface": "vulcanus", "contents": []},
                    ]
                },
            },
        )
        result = observe.logistics(_gs(tmp_path), surface="vulcanus")
        assert result["network_count"] == 1
        assert result["networks"][0]["network_id"] == 2

    def test_unknown_force(self, tmp_path: Path) -> None:
        write_json(tmp_path / "logistics.json", {"tick": 1, "forces": {"player": []}})
        result = observe.logistics(_gs(tmp_path), force="enemy")
        assert "error" in result
        assert result["available_forces"] == ["player"]


class TestPlayerInventory:
    def test_no_data_yet(self, tmp_path: Path) -> None:
        result = observe.player_inventory(_gs(tmp_path))
        assert "error" in result

    def test_no_players_connected(self, tmp_path: Path) -> None:
        write_json(tmp_path / "inventories.json", {"tick": 1, "players": {}})
        result = observe.player_inventory(_gs(tmp_path))
        assert "error" in result
        assert "hint" in result

    def test_single_connected_player_used_by_default(self, tmp_path: Path) -> None:
        write_json(
            tmp_path / "inventories.json",
            {
                "tick": 1,
                "players": {
                    "jhjaggars": {
                        "contents": [{"name": "iron-plate", "quality": "normal", "count": 8}],
                        "force": "player",
                    }
                },
            },
        )
        result = observe.player_inventory(_gs(tmp_path))
        assert result["player"] == "jhjaggars"
        assert result["contents"][0]["name"] == "iron-plate"

    def test_multiple_players_requires_explicit_name(self, tmp_path: Path) -> None:
        write_json(
            tmp_path / "inventories.json",
            {
                "tick": 1,
                "players": {
                    "alice": {"contents": [], "force": "player"},
                    "bob": {"contents": [], "force": "player"},
                },
            },
        )
        result = observe.player_inventory(_gs(tmp_path))
        assert "error" in result
        assert sorted(result["connected_players"]) == ["alice", "bob"]

        named = observe.player_inventory(_gs(tmp_path), player="bob")
        assert named["player"] == "bob"

    def test_unknown_player_name(self, tmp_path: Path) -> None:
        write_json(
            tmp_path / "inventories.json",
            {"tick": 1, "players": {"alice": {"contents": [], "force": "player"}}},
        )
        result = observe.player_inventory(_gs(tmp_path), player="nobody")
        assert "error" in result
        assert result["connected_players"] == ["alice"]


class TestBuildingsFunctions:
    def test_building_counts_empty(self, tmp_path: Path) -> None:
        result = observe.building_counts(_gs(tmp_path))
        assert result["total"] == 0
        assert "hint" in result  # no data at all -- still points at the setting

    def test_building_counts_force_filter_matches_nothing(self, tmp_path: Path) -> None:
        # Buildings exist, just not for the requested force -- distinct from
        # "the mod isn't exporting buildings at all", so no misleading hint.
        gs = _gs(tmp_path)
        events_path = tmp_path / "buildings.ndjson"
        with events_path.open("w", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "t": 1,
                        "op": "add",
                        "entity": {
                            "id": 1,
                            "name": "stone-furnace",
                            "type": "furnace",
                            "surface": "nauvis",
                            "force": "player",
                            "position": {"x": 0, "y": 0},
                        },
                    }
                )
                + "\n"
            )
        gs.refresh(force=True)

        result = observe.building_counts(gs, force="enemy")
        assert result["total"] == 0
        assert "hint" not in result
        assert result["available_forces"] == ["player"]

    def test_query_and_count_buildings(self, tmp_path: Path) -> None:
        gs = _gs(tmp_path)
        events_path = tmp_path / "buildings.ndjson"
        with events_path.open("w", encoding="utf-8") as f:
            for i, (name, typ) in enumerate(
                [
                    ("assembling-machine-2", "assembling-machine"),
                    ("assembling-machine-2", "assembling-machine"),
                    ("stone-furnace", "furnace"),
                ]
            ):
                f.write(
                    json.dumps(
                        {
                            "t": i,
                            "op": "add",
                            "entity": {
                                "id": i,
                                "name": name,
                                "type": typ,
                                "surface": "nauvis",
                                "force": "player",
                                "position": {"x": i, "y": 0},
                            },
                        }
                    )
                    + "\n"
                )
        gs.refresh(force=True)

        counts = observe.building_counts(gs)
        assert counts["total"] == 3
        assert counts["by_name"]["assembling-machine-2"] == 2
        assert counts["by_type"]["furnace"] == 1

        query = observe.query_buildings(gs, name="stone-furnace")
        assert query["total_matches"] == 1
        assert query["results"][0]["position"] == {"x": 2, "y": 0}

    def test_query_buildings_limit_is_clamped(self, tmp_path: Path) -> None:
        result = observe.query_buildings(_gs(tmp_path), limit=99999)
        # no data yet, but limit clamping itself shouldn't raise
        assert result["results"] == []
