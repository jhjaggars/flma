"""Unit tests for GameState: snapshot re-reading and the buildings NDJSON
event-log folding (including truncation/compaction detection)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from src.game_state import BuildingIndex, GameState, SnapshotFile

pytestmark = pytest.mark.unit


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def append_ndjson(path: Path, *events: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")


class TestSnapshotFile:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        snap = SnapshotFile(tmp_path / "missing.json")
        assert snap.read() == {}
        assert snap.age_seconds() is None

    def test_reads_written_data(self, tmp_path: Path) -> None:
        path = tmp_path / "tech.json"
        write_json(path, {"tick": 100, "force": "player"})
        snap = SnapshotFile(path)
        assert snap.read() == {"tick": 100, "force": "player"}
        assert snap.age_seconds() is not None
        assert snap.age_seconds() >= 0

    def test_missing_file_after_good_read_keeps_last_good_value(self, tmp_path: Path) -> None:
        # The mod writes via truncate-then-write, not atomic rename, so a
        # transient FileNotFoundError should be indistinguishable from a torn
        # write — both fall back to the last successfully parsed value rather
        # than resetting to {}.
        path = tmp_path / "tech.json"
        write_json(path, {"tick": 1})
        snap = SnapshotFile(path)
        first = snap.read()
        path.unlink()
        second = snap.read()
        assert second == first == {"tick": 1}

    def test_handles_partial_write_gracefully(self, tmp_path: Path) -> None:
        path = tmp_path / "tech.json"
        write_json(path, {"tick": 1})
        snap = SnapshotFile(path)
        assert snap.read() == {"tick": 1}
        # Simulate a torn write (mod mid-write when we poll)
        path.write_text("{not valid json", encoding="utf-8")
        # Should keep the last-good value rather than raising
        assert snap.read() == {"tick": 1}


class TestBuildingIndex:
    def test_folds_add_and_remove_events(self, tmp_path: Path) -> None:
        path = tmp_path / "buildings.ndjson"
        path.write_text("", encoding="utf-8")
        idx = BuildingIndex(path)

        append_ndjson(
            path,
            {"t": 10, "op": "add", "entity": {"id": 1, "name": "assembling-machine-2"}},
            {"t": 11, "op": "add", "entity": {"id": 2, "name": "inserter"}},
        )
        idx.refresh()
        assert {b["id"] for b in idx.all()} == {1, 2}
        assert idx.last_tick == 11

        append_ndjson(path, {"t": 20, "op": "remove", "id": 1})
        idx.refresh()
        assert {b["id"] for b in idx.all()} == {2}
        assert idx.last_tick == 20

    def test_detects_compaction_and_replays(self, tmp_path: Path) -> None:
        path = tmp_path / "buildings.ndjson"
        append_ndjson(
            path,
            {"t": 1, "op": "add", "entity": {"id": 1, "name": "a"}},
            {"t": 2, "op": "add", "entity": {"id": 2, "name": "b"}},
            {"t": 3, "op": "remove", "id": 1},
        )
        idx = BuildingIndex(path)
        idx.refresh()
        assert {b["id"] for b in idx.all()} == {2}

        # Mod compacts: truncates and rewrites from its own in-memory registry,
        # which only ever contains what's currently standing (id 2).
        path.write_text("", encoding="utf-8")
        append_ndjson(path, {"t": 100, "op": "add", "entity": {"id": 2, "name": "b"}})
        idx.refresh()
        assert {b["id"] for b in idx.all()} == {2}
        assert idx.last_tick == 100

    def test_detects_compaction_via_fingerprint_when_size_does_not_shrink(
        self, tmp_path: Path
    ) -> None:
        # A size-only check misses a compaction that rewrites the file to a
        # size at or above our current offset -- the bridge would then
        # resume reading at the old byte offset into unrelated new content.
        # BuildingIndex additionally fingerprints the file's leading bytes,
        # which always change on compaction (mod/control.lua's
        # compact_buildings always starts the rewrite with a fresh tick
        # value), so this case is still caught.
        path = tmp_path / "buildings.ndjson"
        append_ndjson(
            path,
            {"t": 1, "op": "add", "entity": {"id": 1, "name": "a"}},
            {"t": 2, "op": "add", "entity": {"id": 2, "name": "b"}},
        )
        idx = BuildingIndex(path)
        idx.refresh()
        assert {b["id"] for b in idx.all()} == {1, 2}
        old_size = path.stat().st_size

        padding = "x" * old_size
        new_line = json.dumps(
            {"t": 999, "op": "add", "entity": {"id": 2, "name": "b", "extra": padding}}
        )
        path.write_text(new_line + "\n", encoding="utf-8")
        assert path.stat().st_size >= old_size  # rewrite did NOT shrink below the old offset

        idx.refresh()
        assert {b["id"] for b in idx.all()} == {2}
        assert idx.last_tick == 999

    def test_ignores_partial_trailing_line(self, tmp_path: Path) -> None:
        path = tmp_path / "buildings.ndjson"
        path.write_text(
            '{"t": 1, "op": "add", "entity": {"id": 1, "name": "a"}}\n', encoding="utf-8"
        )
        idx = BuildingIndex(path)
        idx.refresh()
        assert len(idx.all()) == 1

        # Append a line without a trailing newline (looks like a concurrent
        # write caught mid-flush).
        with path.open("a", encoding="utf-8") as f:
            f.write('{"t": 2, "op": "add", "entity"')
        idx.refresh()
        # The good line's already applied; the partial one is not consumed.
        assert len(idx.all()) == 1

        # Complete the line on the next refresh.
        with path.open("a", encoding="utf-8") as f:
            f.write(': {"id": 2, "name": "b"}}\n')
        idx.refresh()
        assert {b["id"] for b in idx.all()} == {1, 2}

    def test_missing_file_is_a_noop(self, tmp_path: Path) -> None:
        idx = BuildingIndex(tmp_path / "missing.ndjson")
        idx.refresh()  # should not raise
        assert idx.all() == []


class TestGameState:
    def test_get_tech_returns_snapshot(self, tmp_path: Path) -> None:
        write_json(tmp_path / "tech.json", {"tick": 5, "force": "player"})
        gs = GameState(tmp_path, min_refresh_interval=0)
        assert gs.get_tech() == {"tick": 5, "force": "player"}

    def test_get_buildings_reads_ndjson(self, tmp_path: Path) -> None:
        append_ndjson(
            tmp_path / "buildings.ndjson",
            {"t": 1, "op": "add", "entity": {"id": 7, "name": "furnace", "type": "furnace"}},
        )
        gs = GameState(tmp_path, min_refresh_interval=0)
        buildings = gs.get_buildings()
        assert len(buildings) == 1
        assert buildings[0]["name"] == "furnace"

    def test_health_check_requires_directory(self, tmp_path: Path) -> None:
        gs = GameState(tmp_path / "nope", min_refresh_interval=0)
        assert gs.health_check() is False
        gs2 = GameState(tmp_path, min_refresh_interval=0)
        assert gs2.health_check() is True

    def test_refresh_respects_min_interval(self, tmp_path: Path) -> None:
        path = tmp_path / "tech.json"
        write_json(path, {"tick": 1})
        gs = GameState(tmp_path, min_refresh_interval=60)
        assert gs.get_tech() == {"tick": 1}

        # Update the file, but since min_refresh_interval is huge, a
        # non-forced refresh should keep serving the cached snapshot.
        time.sleep(0.01)
        write_json(path, {"tick": 2})
        assert gs.get_tech() == {"tick": 1}

        # A forced refresh always re-reads.
        gs.refresh(force=True)
        assert gs.get_tech() == {"tick": 2}

    def test_snapshot_ages_reports_none_when_missing(self, tmp_path: Path) -> None:
        gs = GameState(tmp_path, min_refresh_interval=0)
        ages = gs.snapshot_ages()
        assert ages == {
            "tech": None,
            "production": None,
            "logistics": None,
            "inventories": None,
            "research": None,
            "buildings": None,
            "recipes": None,
        }

    def test_get_research_returns_snapshot(self, tmp_path: Path) -> None:
        write_json(
            tmp_path / "research.json",
            {"tick": 5, "forces": {"player": {"current_research": "automation"}}},
        )
        gs = GameState(tmp_path, min_refresh_interval=0)
        assert gs.get_research()["forces"]["player"]["current_research"] == "automation"

    def test_snapshot_ages_reports_buildings_age(self, tmp_path: Path) -> None:
        append_ndjson(
            tmp_path / "buildings.ndjson",
            {"t": 1, "op": "add", "entity": {"id": 1, "name": "a"}},
        )
        gs = GameState(tmp_path, min_refresh_interval=0)
        ages = gs.snapshot_ages()
        assert ages["buildings"] is not None
        assert ages["buildings"] >= 0

    def test_snapshot_ages_reports_recipes_age_without_parsing(self, tmp_path: Path) -> None:
        # recipes.json is ~11 MB in real games and only consumed out-of-band
        # (recipe-mcp's build_db, the planner) — the bridge must stat() it,
        # never parse it. Invalid JSON proves no parse happens.
        (tmp_path / "recipes.json").write_text("{ not json at all")
        gs = GameState(tmp_path, min_refresh_interval=0)
        ages = gs.snapshot_ages()
        assert ages["recipes"] is not None
        assert ages["recipes"] >= 0


class TestGameStateLocking:
    def test_refresh_is_serialized_by_a_lock(self, tmp_path: Path) -> None:
        """GameState is queried from concurrent asyncio.to_thread() workers
        (src/server.py), so refresh() must hold a lock across its whole body.
        The worst race it guards against: two threads both pass the
        min_refresh_interval throttle and both call BuildingIndex.refresh(),
        which would double-advance its byte offset and permanently skip
        whatever fell in the gap.

        Verified directly (not probabilistically): patch
        BuildingIndex.refresh to record when each call starts/ends, run two
        threads concurrently, and confirm the calls never overlap -- the
        second call's start is never before the first call's end.
        """
        append_ndjson(
            tmp_path / "buildings.ndjson",
            {"t": 1, "op": "add", "entity": {"id": 1, "name": "a"}},
        )
        gs = GameState(tmp_path, min_refresh_interval=0)

        original_refresh = gs.buildings.refresh
        events: list[tuple[str, float]] = []

        def slow_refresh() -> None:
            events.append(("start", time.monotonic()))
            time.sleep(0.1)
            original_refresh()
            events.append(("end", time.monotonic()))

        gs.buildings.refresh = slow_refresh  # type: ignore[method-assign]

        threads = [threading.Thread(target=gs.refresh, kwargs={"force": True}) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert [label for label, _ in events] == ["start", "end", "start", "end"]
        first_end = events[1][1]
        second_start = events[2][1]
        assert second_start >= first_end
