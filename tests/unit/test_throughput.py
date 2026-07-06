"""Unit tests for planner/throughput.py's belt/pipe constant helpers — pure
arithmetic, no DB or live game state."""

from __future__ import annotations

import pytest
from planner import throughput

pytestmark = pytest.mark.unit


class TestRateFromBelts:
    def test_round_trips_with_belts_needed(self) -> None:
        """rate_from_belts is the inverse of belts_needed: converting a rate
        to belts and back should return the original rate."""
        original_rate = 42.0
        belts = throughput.belts_needed(original_rate, tier="fast-transport-belt")
        back = throughput.rate_from_belts(belts["belts"], tier="fast-transport-belt")
        assert back["items_per_sec"] == pytest.approx(original_rate)

    def test_default_tier_is_base_starter_tier(self) -> None:
        """No live tech-scoping in this pure-math path, so the static
        default should be the safe base/starter tier, not the fastest one —
        see planner/cli.py's cmd_belts for the live tech-scoped default."""
        result = throughput.rate_from_belts(1.0)
        assert result["tier"] == throughput.DEFAULT_BELT_TIER_ORDER[0]
        assert result["tier"] == "transport-belt"

    def test_unknown_tier_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown belt tier"):
            throughput.rate_from_belts(1.0, tier="not-a-real-belt")

    def test_carries_accuracy_placeholder_flag(self) -> None:
        result = throughput.rate_from_belts(1.0)
        assert result["accurate"] == throughput.VALUES_ARE_PYANODONS_ACCURATE

    def test_two_belts_double_one_belt(self) -> None:
        one = throughput.rate_from_belts(1.0, tier="transport-belt")
        two = throughput.rate_from_belts(2.0, tier="transport-belt")
        assert two["items_per_sec"] == pytest.approx(one["items_per_sec"] * 2)
