"""Async SQLite database wrapper for the recipe-calculation engine.

Vendored from recipe-mcp's `src/db.py` (see `planner/recipedb/__init__.py`
for provenance). Opens the database read-only with the immutable flag — the
DB is built once by `build_db.py` and never written at runtime.

Uses asyncio.to_thread() to run blocking sqlite3 calls in a thread pool.
Connections are cached per (thread, path) rather than opened and closed on
every call — see `_conn_for` — since a one-shot CLI invocation only ever
issues a handful of queries but a long-lived server (flma_mcp) can issue
hundreds per request (a deep recipe-chain `expand`/`recommend`), and
`asyncio.to_thread`'s bounded worker pool means "per thread" caps the
connection count regardless of request volume. Safe specifically because
the DB is opened `immutable=1`: no writer, no WAL, nothing to invalidate.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading

logger = logging.getLogger(__name__)

_local = threading.local()


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


def _conn_for(path: str) -> sqlite3.Connection:
    """Return this thread's cached connection to `path`, opening one on
    first use. `immutable=1` means the underlying file is never expected to
    change out from under a cached connection during the process's
    lifetime -- if you `build-db` again while a long-lived caller (e.g.
    flma_mcp) is still running, restart it rather than expecting this to
    pick up the new file."""
    conns: dict[str, sqlite3.Connection] = getattr(_local, "conns", None)
    if conns is None:
        conns = {}
        _local.conns = conns
    conn = conns.get(path)
    if conn is None:
        conn = _connect(path)
        conns[path] = conn
    return conn


def _run_query(path: str, query: str, params: tuple = ()) -> list[dict]:
    conn = _conn_for(path)
    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _run_one(path: str, query: str, params: tuple = ()) -> dict | None:
    conn = _conn_for(path)
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


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
