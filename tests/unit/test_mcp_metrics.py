"""Unit tests for flma_mcp/metrics.py -- the /metrics collectors. Mirrors
tests/unit/test_mcp_live_tools.py's binding pattern (a fresh GameState per
test, bound onto flma_mcp.state's module global via monkeypatch) since
metrics.py reads the same shared state through flma_mcp.state.gs()."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from flma_mcp import config, metrics, state
from src.game_state import GameState

pytestmark = pytest.mark.unit


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolated_state():
    yield
    state.reset_for_tests()
    metrics.reset_for_tests()


def bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, min_refresh_interval: float = 0
) -> GameState:
    gs = GameState(tmp_path, min_refresh_interval=min_refresh_interval)
    monkeypatch.setattr(state, "_gs", gs)
    return gs


def _families_by_name(families) -> dict:
    return {fam.name: fam for fam in families}


def _samples(fam, **label_filter) -> list:
    return [
        (labels, value)
        for labels, value in fam.samples
        if all(labels.get(k) == v for k, v in label_filter.items())
    ]


class TestSelectNames:
    def test_top_n_selects_highest_scores(self) -> None:
        scores = {"a": 10.0, "b": 5.0, "c": 1.0}
        selected = metrics.select_names(
            scores, top_n=2, allowlist=frozenset(), previous=frozenset()
        )
        assert selected == {"a", "b"}

    def test_allowlist_below_the_cut_is_still_selected(self) -> None:
        scores = {"a": 10.0, "b": 5.0, "c": 1.0}
        selected = metrics.select_names(
            scores, top_n=1, allowlist=frozenset({"c"}), previous=frozenset()
        )
        assert selected == {"a", "c"}

    def test_top_n_zero_returns_allowlist_only(self) -> None:
        scores = {"a": 10.0, "b": 5.0}
        selected = metrics.select_names(
            scores, top_n=0, allowlist=frozenset({"z"}), previous=frozenset()
        )
        assert selected == {"z"}

    def test_top_n_negative_returns_everything(self) -> None:
        scores = {"a": 10.0, "b": 5.0}
        selected = metrics.select_names(
            scores, top_n=-1, allowlist=frozenset(), previous=frozenset()
        )
        assert selected == {"a", "b"}

    def test_ties_break_by_name_deterministically(self) -> None:
        scores = {"b": 1.0, "a": 1.0, "c": 1.0}
        selected = metrics.select_names(
            scores, top_n=2, allowlist=frozenset(), previous=frozenset()
        )
        assert selected == {"a", "b"}  # "a","b" sort before "c" on tied score

    def test_sticky_retains_a_previously_selected_id_within_slack(self) -> None:
        # 10 ids, scores 10..1 descending (rank 1 = "n10" .. rank 10 = "n1").
        scores = {f"n{i}": float(i) for i in range(1, 11)}
        top_n = 2  # top_n*1.5 = 3 -> rank 3 (score 8.0, "n8") is in the slack window
        previous = frozenset({"n8"})
        selected = metrics.select_names(
            scores, top_n=top_n, allowlist=frozenset(), previous=previous
        )
        assert "n8" in selected  # rank 3, within top_n*(1+0.5)=3
        assert "n7" not in selected  # rank 4, outside the slack window

    def test_sticky_does_not_resurrect_an_id_outside_the_slack_window(self) -> None:
        scores = {f"n{i}": float(i) for i in range(1, 11)}
        previous = frozenset({"n1"})  # rank 10, far outside any reasonable slack window
        selected = metrics.select_names(
            scores, top_n=2, allowlist=frozenset(), previous=previous, slack=0.5
        )
        assert "n1" not in selected

    def test_slack_zero_disables_sticky_retention(self) -> None:
        scores = {f"n{i}": float(i) for i in range(1, 11)}
        previous = frozenset({"n8"})  # rank 3
        selected = metrics.select_names(
            scores, top_n=2, allowlist=frozenset(), previous=previous, slack=0
        )
        assert "n8" not in selected


class TestProductionFamilies:
    def test_input_counts_map_to_produced_not_consumed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The single most important regression test in this file: Factorio's
        production.json calls what the force PRODUCED `input_counts` and
        what it CONSUMED `output_counts` (see planner/live_state.py's
        net_production docstring / SCHEMA.md). A metric-naming refactor that
        silently flips this would only be caught here."""
        write_json(
            tmp_path / "production.json",
            {
                "tick": 100,
                "forces": {
                    "player": {
                        "surfaces": {
                            "nauvis": {
                                "items": {
                                    "input_counts": {"iron-plate": 7},
                                    "output_counts": {"iron-plate": 3},
                                    "input_rates_per_min": {"iron-plate": 70.0},
                                    "output_rates_per_min": {"iron-plate": 30.0},
                                }
                            }
                        }
                    }
                },
            },
        )
        write_json(tmp_path / "research.json", {"tick": 100, "forces": {"player": {}}})
        bind(tmp_path, monkeypatch)

        families = _families_by_name(metrics.collect())
        produced = _samples(families["flma_items_produced_total"], item="iron-plate")
        consumed = _samples(families["flma_items_consumed_total"], item="iron-plate")
        assert produced == [({"force": "player", "surface": "nauvis", "item": "iron-plate"}, 7)]
        assert consumed == [({"force": "player", "surface": "nauvis", "item": "iron-plate"}, 3)]

    def test_fluid_float_and_item_int_types_are_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_json(
            tmp_path / "production.json",
            {
                "tick": 100,
                "forces": {
                    "player": {
                        "surfaces": {
                            "nauvis": {
                                "items": {
                                    "input_counts": {"iron-plate": 5},
                                    "output_counts": {},
                                    "input_rates_per_min": {"iron-plate": 5.0},
                                    "output_rates_per_min": {},
                                },
                                "fluids": {
                                    "input_counts": {"water": 117057426.56},
                                    "output_counts": {},
                                    "input_rates_per_min": {"water": 1201.98},
                                    "output_rates_per_min": {},
                                },
                            }
                        }
                    }
                },
            },
        )
        write_json(tmp_path / "research.json", {"tick": 100, "forces": {"player": {}}})
        bind(tmp_path, monkeypatch)

        families = _families_by_name(metrics.collect())
        item_value = _samples(families["flma_items_produced_total"], item="iron-plate")[0][1]
        fluid_value = _samples(families["flma_fluids_produced_total"], item="water")[0][1]
        assert isinstance(item_value, int)
        assert isinstance(fluid_value, float)
        assert fluid_value == 117057426.56

    def test_two_surfaces_produce_two_labelled_samples(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_json(
            tmp_path / "production.json",
            {
                "tick": 100,
                "forces": {
                    "player": {
                        "surfaces": {
                            "nauvis": {
                                "items": {
                                    "input_counts": {"iron-plate": 1},
                                    "output_counts": {},
                                    "input_rates_per_min": {"iron-plate": 1.0},
                                    "output_rates_per_min": {},
                                }
                            },
                            "fulgora": {
                                "items": {
                                    "input_counts": {"iron-plate": 2},
                                    "output_counts": {},
                                    "input_rates_per_min": {"iron-plate": 2.0},
                                    "output_rates_per_min": {},
                                }
                            },
                        }
                    }
                },
            },
        )
        write_json(tmp_path / "research.json", {"tick": 100, "forces": {"player": {}}})
        bind(tmp_path, monkeypatch)

        families = _families_by_name(metrics.collect())
        samples = _samples(families["flma_items_produced_total"], item="iron-plate")
        surfaces = {labels["surface"] for labels, _ in samples}
        assert surfaces == {"nauvis", "fulgora"}

    def test_surface_missing_items_key_is_skipped_without_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_json(
            tmp_path / "production.json",
            {
                "tick": 100,
                "forces": {
                    "player": {
                        "surfaces": {
                            "nauvis": {
                                "fluids": {
                                    "input_counts": {},
                                    "output_counts": {},
                                    "input_rates_per_min": {},
                                    "output_rates_per_min": {},
                                }
                                # deliberately no "items" key at all
                            }
                        }
                    }
                },
            },
        )
        write_json(tmp_path / "research.json", {"tick": 100, "forces": {"player": {}}})
        bind(tmp_path, monkeypatch)

        families = _families_by_name(metrics.collect())  # must not raise
        assert families["flma_items_produced_total"].samples == []

    def test_selected_id_absent_from_rate_map_still_gets_explicit_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "METRICS_ITEM_ALLOWLIST", frozenset({"raw-coal"}))
        write_json(
            tmp_path / "production.json",
            {
                "tick": 100,
                "forces": {
                    "player": {
                        "surfaces": {
                            "nauvis": {
                                "items": {
                                    "input_counts": {"iron-plate": 5},
                                    "output_counts": {},
                                    "input_rates_per_min": {"iron-plate": 5.0},
                                    "output_rates_per_min": {},
                                }
                                # "raw-coal" never appears anywhere
                            }
                        }
                    }
                },
            },
        )
        write_json(tmp_path / "research.json", {"tick": 100, "forces": {"player": {}}})
        bind(tmp_path, monkeypatch)

        families = _families_by_name(metrics.collect())
        samples = _samples(families["flma_items_produced_total"], item="raw-coal")
        assert samples == [({"force": "player", "surface": "nauvis", "item": "raw-coal"}, 0)]

    def test_non_player_forces_produce_no_series(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_json(
            tmp_path / "production.json",
            {
                "tick": 100,
                "forces": {
                    "player": {"surfaces": {}},
                    "enemy": {
                        "surfaces": {
                            "nauvis": {
                                "items": {
                                    "input_counts": {"iron-plate": 999},
                                    "output_counts": {},
                                    "input_rates_per_min": {"iron-plate": 999.0},
                                    "output_rates_per_min": {},
                                }
                            }
                        }
                    },
                },
            },
        )
        write_json(tmp_path / "research.json", {"tick": 100, "forces": {"player": {}}})
        bind(tmp_path, monkeypatch)

        families = _families_by_name(metrics.collect())
        assert families["flma_items_produced_total"].samples == []


class TestLivenessAndStaleness:
    def test_never_seen_snapshot_reports_flma_up_zero_with_no_gaps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bind(tmp_path, monkeypatch)
        families = _families_by_name(metrics.collect())
        assert families["flma_up"].samples == [({}, 0)]
        assert "flma_items_produced_total" not in families
        age_samples = _samples(families["flma_snapshot_age_seconds"], snapshot="production")
        assert age_samples == []  # absent, not NaN

    def test_fresh_snapshot_reports_up_and_production_families(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_json(tmp_path / "production.json", {"tick": 1, "forces": {}})
        write_json(tmp_path / "research.json", {"tick": 1, "forces": {}})
        gs = bind(tmp_path, monkeypatch)
        gs.refresh(force=True)

        families = _families_by_name(metrics.collect())
        assert families["flma_up"].samples == [({}, 1)]
        assert "flma_items_produced_total" in families

    def test_stale_snapshot_drops_game_data_but_keeps_meta(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_json(tmp_path / "production.json", {"tick": 1, "forces": {}})
        write_json(tmp_path / "research.json", {"tick": 1, "forces": {}})
        gs = bind(tmp_path, monkeypatch)
        gs.refresh(force=True)

        import flma_mcp.config as mcp_config

        monkeypatch.setattr(mcp_config, "STALE_AFTER_SECONDS", -1)

        families = _families_by_name(metrics.collect())
        assert families["flma_up"].samples == [({}, 0)]
        assert "flma_items_produced_total" not in families
        assert "flma_technologies" not in families
        # Meta families survive staleness.
        assert families["flma_scrape_duration_seconds"].samples

    def test_save_info_present_when_current_save_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_json(tmp_path / "current-save.json", {"save_id": "abc123", "tick": 1})
        (tmp_path / "abc123").mkdir()
        write_json(tmp_path / "abc123" / "production.json", {"tick": 1, "forces": {}})
        gs = bind(tmp_path, monkeypatch)
        gs.refresh(force=True)

        families = _families_by_name(metrics.collect())
        assert families["flma_save_info"].samples == [({"save_id": "abc123"}, 1)]

    def test_save_info_absent_without_current_save_pointer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bind(tmp_path, monkeypatch)
        # _meta_families always builds the Family object; "absent" is a
        # render()-level guarantee (see the sibling test above).
        families = _families_by_name(metrics.collect())
        assert families["flma_save_info"].samples == []
        assert "flma_save_info" not in metrics.render_text()


class TestLogisticsFamilies:
    def test_robots_summed_per_surface(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_json(
            tmp_path / "logistics.json",
            {
                "tick": 1,
                "forces": {
                    "player": [
                        {
                            "network_id": 1,
                            "surface": "nauvis",
                            "contents": [],
                            "available_logistic_robots": 3,
                            "all_logistic_robots": 10,
                            "available_construction_robots": 1,
                            "all_construction_robots": 5,
                        },
                        {
                            "network_id": 2,
                            "surface": "nauvis",
                            "contents": [],
                            "available_logistic_robots": 2,
                            "all_logistic_robots": 8,
                            "available_construction_robots": 0,
                            "all_construction_robots": 4,
                        },
                    ]
                },
            },
        )
        write_json(tmp_path / "production.json", {"tick": 1, "forces": {}})
        write_json(tmp_path / "research.json", {"tick": 1, "forces": {}})
        bind(tmp_path, monkeypatch)

        families = _families_by_name(metrics.collect())
        assert _samples(families["flma_logistic_robots"], surface="nauvis") == [
            ({"force": "player", "surface": "nauvis"}, 18)
        ]
        assert _samples(families["flma_logistic_robots_available"], surface="nauvis") == [
            ({"force": "player", "surface": "nauvis"}, 5)
        ]
        assert _samples(families["flma_logistic_networks"], surface="nauvis") == [
            ({"force": "player", "surface": "nauvis"}, 2)
        ]

    def test_empty_dict_forces_value_does_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Factorio's table_to_json serializes an empty array as `{}`, not
        # `[]` -- an idle force's logistics entry can look like this.
        write_json(tmp_path / "logistics.json", {"tick": 1, "forces": {"player": {}}})
        write_json(tmp_path / "production.json", {"tick": 1, "forces": {}})
        write_json(tmp_path / "research.json", {"tick": 1, "forces": {}})
        bind(tmp_path, monkeypatch)

        families = _families_by_name(metrics.collect())  # must not raise
        assert families["flma_logistic_networks"].samples == []


class TestBuildingsAndTech:
    def test_buildings_by_name_filters_to_the_requested_force(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ndjson = (
            json.dumps(
                {
                    "t": 1,
                    "op": "add",
                    "entity": {
                        "id": 1,
                        "name": "assembling-machine-1",
                        "type": "assembling-machine",
                        "surface": "nauvis",
                        "position": {"x": 0, "y": 0},
                        "force": "player",
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "t": 2,
                    "op": "add",
                    "entity": {
                        "id": 2,
                        "name": "assembling-machine-1",
                        "type": "assembling-machine",
                        "surface": "nauvis",
                        "position": {"x": 1, "y": 1},
                        "force": "enemy",
                    },
                }
            )
            + "\n"
        )
        (tmp_path / "buildings.ndjson").write_text(ndjson, encoding="utf-8")
        write_json(tmp_path / "production.json", {"tick": 1, "forces": {}})
        write_json(tmp_path / "research.json", {"tick": 1, "forces": {}})
        bind(tmp_path, monkeypatch)

        families = _families_by_name(metrics.collect())
        by_name = _samples(families["flma_buildings_by_name"], force="player")
        assert by_name == [({"force": "player", "name": "assembling-machine-1"}, 1)]

    def test_technologies_match_tech_tree_classification(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_json(
            tmp_path / "tech.json",
            {
                "tick": 1,
                "forces": {
                    "player": {
                        "technologies": {
                            "automation": {
                                "researched": True,
                                "enabled": True,
                                "prerequisites": [],
                            },
                            "logistics": {
                                "researched": False,
                                "enabled": True,
                                "prerequisites": ["automation"],
                            },
                            "locked-tech": {
                                "researched": False,
                                "enabled": False,
                                "prerequisites": [],
                            },
                        }
                    }
                },
            },
        )
        write_json(tmp_path / "production.json", {"tick": 1, "forces": {}})
        write_json(tmp_path / "research.json", {"tick": 1, "forces": {}})
        bind(tmp_path, monkeypatch)

        families = _families_by_name(metrics.collect())
        counts = {
            labels["status"]: value for labels, value in families["flma_technologies"].samples
        }
        assert counts == {"researched": 1, "available": 1, "locked": 1}

    def test_empty_research_queue_reports_zero_length(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_json(
            tmp_path / "research.json",
            {"tick": 1, "forces": {"player": {"research_queue": {}}}},
        )
        write_json(tmp_path / "production.json", {"tick": 1, "forces": {}})
        bind(tmp_path, monkeypatch)

        families = _families_by_name(metrics.collect())
        assert _samples(families["flma_research_queue_length"], force="player") == [
            ({"force": "player"}, 0)
        ]

    def test_no_current_research_omits_current_info_family(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_json(
            tmp_path / "research.json",
            {"tick": 1, "forces": {"player": {"research_queue": {}}}},
        )
        write_json(tmp_path / "production.json", {"tick": 1, "forces": {}})
        bind(tmp_path, monkeypatch)

        # _research_families always constructs the Family object (so
        # collect()'s raw list still has it, empty) -- "entirely absent"
        # is a render()-level guarantee (zero-sample families are skipped),
        # so assert at that level, not against collect()'s raw output.
        families = _families_by_name(metrics.collect())
        assert families["flma_research_current_info"].samples == []
        assert "flma_research_current_info" not in metrics.render_text()


class TestRenderTextEndToEnd:
    def test_rendered_body_is_well_formed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_json(
            tmp_path / "production.json",
            {
                "tick": 42,
                "forces": {
                    "player": {
                        "surfaces": {
                            "nauvis": {
                                "items": {
                                    "input_counts": {"iron-plate": 7},
                                    "output_counts": {"iron-plate": 3},
                                    "input_rates_per_min": {"iron-plate": 70.5},
                                    "output_rates_per_min": {"iron-plate": 30.0},
                                }
                            }
                        }
                    }
                },
            },
        )
        write_json(tmp_path / "research.json", {"tick": 42, "forces": {"player": {}}})
        bind(tmp_path, monkeypatch)

        body = metrics.render_text()

        seen_types: set[str] = set()
        sample_re_ok = True
        lines = body.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("# TYPE"):
                name = line.split()[2]
                if name in seen_types:
                    sample_re_ok = False
                seen_types.add(name)
                # HELP must immediately precede TYPE for the same name.
                assert lines[i - 1].startswith(f"# HELP {name} ")
        assert sample_re_ok
        assert "nan" not in body.replace("NaN", "")
        assert " inf" not in body
        assert "# TYPE flma_up gauge" in body

    def test_never_raises_when_a_collector_blows_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_json(tmp_path / "production.json", {"tick": 1, "forces": {}})
        write_json(tmp_path / "research.json", {"tick": 1, "forces": {}})
        bind(tmp_path, monkeypatch)

        def boom(_gs):
            raise RuntimeError("boom")

        monkeypatch.setattr(metrics, "_GAME_DATA_COLLECTORS", (boom,))

        body = metrics.render_text()
        assert "flma_up 1" in body
        families = _families_by_name(metrics.collect())
        assert families["flma_scrape_errors_total"].samples[0][1] >= 1
