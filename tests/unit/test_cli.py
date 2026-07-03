"""Unit tests for planner/cli.py helpers that don't need a live DB/game
state — pure string/data formatting logic."""

from __future__ import annotations

import pytest
from planner.cli import _is_tech_locked

pytestmark = pytest.mark.unit


class TestIsTechLocked:
    def test_matches_exact_item(self) -> None:
        notes = ["ore-chromium: only tech-locked producer(s) available (requires: X) — treating as raw"]
        assert _is_tech_locked("ore-chromium", notes) is True

    def test_no_false_positive_on_substring(self) -> None:
        # "water" is a substring of "geothermal-water" — a naive `in` check
        # over the whole note text would wrongly tag plain water as
        # tech-locked too.
        notes = [
            "geothermal-water: only tech-locked producer(s) available (requires: X) — treating as raw"
        ]
        assert _is_tech_locked("water", notes) is False
        assert _is_tech_locked("geothermal-water", notes) is True

    def test_no_notes(self) -> None:
        assert _is_tech_locked("sand", []) is False
