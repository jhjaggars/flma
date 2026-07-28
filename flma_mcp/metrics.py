"""Collectors for the `/metrics` Prometheus scrape endpoint (`server.py`'s
`/metrics` route). Reads the same shared `GameState` (`flma_mcp/state.py`)
and reuses the same shaping functions (`planner/observe.py`,
`planner/live_state.py`) every MCP tool already goes through -- nothing here
reimplements research/tech/logistics/building shaping, it only turns those
results into `exposition.Family` objects.

See `flma_mcp/CLAUDE.md`'s "Prometheus metrics" section for the full metric
table, the produced/consumed naming caveat (Factorio's `input_counts` means
PRODUCED and `output_counts` means CONSUMED -- inverted from intuition, see
`planner/live_state.py`'s `net_production` docstring), the cardinality-cap
rule (anything an alert references must be in `FLMA_MCP_METRICS_ITEM_
ALLOWLIST` -- top-N membership is not stable enough to alert on), and why
game-data series are dropped entirely rather than frozen when the game isn't
running.

`collect()` must never raise -- a single bad collector must not take
`flma_up` down with it, since that's the one series every alert hangs off.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

from planner import live_state, observe
from src.game_state import GameState

from flma_mcp import config, state
from flma_mcp.exposition import CONTENT_TYPE, Family, render

logger = logging.getLogger("flma_mcp.metrics")

# Sticky top-N selection, keyed by (force, kind) where kind is "items" or
# "fluids". Rebound wholesale (never mutated in place) each scrape so two
# overlapping scrapes can't tear it -- see _production_families.
_selection: dict[tuple[str, str], frozenset[str]] = {}

# Process-lifetime count of collector failures, exposed as
# flma_scrape_errors_total. A counter, so it only ever grows within one
# process; reset_for_tests() clears it between tests.
_scrape_errors = 0


def _bump_scrape_errors() -> None:
    global _scrape_errors
    _scrape_errors += 1


def reset_for_tests() -> None:
    """Test-only: clears the sticky top-N selection state and the scrape-
    error counter, so each test starts clean instead of inheriting selection
    history or error counts left over from a previous test in the same
    process."""
    global _scrape_errors
    _selection.clear()
    _scrape_errors = 0


def select_names(
    scores: Mapping[str, float],
    top_n: int,
    allowlist: frozenset[str],
    previous: frozenset[str],
    slack: float = 0.5,
) -> frozenset[str]:
    """Choose which item/fluid ids get a production series this scrape.

    `top_n == 0` emits the allowlist only (per-item production effectively
    off); `top_n < 0` emits everything, uncapped. Otherwise: the top `top_n`
    ids by `scores`, plus any id in `previous` that still ranks within
    `top_n * (1 + slack)` -- hysteresis against boundary flapping, since ids
    clustered near the cutoff can otherwise swap in and out on every scrape
    for no analytical reason (see flma_mcp/CLAUDE.md, measured on a real
    save: ranks 34-52 sat within ~15 units/min of each other) -- plus the
    allowlist unconditionally. Ties break on name so selection is
    deterministic scrape to scrape.
    """
    if top_n == 0:
        return frozenset(allowlist)
    if top_n < 0:
        return frozenset(scores) | frozenset(allowlist)
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    head = {name for name, _ in ranked[:top_n]}
    slack_cutoff = top_n + int(top_n * slack)
    sticky = {name for name, _ in ranked[top_n:slack_cutoff] if name in previous}
    return frozenset(head | sticky) | frozenset(allowlist)


# --------------------------------------------------------------------------
# Meta families -- always emitted, even when the game isn't running.
# --------------------------------------------------------------------------


def _fallback_up_family(fresh: dict[str, Any]) -> list[Family]:
    """Used only if `_meta_families` itself somehow raises -- keeps
    `flma_up` on the wire even when everything else about this scrape has
    gone wrong."""
    up = Family(
        "flma_up",
        "gauge",
        "1 if the flma mod's live export is fresh (Factorio running and exporting), 0 otherwise.",
    )
    up.add(1 if fresh.get("game_running") else 0)
    return [up]


def _meta_families(gs: GameState, fresh: dict[str, Any], scrape_seconds: float) -> list[Family]:
    up = Family(
        "flma_up",
        "gauge",
        "1 if the flma mod's live export is fresh (Factorio running and "
        "exporting), 0 otherwise. NOT the same as Prometheus' own up{} "
        "series, which only says this exporter itself answered the scrape "
        "-- see flma_mcp/CLAUDE.md for the distinction.",
    )
    up.add(1 if fresh.get("game_running") else 0)

    ages = Family(
        "flma_snapshot_age_seconds",
        "gauge",
        "Seconds since each exported snapshot file was last written. A "
        "snapshot never seen at all has no series here -- use absent(), "
        "not a NaN or large-value comparison, to detect that.",
    )
    for snapshot, age in gs.snapshot_ages().items():
        if age is not None:
            ages.add(age, snapshot=snapshot)

    save_info = Family(
        "flma_save_info",
        "gauge",
        "Always 1; carries the flma mod's active per-save export directory "
        "id as a label. Changes when the running save changes -- "
        "flma_items_*_total/flma_fluids_*_total counters are NOT "
        "comparable across a save switch (see flma_mcp/CLAUDE.md).",
    )
    save_id = getattr(gs, "_active_save_id", None)
    if save_id is not None:
        save_info.add(1, save_id=save_id)

    duration = Family(
        "flma_scrape_duration_seconds",
        "gauge",
        "Seconds spent building this exposition body (dominated by the "
        "building-index scan when flma-export-buildings is on).",
    )
    duration.add(scrape_seconds)

    errors = Family(
        "flma_scrape_errors_total",
        "counter",
        "Collector functions that have raised since this process started. "
        "Non-zero means at least one scrape was incomplete -- check server "
        "logs for which collector failed.",
    )
    errors.add(_scrape_errors)

    return [up, ages, save_info, duration, errors]


# --------------------------------------------------------------------------
# Game-data families -- only collected while flma_up == 1 (see collect()).
# --------------------------------------------------------------------------


def _research_families(gs: GameState) -> list[Family]:
    progress = Family(
        "flma_research_progress_ratio",
        "gauge",
        "Progress (0-1) of the technology currently being researched, per "
        "force. Absent for a force with nothing in progress.",
    )
    current = Family(
        "flma_research_current_info",
        "gauge",
        "Always 1 when research is in progress for `force`; carries the "
        "technology name as a label. Family is entirely absent when idle.",
    )
    queue_len = Family(
        "flma_research_queue_length",
        "gauge",
        "Number of technologies queued behind the current research, per force.",
    )

    for force in config.METRICS_FORCES:
        result = observe.research_status(gs, force=force)
        if "error" in result:
            continue
        research_progress = result.get("research_progress")
        if research_progress is not None:
            progress.add(research_progress, force=force)
        current_research = result.get("current_research")
        if current_research:
            current.add(1, force=force, technology=current_research)
        # research_queue can serialize as `{}` instead of `[]` when empty
        # (Factorio's table_to_json can't distinguish -- see SCHEMA.md);
        # len() is correct either way, no special-casing needed.
        queue_len.add(len(result.get("research_queue") or []), force=force)

    return [progress, current, queue_len]


def _tech_families(gs: GameState) -> list[Family]:
    counts_fam = Family(
        "flma_technologies",
        "gauge",
        "Technology count by status (researched/available/locked) for "
        "`force`, from planner.observe.tech_tree's live prerequisite-based "
        "classification.",
    )
    bonus_fam = Family(
        "flma_mining_drill_productivity_bonus_ratio",
        "gauge",
        "Force's current mining-drill yield bonus, e.g. 0.2 = +20% ore per "
        "mining operation at no extra energy/time cost. 0 if the mod build "
        "predates this field or the force has no bonus.",
    )

    for force in config.METRICS_FORCES:
        tree = observe.tech_tree(gs, force=force)
        if "error" in tree:
            continue
        # Always emit all three statuses (even at 0) so a dashboard sum
        # never silently drops a status that happens to be empty this scrape.
        counts = {"researched": 0, "available": 0, "locked": 0}
        for tech in tree.get("technologies", []):
            status = tech.get("status")
            if status in counts:
                counts[status] += 1
        for status, count in counts.items():
            counts_fam.add(count, force=force, status=status)
        bonus_fam.add(live_state.mining_drill_productivity_bonus(gs, force=force), force=force)

    return [counts_fam, bonus_fam]


def _logistics_families(gs: GameState) -> list[Family]:
    networks_fam = Family(
        "flma_logistic_networks",
        "gauge",
        "Number of logistic networks for `force` on `surface`.",
    )
    robots_fam = Family(
        "flma_logistic_robots",
        "gauge",
        "Total logistic (bot) robots across `force`'s logistic networks on `surface`.",
    )
    robots_avail_fam = Family(
        "flma_logistic_robots_available",
        "gauge",
        "Idle logistic robots (not currently assigned to a task) across "
        "`force`'s logistic networks on `surface`.",
    )
    construction_fam = Family(
        "flma_construction_robots",
        "gauge",
        "Total construction robots across `force`'s logistic networks on `surface`.",
    )
    construction_avail_fam = Family(
        "flma_construction_robots_available",
        "gauge",
        "Idle construction robots across `force`'s logistic networks on `surface`.",
    )

    for force in config.METRICS_FORCES:
        result = observe.logistics(gs, force=force)
        if "error" in result:
            continue
        raw_networks = result.get("networks", [])
        # An idle force's `networks` value can serialize as `{}` (a dict)
        # instead of `[]` -- the same empty-array-as-object quirk documented
        # in SCHEMA.md -- so guard the type rather than assume a list.
        if not isinstance(raw_networks, list):
            raw_networks = []

        per_surface: dict[str, dict[str, int]] = {}
        for net in raw_networks:
            surface = net.get("surface") or "unknown"
            agg = per_surface.setdefault(
                surface,
                {
                    "networks": 0,
                    "robots": 0,
                    "robots_avail": 0,
                    "construction": 0,
                    "construction_avail": 0,
                },
            )
            agg["networks"] += 1
            agg["robots"] += net.get("all_logistic_robots") or 0
            agg["robots_avail"] += net.get("available_logistic_robots") or 0
            agg["construction"] += net.get("all_construction_robots") or 0
            agg["construction_avail"] += net.get("available_construction_robots") or 0

        for surface, agg in per_surface.items():
            networks_fam.add(agg["networks"], force=force, surface=surface)
            robots_fam.add(agg["robots"], force=force, surface=surface)
            robots_avail_fam.add(agg["robots_avail"], force=force, surface=surface)
            construction_fam.add(agg["construction"], force=force, surface=surface)
            construction_avail_fam.add(agg["construction_avail"], force=force, surface=surface)

    return [networks_fam, robots_fam, robots_avail_fam, construction_fam, construction_avail_fam]


def _building_families(gs: GameState) -> list[Family]:
    by_name = Family(
        "flma_buildings_by_name",
        "gauge",
        "Placed building count for `force`, by internal entity name. "
        "Requires the flma-export-buildings map setting. Capped at "
        "FLMA_MCP_METRICS_TOP_BUILDING_NAMES by count (-1 = unlimited).",
    )
    by_type = Family(
        "flma_buildings_by_type",
        "gauge",
        "Placed building count for `force`, by entity type "
        "(assembling-machine, furnace, ...). Requires the "
        "flma-export-buildings map setting.",
    )

    for force in config.METRICS_FORCES:
        counts = observe.building_counts(gs, force=force)
        if counts.get("total", 0) == 0:
            continue
        names = counts.get("by_name") or {}
        top_n = config.METRICS_TOP_BUILDING_NAMES
        if top_n >= 0:
            names = dict(sorted(names.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n])
        for name, count in names.items():
            by_name.add(count, force=force, name=name)
        for type_name, count in (counts.get("by_type") or {}).items():
            by_type.add(count, force=force, type=type_name)

    return [by_name, by_type]


_PRODUCED_HELP = (
    "{noun} produced (cumulative since the force began). Sourced from "
    "production.json's input_counts field -- Factorio's LuaFlowStatistics "
    'naming is inverted from intuition: "input" is what the force '
    'PRODUCED, "output" is what it CONSUMED (matches the in-game '
    "production GUI's left/right split; see SCHEMA.md and "
    "planner/live_state.py's net_production docstring). Selection capped "
    "to the top FLMA_MCP_METRICS_TOP_ITEMS by throughput plus "
    "FLMA_MCP_METRICS_ITEM_ALLOWLIST -- see flma_mcp/CLAUDE.md's "
    "cardinality section before writing an alert against this."
)
_CONSUMED_HELP = (
    "{noun} consumed (cumulative since the force began). Sourced from "
    "production.json's output_counts field -- see the sibling "
    "flma_{kind}_produced_total metric's HELP for the input/output naming "
    "caveat."
)
_PRODUCED_RATE_HELP = (
    "{noun} produced per minute -- the Factorio engine's own 60-second "
    "rolling average (input_rates_per_min), not a Prometheus rate() over "
    "the counter. This is the number the in-game GUI shows. For windows "
    "longer than a minute prefer rate(flma_{kind}_produced_total[5m]) * 60."
)
_CONSUMED_RATE_HELP = (
    "{noun} consumed per minute, engine 60-second rolling average (output_rates_per_min)."
)


def _production_families(gs: GameState) -> list[Family]:
    produced = Family("flma_items_produced_total", "counter", _PRODUCED_HELP.format(noun="Items"))
    consumed = Family(
        "flma_items_consumed_total", "counter", _CONSUMED_HELP.format(noun="Items", kind="items")
    )
    produced_rate = Family(
        "flma_items_produced_per_minute",
        "gauge",
        _PRODUCED_RATE_HELP.format(noun="Items", kind="items"),
    )
    consumed_rate = Family(
        "flma_items_consumed_per_minute",
        "gauge",
        _CONSUMED_RATE_HELP.format(noun="Items"),
    )
    fluids_produced = Family(
        "flma_fluids_produced_total", "counter", _PRODUCED_HELP.format(noun="Fluids")
    )
    fluids_consumed = Family(
        "flma_fluids_consumed_total", "counter", _CONSUMED_HELP.format(noun="Fluids", kind="fluids")
    )
    fluids_produced_rate = Family(
        "flma_fluids_produced_per_minute",
        "gauge",
        _PRODUCED_RATE_HELP.format(noun="Fluids", kind="fluids"),
    )
    fluids_consumed_rate = Family(
        "flma_fluids_consumed_per_minute",
        "gauge",
        _CONSUMED_RATE_HELP.format(noun="Fluids"),
    )
    seen_fam = Family(
        "flma_metrics_items_seen",
        "gauge",
        "Distinct item/fluid ids present in this scrape's production "
        "snapshot for `force`, before the top-N cap.",
    )
    selected_fam = Family(
        "flma_metrics_items_selected",
        "gauge",
        "Distinct item/fluid ids actually emitted as "
        "flma_{items,fluids}_*_total/_per_minute series for `force` -- "
        "compare against flma_metrics_items_seen to see whether "
        "FLMA_MCP_METRICS_TOP_ITEMS is discarding anything.",
    )

    families = [
        produced,
        consumed,
        produced_rate,
        consumed_rate,
        fluids_produced,
        fluids_consumed,
        fluids_produced_rate,
        fluids_consumed_rate,
        seen_fam,
        selected_fam,
    ]

    data = gs.get_production()
    all_forces = data.get("forces") or {}

    for force in config.METRICS_FORCES:
        force_data = all_forces.get(force) or {}
        surfaces = force_data.get("surfaces") or {}
        if not surfaces:
            continue

        for kind, kind_singular, (prod_fam, cons_fam, prod_rate_fam, cons_rate_fam) in (
            ("items", "item", (produced, consumed, produced_rate, consumed_rate)),
            (
                "fluids",
                "fluid",
                (fluids_produced, fluids_consumed, fluids_produced_rate, fluids_consumed_rate),
            ),
        ):
            # Only surfaces that actually have this kind's block -- an older
            # mod build or a mid-cycle read can omit it entirely; skip
            # rather than treat as all-zero.
            surface_maps = {
                surface_name: surface_data[kind]
                for surface_name, surface_data in surfaces.items()
                if surface_data.get(kind)
            }
            if not surface_maps:
                continue

            scores: dict[str, float] = {}
            all_names: set[str] = set()
            for kind_data in surface_maps.values():
                input_counts = kind_data.get("input_counts") or {}
                output_counts = kind_data.get("output_counts") or {}
                input_rates = kind_data.get("input_rates_per_min") or {}
                output_rates = kind_data.get("output_rates_per_min") or {}
                all_names.update(input_counts, output_counts, input_rates, output_rates)
                for name, rate in input_rates.items():
                    scores[name] = scores.get(name, 0.0) + rate
                for name, rate in output_rates.items():
                    scores[name] = scores.get(name, 0.0) + rate

            selection_key = (force, kind)
            selected = select_names(
                scores,
                config.METRICS_TOP_ITEMS,
                config.METRICS_ITEM_ALLOWLIST,
                _selection.get(selection_key, frozenset()),
                config.METRICS_TOP_ITEMS_SLACK,
            )
            _selection[selection_key] = selected

            for surface_name, kind_data in surface_maps.items():
                input_counts = kind_data.get("input_counts") or {}
                output_counts = kind_data.get("output_counts") or {}
                input_rates = kind_data.get("input_rates_per_min") or {}
                output_rates = kind_data.get("output_rates_per_min") or {}
                # Labeled "item" on the fluid families too -- items and
                # fluids share one id namespace in Factorio, so a common
                # label name lets a query union both kinds (e.g. topk over
                # flma_(items|fluids)_produced_per_minute).
                for name in selected:
                    prod_fam.add(
                        input_counts.get(name, 0), force=force, surface=surface_name, item=name
                    )
                    cons_fam.add(
                        output_counts.get(name, 0), force=force, surface=surface_name, item=name
                    )
                    prod_rate_fam.add(
                        input_rates.get(name, 0), force=force, surface=surface_name, item=name
                    )
                    cons_rate_fam.add(
                        output_rates.get(name, 0), force=force, surface=surface_name, item=name
                    )

            seen_fam.add(len(all_names), force=force, kind=kind_singular)
            selected_fam.add(len(selected), force=force, kind=kind_singular)

    return families


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------

_GAME_DATA_COLLECTORS = (
    _research_families,
    _tech_families,
    _logistics_families,
    _production_families,
)


def collect() -> list[Family]:
    """Build every metric family for one scrape. Never raises: each
    game-data collector is individually wrapped in try/except, and a
    failure there costs only that collector's series, not flma_up (which is
    what every alert should hang off -- see flma_mcp/CLAUDE.md)."""
    gs = state.gs()
    fresh = state.freshness()

    start = time.perf_counter()
    game_families: list[Family] = []
    # Game-data series are dropped entirely (not frozen at their last
    # value) once the export goes stale -- a frozen per-minute rate would
    # be a false statement about a factory that isn't running. See
    # flma_mcp/CLAUDE.md's staleness section.
    if fresh.get("game_running"):
        collectors = list(_GAME_DATA_COLLECTORS)
        if config.METRICS_BUILDINGS:
            collectors.append(_building_families)
        for collector in collectors:
            try:
                game_families.extend(collector(gs))
            except Exception:
                logger.exception("flma_mcp.metrics: %s failed", collector.__name__)
                _bump_scrape_errors()
    scrape_seconds = time.perf_counter() - start

    try:
        meta_families = _meta_families(gs, fresh, scrape_seconds)
    except Exception:
        logger.exception("flma_mcp.metrics: _meta_families failed")
        _bump_scrape_errors()
        meta_families = _fallback_up_family(fresh)

    return meta_families + game_families


def render_text() -> str:
    """Synchronous, blocking -- collect() + exposition.render(). Callers on
    an event loop should use build_body() instead."""
    return render(collect())


async def build_body() -> str:
    """Refresh the shared GameState the same way every MCP tool does
    (`flma_mcp/live_tools.py`'s `_observe`), then render off the event loop.
    Skipping the `to_thread` hop here would put the building-index scan (or,
    after a mod-side buildings.ndjson compaction, a full replay measured at
    ~850ms) directly on the event loop, stalling every in-flight MCP tool
    call for the remote agent."""
    await state.warm()
    return await asyncio.to_thread(render_text)


__all__ = [
    "CONTENT_TYPE",
    "select_names",
    "reset_for_tests",
    "collect",
    "render_text",
    "build_body",
]
