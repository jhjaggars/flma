"""In-memory world model folded from the flma mod's script-output files.

The mod writes small, engine-aggregated full snapshots (tech.json,
production.json, logistics.json, inventories.json) that are cheap to just
re-read and replace in memory, plus an append-only NDJSON event log
(buildings.ndjson) for the one dataset whose baseline is proportional to base
size. This module owns:

  - SnapshotFile: lazy re-read of one JSON snapshot, cached by mtime+size so a
    burst of tool calls doesn't re-parse on every call.
  - BuildingIndex: tails buildings.ndjson, folding add/remove events into a
    dict keyed by unit_number. Detects mod-side compaction (the file shrinking
    below the last-read offset) and replays from scratch in that case.
  - GameState: composes the above and is what src/server.py's tools query.

All file I/O here is synchronous; src/server.py wraps calls in
asyncio.to_thread(), mirroring the AsyncDatabase pattern in apps/recipe-mcp.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SnapshotFile:
    """A single full-overwrite JSON snapshot, re-read only when it changes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._mtime: float | None = None
        self._size: int | None = None
        self._data: dict[str, Any] = {}

    def read(self) -> dict[str, Any]:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            # Keep the last-good value rather than wiping it — the mod writes
            # via truncate-then-write (not atomic rename), so a momentary
            # disappearance is indistinguishable from "genuinely never
            # written" and from a torn write; both should fall back to
            # whatever we last parsed successfully.
            return self._data

        if stat.st_mtime == self._mtime and stat.st_size == self._size:
            return self._data

        try:
            text = self.path.read_text(encoding="utf-8")
            self._data = json.loads(text) if text else {}
        except (OSError, json.JSONDecodeError) as exc:
            # A snapshot can be read mid-write; keep the last-good value and
            # log rather than raising — the next poll will pick up the retry.
            logger.warning("failed to read snapshot %s: %s", self.path, exc)
            return self._data

        self._mtime = stat.st_mtime
        self._size = stat.st_size
        return self._data

    def age_seconds(self) -> float | None:
        if self._mtime is None:
            return None
        return time.time() - self._mtime

    @property
    def cached(self) -> dict[str, Any]:
        """Last-parsed value without touching disk — use this from GameState
        query methods so GameState.min_refresh_interval actually bounds disk
        I/O; calling read() again here would re-check mtime unconditionally
        and defeat the throttle."""
        return self._data


class BuildingIndex:
    """Folds the buildings.ndjson add/remove event log into a live index.

    Tracks a byte offset into the file. If the file has shrunk since the last
    read, the mod compacted it (rewrote it from its own in-memory registry) —
    in that case we reset the index and replay from the start.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._buildings: dict[int, dict[str, Any]] = {}
        self._last_tick = 0

    def refresh(self) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return

        if size < self._offset:
            # Compacted (or truncated) — replay from scratch.
            self._offset = 0
            self._buildings = {}

        if size == self._offset:
            return

        # Read in binary and split manually rather than iterating a text-mode
        # file: f.tell() is unreliable after iterating with `for line in f`
        # (buffered read-ahead disables it), so byte offsets have to be
        # tracked by hand instead.
        with self.path.open("rb") as f:
            f.seek(self._offset)
            chunk = f.read()

        lines = chunk.split(b"\n")
        partial = lines[-1]  # bytes after the last newline; b"" if chunk ended in \n
        complete_lines = lines[:-1]
        consumed = len(chunk) - len(partial)

        for raw_line in complete_lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("skipping corrupt line in %s: %s", self.path, exc)
                continue
            self._apply(event)

        self._offset += consumed

    def _apply(self, event: dict[str, Any]) -> None:
        self._last_tick = max(self._last_tick, event.get("t", 0))
        op = event.get("op")
        if op == "add":
            entity = event.get("entity") or {}
            eid = entity.get("id")
            if eid is not None:
                self._buildings[eid] = entity
        elif op == "remove":
            self._buildings.pop(event.get("id"), None)

    def all(self) -> list[dict[str, Any]]:
        return list(self._buildings.values())

    @property
    def last_tick(self) -> int:
        return self._last_tick


class GameState:
    """Composes the tech/production/logistics/inventory snapshots and the
    building index into the query surface used by MCP tools."""

    def __init__(self, script_output_dir: Path, min_refresh_interval: float = 0.5) -> None:
        self.dir = script_output_dir
        self.min_refresh_interval = min_refresh_interval
        self._last_refresh = 0.0

        self.tech = SnapshotFile(script_output_dir / "tech.json")
        self.production = SnapshotFile(script_output_dir / "production.json")
        self.logistics = SnapshotFile(script_output_dir / "logistics.json")
        self.inventories = SnapshotFile(script_output_dir / "inventories.json")
        self.buildings = BuildingIndex(script_output_dir / "buildings.ndjson")

    def refresh(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_refresh) < self.min_refresh_interval:
            return
        self._last_refresh = now
        # SnapshotFile.read() is itself cheap when unchanged (mtime/size check).
        self.tech.read()
        self.production.read()
        self.logistics.read()
        self.inventories.read()
        self.buildings.refresh()

    def health_check(self) -> bool:
        return self.dir.exists()

    # -- query helpers used by server.py tools --------------------------------

    def get_tech(self) -> dict[str, Any]:
        self.refresh()
        return self.tech.cached

    def get_production(self) -> dict[str, Any]:
        self.refresh()
        return self.production.cached

    def get_logistics(self) -> dict[str, Any]:
        self.refresh()
        return self.logistics.cached

    def get_inventories(self) -> dict[str, Any]:
        self.refresh()
        return self.inventories.cached

    def get_buildings(self) -> list[dict[str, Any]]:
        self.refresh()
        return self.buildings.all()

    def snapshot_ages(self) -> dict[str, float | None]:
        self.refresh()
        return {
            "tech": self.tech.age_seconds(),
            "production": self.production.age_seconds(),
            "logistics": self.logistics.age_seconds(),
            "inventories": self.inventories.age_seconds(),
        }
