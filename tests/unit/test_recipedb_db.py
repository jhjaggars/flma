"""Unit tests for planner/recipedb/db.py's connection caching:
`_conn_for` must reuse one connection per (thread, path) instead of
open/close-per-call, since a long-lived caller (flma_mcp) can issue hundreds
of queries per request where a one-shot CLI invocation issues a handful."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from planner.recipedb import db as recipedb_db
from planner.recipedb.db import AsyncDatabase, _conn_for, _run_one, _run_query

pytestmark = pytest.mark.unit


def make_db(path: Path) -> str:
    db_path = str(path / "recipes.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany(
        "INSERT INTO widgets (id, name) VALUES (?, ?)",
        [(1, "iron-gear-wheel"), (2, "copper-cable")],
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture(autouse=True)
def _clear_thread_local_cache():
    # The cache is a module-level threading.local() -- clear it before and
    # after each test so one test's cached connection to a tmp_path db
    # (deleted at teardown) can't leak into the next.
    if hasattr(recipedb_db._local, "conns"):
        for conn in recipedb_db._local.conns.values():
            conn.close()
        del recipedb_db._local.conns
    yield
    if hasattr(recipedb_db._local, "conns"):
        for conn in recipedb_db._local.conns.values():
            conn.close()
        del recipedb_db._local.conns


class TestConnFor:
    def test_returns_a_working_connection(self, tmp_path: Path) -> None:
        db_path = make_db(tmp_path)
        conn = _conn_for(db_path)
        rows = conn.execute("SELECT name FROM widgets ORDER BY id").fetchall()
        assert [r["name"] for r in rows] == ["iron-gear-wheel", "copper-cable"]

    def test_same_path_returns_the_same_cached_connection(self, tmp_path: Path) -> None:
        db_path = make_db(tmp_path)
        first = _conn_for(db_path)
        second = _conn_for(db_path)
        assert first is second

    def test_different_paths_get_different_connections(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        path_a = make_db(tmp_path / "a")
        path_b = make_db(tmp_path / "b")
        assert _conn_for(path_a) is not _conn_for(path_b)


class TestRunQueryAndRunOne:
    def test_run_query_returns_dict_rows(self, tmp_path: Path) -> None:
        db_path = make_db(tmp_path)
        rows = _run_query(db_path, "SELECT * FROM widgets ORDER BY id")
        assert rows == [
            {"id": 1, "name": "iron-gear-wheel"},
            {"id": 2, "name": "copper-cable"},
        ]

    def test_run_one_returns_a_single_dict_or_none(self, tmp_path: Path) -> None:
        db_path = make_db(tmp_path)
        row = _run_one(db_path, "SELECT * FROM widgets WHERE id = ?", (2,))
        assert row == {"id": 2, "name": "copper-cable"}
        assert _run_one(db_path, "SELECT * FROM widgets WHERE id = ?", (999,)) is None

    def test_reuses_the_cached_connection_across_calls(self, tmp_path: Path) -> None:
        db_path = make_db(tmp_path)
        _run_query(db_path, "SELECT * FROM widgets")
        conn_after_first_call = recipedb_db._local.conns[db_path]
        _run_query(db_path, "SELECT * FROM widgets")
        assert recipedb_db._local.conns[db_path] is conn_after_first_call


class TestAsyncDatabase:
    async def test_fetch_all_and_fetch_one(self, tmp_path: Path) -> None:
        adb = AsyncDatabase(make_db(tmp_path))
        rows = await adb.fetch_all("SELECT * FROM widgets ORDER BY id")
        assert len(rows) == 2
        row = await adb.fetch_one("SELECT * FROM widgets WHERE id = ?", (1,))
        assert row is not None
        assert row["name"] == "iron-gear-wheel"

    async def test_health_check_true_when_recipes_table_has_rows(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "recipes.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE recipes (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO recipes (id) VALUES (1)")
        conn.commit()
        conn.close()
        assert await AsyncDatabase(db_path).health_check() is True

    async def test_health_check_false_when_recipes_table_empty(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "recipes.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE recipes (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        assert await AsyncDatabase(db_path).health_check() is False
