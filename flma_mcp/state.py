"""Process-wide GameState for the flma MCP server.

A one-shot CLI process constructs a fresh `GameState` per invocation (see
`planner/live_state.open_game_state`) because there's nothing to reuse it
across. A long-lived server is the opposite: it must hold ONE GameState open
for its whole lifetime and refresh it in place, or every tool call pays a
cold reload of `buildings.ndjson` (tens of MB on a large save — see
SCHEMA.md) plus a fresh SnapshotFile parse of everything else.

This module owns that one shared instance and opts `planner.live_state`'s
`open_game_state()` into using it (see `use_shared_game_state`), so every
planning-command handler `flma_mcp.cli_bridge.run_cli` drives -- all of
which call `open_game_state()` themselves, unmodified -- transparently reuses
it too instead of constructing their own.
"""

from __future__ import annotations

import asyncio
from typing import Any

from planner import live_state
from src.game_state import GameState

from flma_mcp import config

_gs: GameState | None = None


def gs() -> GameState:
    """The shared GameState. `init()` must have run first (server.py's
    `main()` calls it before starting the transport)."""
    if _gs is None:
        raise RuntimeError("flma_mcp.state.init() has not been called yet")
    return _gs


def init() -> GameState:
    """Construct the shared GameState and do its first (cold) load
    synchronously, before the server starts accepting requests -- so that
    cost never lands on an event-loop request handler. Idempotent: safe to
    call more than once (e.g. from tests), returns the same instance."""
    global _gs
    if _gs is None:
        _gs = GameState(config.SCRIPT_OUTPUT_DIR)
        _gs.refresh(force=True)
        live_state.use_shared_game_state(_gs)
    return _gs


async def warm() -> None:
    """Refresh the shared GameState off the event loop. Every tool awaits
    this before doing anything else (see live_tools.py/planning_tools.py) --
    it's what makes a post-compaction `buildings.ndjson` replay (another
    cold-load-sized cost, see src/game_state.py's BuildingIndex.refresh)
    happen in a worker thread instead of blocking the loop mid-request. By
    the time a planning handler's own synchronous `open_game_state()` call
    runs, this has already made that refresh a cheap no-op (a handful of
    stat() calls)."""
    await asyncio.to_thread(gs().refresh, True)


def freshness() -> dict[str, Any]:
    """The envelope attached to every tool result (live and planning) so a
    remote caller -- which cannot see this desktop -- knows whether what
    it's looking at is current. Computed from the freshest of the
    ~5-second-cadence snapshots (production/research); tech.json is
    event-driven (only updated on research changes) so it's a poor signal
    for "is the game currently running" and is deliberately excluded here."""
    ages = gs().snapshot_ages()
    candidates = [a for a in (ages.get("production"), ages.get("research")) if a is not None]
    age = min(candidates) if candidates else None
    stale = age is None or age > config.STALE_AFTER_SECONDS
    game_running = age is not None and not stale
    note = None
    if stale:
        if age is None:
            note = (
                "No live snapshot has ever been seen under "
                f"{config.SCRIPT_OUTPUT_DIR} -- Factorio may not be running, or the "
                "flma mod's 'flma-export-enabled' map setting may be off. Recipe "
                "and machine-count math from recipes.db is unaffected."
            )
        else:
            note = (
                f"Factorio does not appear to be running (last production/research "
                f"snapshot is {_format_age(age)} old). Live production/logistics/"
                "inventory numbers are historical, not current. Recipe and "
                "machine-count math from recipes.db is unaffected."
            )
    return {
        "age_seconds": age,
        "stale": stale,
        "game_running": game_running,
        "save_id": getattr(gs(), "_active_save_id", None),
        "note": note,
    }


def _format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f}m"
    return f"{minutes / 60:.1f}h"


def reset_for_tests() -> None:
    """Test-only: undo `init()` so each test gets an isolated GameState
    bound to its own tmp_path instead of silently reusing whatever a
    previous test initialized."""
    global _gs
    _gs = None
    live_state.reset_shared_game_state()


__all__ = ["gs", "init", "warm", "freshness", "reset_for_tests"]
