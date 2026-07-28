"""Unit tests for flma_mcp/exposition.py -- the hand-rolled Prometheus text
exposition (0.0.4) writer. Pure functions, no GameState, no fixtures: these
pin the exact traps a hand-rolled exporter is prone to (escape ordering,
lowercase inf/nan, bool-is-int, duplicate TYPE lines) -- see that module's
docstring and flma_mcp/CLAUDE.md's "why hand-rolled" note for why this isn't
just delegated to a library."""

from __future__ import annotations

import pytest
from flma_mcp.exposition import (
    Family,
    escape_help,
    escape_label_value,
    format_value,
    render,
)

pytestmark = pytest.mark.unit


class TestLabelValueEscaping:
    def test_backslash_is_escaped(self) -> None:
        assert escape_label_value("a\\b") == "a\\\\b"

    def test_quote_is_escaped(self) -> None:
        assert escape_label_value('a"b') == 'a\\"b'

    def test_newline_is_escaped(self) -> None:
        assert escape_label_value("a\nb") == "a\\nb"

    def test_carriage_return_is_dropped(self) -> None:
        assert escape_label_value("a\rb") == "ab"

    def test_backslash_before_quote_ordering(self) -> None:
        # The trap: escaping `"` before `\` would turn this input's literal
        # backslash into `\\"` -- an escaped quote, not "backslash followed
        # by an escaped quote" -- corrupting everything rendered after it.
        assert escape_label_value('a\\"b') == 'a\\\\\\"b'

    def test_hyphenated_factorio_id_passes_through_unmangled(self) -> None:
        assert escape_label_value("iron-plate") == "iron-plate"


class TestHelpEscaping:
    def test_backslash_and_newline_escaped(self) -> None:
        assert escape_help("a\\b\nc") == "a\\\\b\\nc"

    def test_quote_left_literal(self) -> None:
        assert escape_help('a "quoted" word') == 'a "quoted" word'


class TestValueFormatting:
    def test_int_stays_exact(self) -> None:
        assert format_value(5804438) == "5804438"

    def test_large_int_survives_without_precision_loss(self) -> None:
        big = 2**60 + 1
        assert format_value(big) == str(big)

    def test_float_uses_shortest_round_trip(self) -> None:
        assert format_value(1450.2) == "1450.2"

    def test_float_does_not_pad_with_zeros(self) -> None:
        assert "1450.200000" not in format_value(1450.2)

    def test_positive_infinity(self) -> None:
        assert format_value(float("inf")) == "+Inf"

    def test_negative_infinity(self) -> None:
        assert format_value(float("-inf")) == "-Inf"

    def test_nan(self) -> None:
        assert format_value(float("nan")) == "NaN"

    def test_bool_true_is_not_python_str(self) -> None:
        assert format_value(True) == "1"

    def test_bool_false_is_not_python_str(self) -> None:
        assert format_value(False) == "0"

    def test_negative_zero(self) -> None:
        # -0.0's repr is "-0.0", which is valid and distinct from "0.0" --
        # just confirm it doesn't crash or get mangled.
        assert format_value(-0.0) == "-0.0"


class TestRender:
    def test_help_then_type_then_samples(self) -> None:
        fam = Family("flma_up", "gauge", "help text")
        fam.add(1)
        body = render([fam])
        lines = body.splitlines()
        assert lines[0] == "# HELP flma_up help text"
        assert lines[1] == "# TYPE flma_up gauge"
        assert lines[2] == "flma_up 1"

    def test_type_appears_exactly_once(self) -> None:
        fam = Family("flma_x", "gauge", "h")
        fam.add(1, a="1")
        fam.add(2, a="2")
        body = render([fam])
        assert body.count("# TYPE flma_x gauge") == 1

    def test_samples_of_one_family_are_contiguous(self) -> None:
        fam1 = Family("flma_a", "gauge", "h1")
        fam1.add(1)
        fam2 = Family("flma_b", "gauge", "h2")
        fam2.add(2)
        body = render([fam1, fam2])
        lines = body.splitlines()
        a_idx = lines.index("flma_a 1")
        b_help_idx = lines.index("# HELP flma_b h2")
        assert a_idx < b_help_idx

    def test_duplicate_family_name_raises(self) -> None:
        fam1 = Family("flma_dup", "gauge", "h")
        fam1.add(1)
        fam2 = Family("flma_dup", "gauge", "h")
        fam2.add(2)
        with pytest.raises(ValueError, match="duplicate"):
            render([fam1, fam2])

    def test_zero_sample_family_is_omitted_entirely(self) -> None:
        empty = Family("flma_empty", "gauge", "h")
        present = Family("flma_present", "gauge", "h")
        present.add(1)
        body = render([empty, present])
        assert "flma_empty" not in body
        assert "flma_present 1" in body

    def test_no_label_sample_has_no_braces(self) -> None:
        fam = Family("flma_x", "gauge", "h")
        fam.add(1)
        body = render([fam])
        assert "flma_x 1" in body
        assert "flma_x{" not in body

    def test_labels_render_sorted_deterministically(self) -> None:
        fam = Family("flma_x", "gauge", "h")
        fam.add(1, z="1", a="2")
        body = render([fam])
        assert 'flma_x{a="2",z="1"} 1' in body

    def test_body_ends_with_newline(self) -> None:
        fam = Family("flma_x", "gauge", "h")
        fam.add(1)
        assert render([fam]).endswith("\n")

    def test_empty_family_list_renders_empty_string(self) -> None:
        assert render([]) == ""


class TestValidation:
    def test_bad_metric_name_raises(self) -> None:
        with pytest.raises(ValueError):
            Family("flma-bad", "gauge", "h")

    def test_bad_label_name_raises(self) -> None:
        fam = Family("flma_x", "gauge", "h")
        with pytest.raises(ValueError):
            fam.add(1, **{"save-id": "x"})

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(ValueError):
            Family("flma_x", "summary", "h")

    def test_family_name_is_immutable(self) -> None:
        fam = Family("flma_x", "gauge", "h")
        with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
            fam.name = "flma_y"  # type: ignore[misc]

    def test_family_add_is_allowed_despite_frozen(self) -> None:
        fam = Family("flma_x", "gauge", "h")
        fam.add(1)  # must not raise
        assert len(fam.samples) == 1


class TestNanInfNeverLowercase:
    def test_render_of_special_floats_never_lowercase(self) -> None:
        fam = Family("flma_x", "gauge", "h")
        fam.add(float("inf"))
        fam.add(float("-inf"), a="1")
        fam.add(float("nan"), a="2")
        body = render([fam])
        assert "NaN" in body
        assert "+Inf" in body
        assert "-Inf" in body
        # Lowercase-only spellings are a hard parse error for Prometheus --
        # confirm neither the bare "inf" nor "nan" token ever appears.
        assert " inf" not in body
        assert "nan" not in body.replace("NaN", "")
