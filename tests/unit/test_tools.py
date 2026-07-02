"""Unit tests for the MCP tool functions in src.server.

FastMCP tools are plain async functions under the @mcp.tool() decorator, so
they can be called directly (mirroring apps/recipe-mcp's test_tools.py
pattern). We swap the module-level `state` for a GameState pointed at a
tmp_path with hand-written fixture files, so no real Factorio process is
needed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src import server
from src.game_state import GameState

pytestmark = pytest.mark.unit


def _use_state(tmp_path: Path) -> GameState:
    gs = GameState(tmp_path, min_refresh_interval=0)
    server.state = gs
    return gs


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


class TestGetResearchStatus:
    async def test_no_data_yet(self, tmp_path: Path) -> None:
        _use_state(tmp_path)
        result = await server.get_research_status()
        assert "error" in result

    async def test_returns_current_research(self, tmp_path: Path) -> None:
        _use_state(tmp_path)
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
        result = await server.get_research_status()
        assert result["current_research"] == "automation-2"
        assert result["research_progress"] == 0.5
        assert result["research_queue"] == ["automation-2", "logistics-2"]

    async def test_prefers_research_json_over_stale_tech_json(self, tmp_path: Path) -> None:
        # research.json is refreshed every tick-interval cycle so
        # research_progress stays live; tech.json only updates on research
        # events, so it can be stale mid-research. When both exist, the live
        # one wins.
        _use_state(tmp_path)
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
        result = await server.get_research_status()
        assert result["research_progress"] == 0.9
        assert result["tick"] == 50

    async def test_falls_back_to_tech_json_when_research_json_absent(
        self, tmp_path: Path
    ) -> None:
        _use_state(tmp_path)
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
        result = await server.get_research_status()
        assert result["current_research"] == "automation-2"
        assert result["research_progress"] == 0.5


class TestGetTechTree:
    async def test_classifies_researched_available_locked(self, tmp_path: Path) -> None:
        _use_state(tmp_path)
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
        result = await server.get_tech_tree()
        by_name = {t["name"]: t["status"] for t in result["technologies"]}
        assert by_name["automation"] == "researched"
        assert by_name["automation-2"] == "available"  # prereq (automation) researched
        assert by_name["automation-3"] == "locked"  # prereq (automation-2) not researched
        assert by_name["cheating"] == "locked"  # not enabled at all

    async def test_status_filter(self, tmp_path: Path) -> None:
        _use_state(tmp_path)
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
        result = await server.get_tech_tree(status="researched")
        assert result["count"] == 1
        assert result["technologies"][0]["name"] == "a"


class TestGetProductionStats:
    async def test_defaults_to_nauvis(self, tmp_path: Path) -> None:
        _use_state(tmp_path)
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
        result = await server.get_production_stats()
        assert result["surface"] == "nauvis"
        assert result["items"]["output_counts"]["iron-plate"] == 120

    async def test_unknown_force(self, tmp_path: Path) -> None:
        _use_state(tmp_path)
        write_json(tmp_path / "production.json", {"tick": 1, "forces": {}})
        result = await server.get_production_stats(force="enemy")
        assert "error" in result

    async def test_passes_through_rate_fields_alongside_cumulative_counts(
        self, tmp_path: Path
    ) -> None:
        _use_state(tmp_path)
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
        result = await server.get_production_stats()
        assert result["items"]["input_counts"]["iron-plate"] == 1200
        assert result["items"]["input_rates_per_min"]["iron-plate"] == 120.0


class TestBuildingsTools:
    async def test_get_building_counts_empty(self, tmp_path: Path) -> None:
        _use_state(tmp_path)
        result = await server.get_building_counts()
        assert result["total"] == 0
        assert "hint" in result  # no data at all -- still points at the setting

    async def test_get_building_counts_force_filter_matches_nothing(
        self, tmp_path: Path
    ) -> None:
        # Buildings exist, just not for the requested force -- this is a
        # different situation from "the mod isn't exporting buildings at
        # all", so the misleading "enable the setting" hint shouldn't appear.
        gs = _use_state(tmp_path)
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

        result = await server.get_building_counts(force="enemy")
        assert result["total"] == 0
        assert "hint" not in result
        assert result["available_forces"] == ["player"]

    async def test_query_and_count_buildings(self, tmp_path: Path) -> None:
        gs = _use_state(tmp_path)
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

        counts = await server.get_building_counts()
        assert counts["total"] == 3
        assert counts["by_name"]["assembling-machine-2"] == 2
        assert counts["by_type"]["furnace"] == 1

        query = await server.query_buildings(name="stone-furnace")
        assert query["total_matches"] == 1
        assert query["results"][0]["position"] == {"x": 2, "y": 0}


class TestGetSnapshotAge:
    async def test_reports_dir_and_ages(self, tmp_path: Path) -> None:
        _use_state(tmp_path)
        write_json(tmp_path / "tech.json", {"tick": 1})
        result = await server.get_snapshot_age()
        assert result["script_output_dir"] == str(tmp_path)
        assert result["age_seconds"]["tech"] is not None
        assert result["age_seconds"]["production"] is None
        assert "research" in result["age_seconds"]
        assert "buildings" in result["age_seconds"]

    async def test_reports_buildings_and_research_ages(self, tmp_path: Path) -> None:
        gs = _use_state(tmp_path)
        write_json(tmp_path / "research.json", {"tick": 1, "forces": {}})
        with (tmp_path / "buildings.ndjson").open("w", encoding="utf-8") as f:
            f.write(
                json.dumps({"t": 1, "op": "add", "entity": {"id": 1, "name": "a"}}) + "\n"
            )
        gs.refresh(force=True)
        result = await server.get_snapshot_age()
        assert result["age_seconds"]["research"] is not None
        assert result["age_seconds"]["buildings"] is not None
