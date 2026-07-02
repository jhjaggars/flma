"""In-memory world model folded from the flma mod's script-output files.

The mod writes small, engine-aggregated full snapshots (tech.json,
research.json, production.json, logistics.json, inventories.json) that are
cheap to just re-read and replace in memory, plus an append-only NDJSON event
log (buildings.ndjson) for the one dataset whose baseline is proportional to
base size. This module owns:

  - SnapshotFile: lazy re-read of one JSON snapshot, cached by mtime+size so a
    burst of tool calls doesn't re-parse on every call.
  - BuildingIndex: tails buildings.ndjson, folding add/remove events into a
    dict keyed by unit_number. Detects mod-side compaction — the file
    shrinking below the last-read offset, or (since a same-or-larger rewrite
    would otherwise be missed) its leading-bytes fingerprint changing — and
    replays from scratch in that case.
  - GameState: composes the above and is what src/server.py's tools query.
    Guards all reads/writes of its own state with one coarse lock, since
    src/server.py calls it from concurrent request handlers. Also resolves
    which per-save subdirectory is currently active (see below) before every
    refresh, so it follows the mod across save/server switches without the
    operator having to reconfigure SCRIPT_OUTPUT_DIR.

All file I/O here is synchronous; src/server.py wraps calls in
asyncio.to_thread(), mirroring the AsyncDatabase pattern in apps/recipe-mcp.

## Per-save directories

Since mod version 0.3.1, every data file lives under a `<save_id>` subdirectory
of the configured directory (`flma/<save_id>/tech.json`, etc.) rather than
directly in it — this stops switching which save/server is running from
silently mixing or clobbering a different save's files (see `SCHEMA.md`).
GameState is still constructed with the *parent* directory and resolves the
active `<save_id>` itself by reading the small `current-save.json` pointer the
mod maintains there; if that pointer is absent (mod not enabled yet, or an
older mod version), it falls back to treating the given directory as the data
directory directly. The resolved subdirectory is re-checked on every
`refresh()` (throttled like everything else here), so a save switch while the
bridge keeps running is picked up without a restart.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Number of leading bytes of buildings.ndjson fingerprinted to detect
# mod-side compaction — see BuildingIndex.refresh().
_FINGERPRINT_BYTES = 256


def _read_current_save_id(base_dir: Path) -> str | None:
    """Reads the mod's current-save.json pointer, if present.

    Returns None (not an exception) for "no pointer yet" (mod never enabled),
    a torn read (truncate-then-write, same as every other file here), or a
    malformed/missing save_id — all of which should fall back to treating
    base_dir itself as the data directory rather than raising.
    """
    try:
        data = json.loads((base_dir / "current-save.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    save_id = data.get("save_id")
    return save_id if isinstance(save_id, str) and save_id else None


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

    Size alone can miss a compaction: if the mod rewrites the file to a size
    at or above our current offset (e.g. a lot of churn compacted down but
    still bigger than what we'd already consumed), a size-only check would
    resume reading mid-file at the old offset and silently skip events that
    are actually earlier in the new file. To catch that, we also fingerprint
    the first `_FINGERPRINT_BYTES` of the file; every compacted file starts
    with a fresh `{"t":<tick>,...}` record (the mod truncates then rewrites
    from its own in-memory registry — see mod/control.lua compact_buildings),
    so the leading bytes reliably change on compaction even when size doesn't
    shrink below our offset.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._buildings: dict[int, dict[str, Any]] = {}
        self._last_tick = 0
        self._fingerprint: bytes | None = None
        self._mtime: float | None = None

    def _read_fingerprint(self) -> bytes:
        try:
            with self.path.open("rb") as f:
                return f.read(_FINGERPRINT_BYTES)
        except OSError:
            return b""

    def refresh(self) -> None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return
        size = stat.st_size

        fingerprint = self._read_fingerprint()
        compacted = size < self._offset or (
            self._fingerprint is not None and fingerprint != self._fingerprint
        )
        if compacted:
            # Compacted (or truncated) — replay from scratch.
            self._offset = 0
            self._buildings = {}
        self._fingerprint = fingerprint
        self._mtime = stat.st_mtime

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

    def age_seconds(self) -> float | None:
        if self._mtime is None:
            return None
        return time.time() - self._mtime


class GameState:
    """Composes the tech/production/logistics/inventory snapshots and the
    building index into the query surface used by MCP tools.

    Not thread-safe on its own, but src/server.py calls it from concurrent
    request handlers via asyncio.to_thread — so every method here that reads
    or mutates instance state (refresh(), and the getters, since they call
    refresh() and then read the just-refreshed state) takes `self._lock`.
    Worst race this guards against: two threads both pass the
    min_refresh_interval throttle in refresh() and both call
    BuildingIndex.refresh(), which would double-advance its byte offset and
    permanently skip whatever fell in the gap. One coarse `RLock` (reentrant,
    so refresh() can be called from within an already-locked getter without
    deadlocking) is simple and plenty at this scale — this is polled by a
    handful of MCP tool calls, not a hot path.
    """

    def __init__(self, script_output_dir: Path, min_refresh_interval: float = 0.5) -> None:
        self.base_dir = script_output_dir
        self.min_refresh_interval = min_refresh_interval
        self._last_refresh = 0.0
        self._lock = threading.RLock()
        # None means "bound directly to base_dir" (no current-save.json seen
        # yet) — matches _read_current_save_id's own "no pointer" return, so
        # the first _resolve_active_dir() call only rebinds when a real
        # save_id actually appears.
        self._active_save_id: str | None = None
        self._bind(script_output_dir)

    def _bind(self, data_dir: Path) -> None:
        self.dir = data_dir
        self.tech = SnapshotFile(data_dir / "tech.json")
        self.production = SnapshotFile(data_dir / "production.json")
        self.logistics = SnapshotFile(data_dir / "logistics.json")
        self.inventories = SnapshotFile(data_dir / "inventories.json")
        self.research = SnapshotFile(data_dir / "research.json")
        self.buildings = BuildingIndex(data_dir / "buildings.ndjson")
        # recipes.json is ~11 MB and only consumed out-of-band (recipe-mcp's
        # build_db, the planner) — deliberately NOT a SnapshotFile. The bridge
        # never parses it; only its mtime is surfaced via snapshot_ages().
        self.recipes_path = data_dir / "recipes.json"

    def _resolve_active_dir(self) -> None:
        save_id = _read_current_save_id(self.base_dir)
        if save_id != self._active_save_id:
            logger.info(
                "flma active save changed: %s -> %s",
                self._active_save_id,
                save_id,
            )
            self._active_save_id = save_id
            self._bind(self.base_dir / save_id if save_id else self.base_dir)

    def refresh(self, force: bool = False) -> None:
        with self._lock:
            now = time.monotonic()
            if not force and (now - self._last_refresh) < self.min_refresh_interval:
                return
            self._last_refresh = now
            self._resolve_active_dir()
            # SnapshotFile.read() is itself cheap when unchanged (mtime/size check).
            self.tech.read()
            self.production.read()
            self.logistics.read()
            self.inventories.read()
            self.research.read()
            self.buildings.refresh()

    def health_check(self) -> bool:
        with self._lock:
            self._resolve_active_dir()
            return self.dir.exists()

    # -- query helpers used by server.py tools --------------------------------

    def get_tech(self) -> dict[str, Any]:
        with self._lock:
            self.refresh()
            return self.tech.cached

    def get_production(self) -> dict[str, Any]:
        with self._lock:
            self.refresh()
            return self.production.cached

    def get_logistics(self) -> dict[str, Any]:
        with self._lock:
            self.refresh()
            return self.logistics.cached

    def get_inventories(self) -> dict[str, Any]:
        with self._lock:
            self.refresh()
            return self.inventories.cached

    def get_research(self) -> dict[str, Any]:
        with self._lock:
            self.refresh()
            return self.research.cached

    def get_buildings(self) -> list[dict[str, Any]]:
        with self._lock:
            self.refresh()
            return self.buildings.all()

    def snapshot_ages(self) -> dict[str, float | None]:
        with self._lock:
            self.refresh()
            return {
                "tech": self.tech.age_seconds(),
                "production": self.production.age_seconds(),
                "logistics": self.logistics.age_seconds(),
                "inventories": self.inventories.age_seconds(),
                "research": self.research.age_seconds(),
                "buildings": self.buildings.age_seconds(),
                "recipes": self._recipes_age(),
            }

    def _recipes_age(self) -> float | None:
        try:
            return max(0.0, time.time() - self.recipes_path.stat().st_mtime)
        except OSError:
            return None
