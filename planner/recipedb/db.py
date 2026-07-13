"""Async SQLite database wrapper for the recipe-calculation engine.

Vendored from recipe-mcp's `src/db.py` (see `planner/recipedb/__init__.py`
for provenance). Opens the database read-only with the immutable flag — the
DB is built once by `build_db.py` and never written at runtime.

Uses asyncio.to_thread() to run blocking sqlite3 calls in a thread pool.
Each operation opens a short-lived connection to avoid thread-safety issues.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3

logger = logging.getLogger(__name__)


def _connect(path: str) -> sqlite3.Connection:
    # Open read-only; immutable=1 tells SQLite it will never change (no WAL probing)
    conn = sqlite3.connect(
        f"file:{path}?mode=ro&immutable=1",
        uri=True,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _run_query(path: str, query: str, params: tuple = ()) -> list[dict]:
    conn = _connect(path)
    try:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _run_one(path: str, query: str, params: tuple = ()) -> dict | None:
    conn = _connect(path)
    try:
        row = conn.execute(query, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


class AsyncDatabase:
    """Thin async wrapper over the read-only recipes SQLite database."""

    def __init__(self, path: str) -> None:
        self.path = path

    async def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        return await asyncio.to_thread(_run_query, self.path, query, params)

    async def fetch_one(self, query: str, params: tuple = ()) -> dict | None:
        return await asyncio.to_thread(_run_one, self.path, query, params)

    async def health_check(self) -> bool:
        row = await self.fetch_one("SELECT COUNT(*) AS n FROM recipes")
        return row is not None and row["n"] > 0
