"""Unit tests for planner/module_bonus.py's assumed module-acceleration
math for Moondrop greenhouses and Auog paddocks — pure arithmetic, no DB."""

from __future__ import annotations

import pytest
from planner import module_bonus

pytestmark = pytest.mark.unit


class TestIsModuleAccelerated:
    def test_moondrop_greenhouse_matches(self) -> None:
        assert module_bonus.is_module_accelerated("moondrop-greenhouse-mk01") is True

    def test_auog_paddock_matches(self) -> None:
        assert module_bonus.is_module_accelerated("auog-paddock-mk04") is True

    def test_unrelated_machine_does_not_match(self) -> None:
        assert module_bonus.is_module_accelerated("smelter-mk01") is False

    def test_similarly_named_vrauk_paddock_does_not_match(self) -> None:
        """Vrauks paddock is a distinct Pyanodons building family with no
        confirmed module-bonus figure — must not be swept in by a loose
        substring match on "paddock"."""
        assert module_bonus.is_module_accelerated("vrauks-paddock-mk01") is False


class TestEffectiveSpeed:
    def test_boosts_moondrop_greenhouse_by_slots(self) -> None:
        # 16 slots @ +100%/slot -> 17x
        assert module_bonus.effective_speed(
            "moondrop-greenhouse-mk01", 0.058823529411765, 16
        ) == pytest.approx(0.058823529411765 * 17)

    def test_boosts_auog_paddock_by_slots(self) -> None:
        # 4 slots @ +100%/slot -> 5x
        assert module_bonus.effective_speed("auog-paddock-mk01", 0.4, 4) == pytest.approx(0.4 * 5)

    def test_zero_slots_is_unchanged(self) -> None:
        assert module_bonus.effective_speed("moondrop-greenhouse-mk01", 0.4, 0) == pytest.approx(
            0.4
        )

    def test_unrelated_machine_is_unchanged_even_with_slots(self) -> None:
        """A machine outside MODULE_ACCELERATED_PREFIXES is left alone even
        if it happens to have module slots -- we don't have a confirmed
        bonus figure for it."""
        assert module_bonus.effective_speed("smelter-mk01", 1.0, 4) == pytest.approx(1.0)
