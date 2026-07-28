"""Unit tests for flma_mcp/live_tools.py and flma_mcp/state.py.

These test the underlying async functions directly rather than going
through a real FastMCP `mcp.tool()` registration -- `register(mcp)` just
wires each function to a decorator, so exercising the functions themselves
covers the actual behavior (freshness envelope, staleness/no-data handling)
without needing the `mcp` package installed. Nothing in this file imports
`mcp` either, matching cli_bridge/state/live_tools themselves."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from flma_mcp import live_tools, state
from planner import config as planner_config
from planner import live_state
from src.game_state import GameState

pytestmark = pytest.mark.unit


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolated_state():
    # state._gs is a module-global the real server sets once at startup via
    # state.init(); tests bind it directly to a tmp_path GameState instead
    # so each test gets its own isolated snapshot directory.
    yield
    state.reset_for_tests()


def bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, min_refresh_interval: float = 0
) -> GameState:
    gs = GameState(tmp_path, min_refresh_interval=min_refresh_interval)
    monkeypatch.setattr(state, "_gs", gs)
    return gs


class TestFreshness:
    async def test_no_snapshot_ever_seen_is_stale_with_no_age(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bind(tmp_path, monkeypatch)
        fresh = state.freshness()
        assert fresh["age_seconds"] is None
        assert fresh["stale"] is True
        assert fresh["game_running"] is False
        assert fresh["note"] is not None

    async def test_recent_snapshot_is_not_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_json(tmp_path / "production.json", {"tick": 1, "forces": {}})
        gs = bind(tmp_path, monkeypatch)
        gs.refresh(force=True)
        fresh = state.freshness()
        assert fresh["stale"] is False
        assert fresh["game_running"] is True
        assert fresh["note"] is None

    async def test_old_snapshot_is_stale_with_an_actionable_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_json(tmp_path / "production.json", {"tick": 1, "forces": {}})
        gs = bind(tmp_path, monkeypatch)
        gs.refresh(force=True)
        # Simplest way to force staleness without sleeping: shrink the
        # threshold flma_mcp.config actually reads (state.py does
        # `from flma_mcp import config`, a module reference, so patching the
        # attribute on that module object is visible to state.freshness()).
        import flma_mcp.config as mcp_config

        monkeypatch.setattr(mcp_config, "STALE_AFTER_SECONDS", -1)
        fresh = state.freshness()
        assert fresh["stale"] is True
        assert fresh["game_running"] is False
        assert "does not appear to be running" in fresh["note"]


class TestObserveWrapper:
    async def test_wraps_observe_result_with_freshness(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_json(
            tmp_path / "research.json",
            {"tick": 1, "forces": {"player": {"current_research": "automation"}}},
        )
        bind(tmp_path, monkeypatch)

        from planner import observe

        result = await live_tools._observe(observe.research_status, force="player")
        assert result["current_research"] == "automation"
        assert "freshness" in result
        assert "age_seconds" in result["freshness"]

    async def test_no_data_hint_survives_the_wrapper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bind(tmp_path, monkeypatch)
        from planner import observe

        result = await live_tools._observe(observe.research_status, force="player")
        assert "error" in result
        assert "hint" in result
        assert "freshness" in result  # never dropped, even on the error path


class TestStatusPayload:
    async def test_reports_snapshot_ages_and_active_save(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Point RECIPES_DB at a path that doesn't exist -- otherwise this
        # test would silently pick up whatever real recipes.db happens to
        # be built on the machine running the tests.
        monkeypatch.setattr(planner_config, "RECIPES_DB", tmp_path / "no-such-recipes.db")
        write_json(tmp_path / "research.json", {"tick": 1, "forces": {}})
        gs = bind(tmp_path, monkeypatch)
        gs.refresh(force=True)
        await state.warm()

        import asyncio

        payload = await asyncio.to_thread(live_tools._status_payload)
        assert payload["script_output_dir"] == str(tmp_path)
        assert "research" in payload["snapshot_ages_seconds"]
        assert payload["recipes_db"]["exists"] is False
        assert "modpack_alignment" not in payload  # no recipes.db -> can't compute this

    async def test_reports_modpack_alignment_when_recipes_db_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sqlite3

        db_path = tmp_path / "recipes.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE technologies (name TEXT)")
        conn.executemany("INSERT INTO technologies VALUES (?)", [("automation",), ("logistics",)])
        conn.commit()
        conn.close()
        monkeypatch.setattr(planner_config, "RECIPES_DB", db_path)

        write_json(
            tmp_path / "tech.json",
            {
                "tick": 1,
                "forces": {
                    "player": {
                        "technologies": {
                            "automation": {"researched": True},
                            "logistics": {"researched": False},
                        }
                    }
                },
            },
        )
        gs = bind(tmp_path, monkeypatch)
        gs.refresh(force=True)
        await state.warm()

        import asyncio

        payload = await asyncio.to_thread(live_tools._status_payload)
        assert payload["recipes_db"]["exists"] is True
        assert payload["modpack_alignment"]["aligned"] is True
        assert payload["modpack_alignment"]["overlap_count"] == 2


class TestSharedGameStateWiring:
    def test_init_binds_live_state_open_game_state_to_the_shared_instance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import flma_mcp.config as mcp_config

        monkeypatch.setattr(mcp_config, "SCRIPT_OUTPUT_DIR", tmp_path)
        gs = state.init()
        assert live_state.open_game_state(tmp_path) is gs
