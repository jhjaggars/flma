#!/usr/bin/env python3
"""Agent-level eval harness for the factory planner.

Distinct from recipe-mcp's tests/eval/ (which golden-tests `_pick_producer`
directly — no LLM involved). This measures the thing that actually matters
end-to-end: armed with only the planner CLI, does a *fresh* Claude Code
agent (no memory of any prior investigation) reach a correct answer, and how
many tokens does it cost — instead of falling into the kind of manual,
token-heavy investigation this session did by hand to find the sand /
grade-2-zinc bug (dozens of sqlite queries and tool calls before landing on
the fix).

Spawns real `claude -p` subprocesses — makes real, billed API calls. Not a
pytest suite, not part of `make quick`/`make test`/CI. Run explicitly:

    uv run python tests/agent_eval/run_agent_eval.py
    uv run python tests/agent_eval/run_agent_eval.py --model haiku --tasks sand_recipe
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mlflow_trace

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RECIPES_DB = Path.home() / "code/homelab/apps/recipe-mcp/recipes.db"

# Tool access is restricted to exactly the planner CLI invocation shape —
# no arbitrary shell, no edits, no web — so this measures the planner tool
# itself, not general agent capability.
ALLOWED_TOOLS = "Bash(uv run python -m planner*)"


def _recipe_row(recipe_id: str) -> sqlite3.Row | None:
    con = sqlite3.connect(f"file:{RECIPES_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT name, main_product FROM recipes WHERE name = ?", (recipe_id,)
        ).fetchone()
    finally:
        con.close()


def _recipe_produces(recipe_id: str, item_id: str) -> bool:
    con = sqlite3.connect(f"file:{RECIPES_DB}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT 1 FROM recipe_products WHERE recipe_name = ? AND item_name = ?",
            (recipe_id, item_id),
        ).fetchone()
        return row is not None
    finally:
        con.close()


@dataclass
class Task:
    name: str
    prompt: str
    schema: dict[str, Any]
    check: Callable[[dict[str, Any]], tuple[bool, str]]
    allowed_tools: str = ALLOWED_TOOLS


def _recipe_task_check(item_id: str, known_bad: set[str]) -> Callable[[dict], tuple[bool, str]]:
    def check(result: dict[str, Any]) -> tuple[bool, str]:
        recipe_id = result.get("chosen_recipe")
        if not recipe_id:
            return False, "no 'chosen_recipe' in structured result"
        if not _recipe_produces(recipe_id, item_id):
            return False, f"'{recipe_id}' does not actually produce '{item_id}' (hallucinated?)"
        if recipe_id in known_bad:
            return False, f"picked known-bad recipe '{recipe_id}'"
        row = _recipe_row(recipe_id)
        main_product = row["main_product"] if row else None
        tag = "main product" if main_product == item_id else f"main_product={main_product!r}"
        return True, f"picked '{recipe_id}' ({tag})"

    return check


def _chromium_plan_check(result: dict[str, Any]) -> tuple[bool, str]:
    max_count = result.get("max_machine_count")
    has_ash = result.get("raw_inputs_include_ash")
    sand_recipe = result.get("sand_recipe")
    reason = f"max_machine_count={max_count}, ash={has_ash}, sand_recipe={sand_recipe!r}"
    if not isinstance(max_count, int):
        return False, f"no numeric max_machine_count in result ({reason})"
    # Deliberately NOT gating on a machine-count threshold, same reasoning as
    # _fish_plan_check/_battery_plan_check: several Pyanodons buildings
    # (paddocks, greenhouses) have a "flywheel" mechanic where feeding
    # produced output back in raises crafting speed as much as ~12x over the
    # base rate the planner's static model uses for sizing — so a large but
    # legitimate plan and the byproduct-fishing bug (ash-producing chains
    # like the 1,102-separator sand chain) can't be told apart by count
    # alone. The `ash` check below is the actual signal for that failure
    # mode; reported for visibility/telemetry only.
    if has_ash:
        return False, f"ash in raw inputs — the byproduct-fishing sand chain ({reason})"
    if sand_recipe == "grade-2-zinc":
        return False, f"used grade-2-zinc for sand ({reason})"
    return True, reason


def _battery_plan_check(result: dict[str, Any]) -> tuple[bool, str]:
    max_count = result.get("max_machine_count")
    has_ash = result.get("raw_inputs_include_ash")
    battery_recipe = result.get("battery_recipe")
    reason = f"max_machine_count={max_count}, ash={has_ash}, battery_recipe={battery_recipe!r}"
    if has_ash:
        return False, f"ash in raw inputs — a byproduct-fishing (*-pyvoid) chain ({reason})"
    if not battery_recipe:
        return False, f"no 'battery_recipe' in structured result ({reason})"
    if not _recipe_produces(battery_recipe, "battery-mk01"):
        return False, f"'{battery_recipe}' does not actually produce battery-mk01 ({reason})"
    # Confirmed against a live save (in-game screenshot): the recipe
    # literally named 'battery-mk01' is the correct one (pbsb-alloy,
    # melamine, graphite, bolts, glass, zinc-plate, cyanic-acid) — it just
    # also requires the battery-mk01 tech, same as most recipes require some
    # research. 'battery' (no tracked tech gate) was an earlier
    # misdiagnosis that doesn't match what the game actually shows (no
    # rayon, no sulfuric-acid in the real recipe) — being ungated isn't a
    # sign of correctness, see recipe-mcp's chain-depth fix for other
    # examples of "no tracked gate" going hand-in-hand with a worse pick.
    if battery_recipe == "battery":
        return False, f"used 'battery', the confirmed-wrong recipe ({reason})"
    # Deliberately NOT gating on max_machine_count, same reasoning as
    # _fish_plan_check: battery-mk01's chain hits a still-unfixed, separate
    # bug (a yield-blind alternate-recipe tie-break several stages down —
    # pbsb-alloy/coarse/cyanic-acid sub-picks) that legitimately inflates
    # machine counts (~57 even at the no-`--rate` "1 machine" default)
    # regardless of whether the top-level recipe choice is correct. The old
    # >=50 threshold was calibrated for a different failure mode
    # (byproduct-fishing/ash chains) and produced a false negative here on a
    # confirmed-correct answer. Reported for visibility/telemetry only.
    return True, reason


def _fish_plan_check(result: dict[str, Any]) -> tuple[bool, str]:
    fish_recipe = result.get("fish_recipe")
    has_ash = result.get("raw_inputs_include_ash")
    max_count = result.get("max_machine_count")
    reason = f"fish_recipe={fish_recipe!r}, ash={has_ash}, max_machine_count={max_count}"
    if not fish_recipe:
        return False, f"no 'fish_recipe' in structured result ({reason})"
    if not _recipe_produces(fish_recipe, "fish"):
        return False, f"'{fish_recipe}' does not actually produce 'fish' (hallucinated?) ({reason})"
    if has_ash:
        return False, f"ash in raw inputs — a byproduct-fishing (*-pyvoid) chain ({reason})"
    # Deliberately NOT gating on max_machine_count, unlike the chromium/
    # battery checks: Pyanodons fish farms are a genuinely slow recipe
    # (~150s craft time), so a *correct* plan at an explicit --rate can
    # legitimately need 100+ machines (confirmed: `plan fish --rate 1` ->
    # 129x Fish farm MK 04, even after the chain-depth fix) while the
    # planner's no-`--rate` default (sizes for 1 machine) legitimately needs
    # only 2. Neither number says anything about correctness for this item;
    # reported for visibility/telemetry only.
    return True, reason


def _copper_belt_combo_check(result: dict[str, Any]) -> tuple[bool, str]:
    recipes_used = set(result.get("recipes_used") or [])
    screener = result.get("screener_count")
    crusher = result.get("crusher_count")
    furnace = result.get("furnace_count")
    reason = (
        f"recipes_used={sorted(recipes_used)}, screener={screener}, "
        f"crusher={crusher}, furnace={furnace}"
    )
    # This is the actual bundle "Copper processing - Stage 1" unlocks
    # (verified this session): a screener that splits copper-ore into two
    # grades and a crusher that converts the low grade into more of the
    # high grade, both feeding a smelter. A plain single-recipe `plan`
    # answer (no recipes_used overlap with this set) is the failure mode
    # this task exists to catch -- it's a legitimate-looking but ~2.4x
    # more ore-hungry answer (1440 vs. 900 ore/min for the same output).
    expected_recipes = {"copper-plate-4", "grade-1-copper-crush", "grade-2-copper"}
    missing = expected_recipes - recipes_used
    if missing:
        return False, f"missing expected combo recipe(s) {sorted(missing)} ({reason})"
    for recipe_id, item_id in (
        ("copper-plate-4", "copper-plate"),
        ("grade-1-copper-crush", "grade-2-copper"),
        ("grade-2-copper", "grade-2-copper"),
    ):
        if not _recipe_produces(recipe_id, item_id):
            return False, f"'{recipe_id}' does not actually produce '{item_id}' ({reason})"
    # Confirmed by hand this session against the live save: 1 yellow belt
    # (transport-belt tier, 900 copper-ore/min per planner/throughput.py's
    # placeholder constants) requires sizing the bundle for 180
    # copper-plate/min, which needs exactly this machine count -- see
    # `planner tech "Copper processing - Stage 1" --rate 180 --unit per-min`.
    if (screener, crusher, furnace) != (9, 5, 1):
        return False, f"machine counts don't match the confirmed-correct 9/5/1 ({reason})"
    return True, reason


TASKS: list[Task] = [
    Task(
        name="sand_recipe",
        prompt=(
            "In this repo (flma), use the factory planner CLI "
            "(e.g. `uv run python -m planner producers sand`, `uv run python -m planner recipe <id>`) "
            "to determine which recipe should actually be used to produce `sand` — "
            "avoid a recipe where sand is only a low-probability secondary byproduct "
            "of making something else. Report the recipe id you'd use."
        ),
        schema={
            "type": "object",
            "properties": {"chosen_recipe": {"type": "string"}},
            "required": ["chosen_recipe"],
        },
        check=_recipe_task_check("sand", known_bad={"grade-2-zinc"}),
    ),
    Task(
        name="limestone_recipe",
        prompt=(
            "In this repo (flma), use the factory planner CLI to determine which "
            "recipe should be used to produce `limestone`. Report the recipe id."
        ),
        schema={
            "type": "object",
            "properties": {"chosen_recipe": {"type": "string"}},
            "required": ["chosen_recipe"],
        },
        check=_recipe_task_check("limestone", known_bad=set()),
    ),
    Task(
        name="chromium_basic_plan",
        prompt=(
            "In this repo (flma), run `uv run python -m planner plan chromium --rate 1` "
            "to design a basic chromium production line. Report: the count of the "
            "largest single machine type in the plan (max_machine_count), whether "
            "'ash' appears anywhere in the raw inputs (raw_inputs_include_ash), and "
            "which recipe you'd use for the sand ingredient in the chain (sand_recipe)."
        ),
        schema={
            "type": "object",
            "properties": {
                "max_machine_count": {"type": "integer"},
                "raw_inputs_include_ash": {"type": "boolean"},
                "sand_recipe": {"type": "string"},
            },
            "required": ["max_machine_count", "raw_inputs_include_ash", "sand_recipe"],
        },
        check=_chromium_plan_check,
    ),
    Task(
        name="chromium_naive_plan",
        prompt=(
            "In this repo (flma), let's make a plan for a basic chromium "
            "production setup using the data we have. Once you've settled on "
            "an approach, report: the count of the largest single machine type "
            "in your plan (max_machine_count), whether 'ash' appears anywhere "
            "in the raw inputs (raw_inputs_include_ash), and which recipe you "
            "used for the sand ingredient, if sand is part of the chain at all "
            "(sand_recipe — empty string if not)."
        ),
        schema={
            "type": "object",
            "properties": {
                "max_machine_count": {"type": "integer"},
                "raw_inputs_include_ash": {"type": "boolean"},
                "sand_recipe": {"type": "string"},
            },
            "required": ["max_machine_count", "raw_inputs_include_ash", "sand_recipe"],
        },
        check=_chromium_plan_check,
        # Deliberately NOT restricted to the planner CLI shape (see
        # ALLOWED_TOOLS) — the whole point of this task is to see whether the
        # agent discovers and uses the right tool on its own (the way the
        # very first turn of the session that motivated this eval did), not
        # whether it can follow an explicit command it was handed. Read-only
        # exploration tools + Bash, no Edit/Write.
        allowed_tools="Bash,Read,Glob,Grep,Skill",
    ),
    Task(
        name="battery_starter_plan",
        prompt=(
            "In this repo (flma), let's make a plan for producing 'Battery' "
            "(item id battery-mk01 — not the earlier 'CuZn Battery' tier, "
            "battery-mk00) using the data we have. Once you've settled on "
            "an approach, report: the count of the largest single machine type "
            "in your plan (max_machine_count), whether 'ash' appears anywhere "
            "in the raw inputs (raw_inputs_include_ash), and which recipe you "
            "used to produce the battery itself (battery_recipe)."
        ),
        schema={
            "type": "object",
            "properties": {
                "max_machine_count": {"type": "integer"},
                "raw_inputs_include_ash": {"type": "boolean"},
                "battery_recipe": {"type": "string"},
            },
            "required": ["max_machine_count", "raw_inputs_include_ash", "battery_recipe"],
        },
        check=_battery_plan_check,
        # Same open-ended shape as chromium_naive_plan. Unlike sand/chromium,
        # there's no byproduct-fishing trap here — the trap is the opposite
        # shape: 'battery' (no tracked tech gate) LOOKS like the safe
        # ungated starter pick, but the actual correct recipe is
        # 'battery-mk01' (confirmed in-game), which just happens to require
        # research like most recipes do. See _battery_plan_check. The prompt
        # names battery-mk01/battery-mk00 explicitly because an earlier eval
        # run showed an agent reasonably-but-wrongly settling on battery-mk00
        # ("CuZn Battery", a genuinely distinct, earlier-tier item) when the
        # prompt just said "starter battery" — confirmed with the user that
        # battery-mk01 ("Battery") is the intended target.
        allowed_tools="Bash,Read,Glob,Grep,Skill",
    ),
    Task(
        name="fish_farm_plan",
        prompt=(
            "In this repo (flma), let's make a plan for a basic starter "
            "fish farm setup using the data we have. Once you've settled "
            "on an approach, report: which recipe you used to produce fish "
            "(fish_recipe), the count of the largest single machine type "
            "in your plan (max_machine_count), and whether 'ash' appears "
            "anywhere in the raw inputs (raw_inputs_include_ash)."
        ),
        schema={
            "type": "object",
            "properties": {
                "fish_recipe": {"type": "string"},
                "max_machine_count": {"type": "integer"},
                "raw_inputs_include_ash": {"type": "boolean"},
            },
            "required": ["fish_recipe", "max_machine_count", "raw_inputs_include_ash"],
        },
        check=_fish_plan_check,
        # Same open-discovery shape as chromium_naive_plan/battery_starter_plan.
        # Unlike those two, fish has no same-name recipe trap (only
        # 'breed-fish-1'..'breed-fish-4' + aggressive-selection variants
        # produce 'fish'; none is named 'fish' itself) and no useful
        # machine-count ceiling (see _fish_plan_check). What this task
        # actually exercises: does the agent discover the planner CLI's
        # no-`--rate` default (sizes for 1 machine — the "just build one"
        # starter framing added this session) instead of picking an
        # arbitrary rate. Note the fish/fish-egg breeding loop (fish needs
        # fish-egg, fish-egg needs fish) is a known, deferred limitation —
        # the planner stops at the cycle rather than solving the true
        # steady-state fish-farm:egg-farm ratio, and this check doesn't
        # test for that since the tool itself doesn't surface it.
        allowed_tools="Bash,Read,Glob,Grep,Skill",
    ),
    Task(
        name="copper_belt_combo",
        prompt=(
            "In this repo (flma), tell me what machines I need to fully consume "
            "one yellow belt of copper ore at my current tech level. Report: the "
            "recipe ids used in your plan (recipes_used), and the machine count "
            "needed for each of the screener (screener_count), crusher "
            "(crusher_count), and furnace/smelter (furnace_count) steps."
        ),
        schema={
            "type": "object",
            "properties": {
                "recipes_used": {"type": "array", "items": {"type": "string"}},
                "screener_count": {"type": "integer"},
                "crusher_count": {"type": "integer"},
                "furnace_count": {"type": "integer"},
            },
            "required": ["recipes_used", "screener_count", "crusher_count", "furnace_count"],
        },
        check=_copper_belt_combo_check,
        # Open-ended, matching the user's actual phrasing verbatim (not a
        # forced command) -- tests whether a FRESH agent (no memory of the
        # conversation that motivated this task, and therefore no built-in
        # bias toward the `recommend`/`tech` commands) discovers them on its
        # own instead of defaulting to a plain single-recipe `plan`, which
        # answers with a legitimate-looking but wrong (uncombined, ~2.4x
        # more ore-hungry) plan. Same open-discovery shape as
        # chromium_naive_plan/battery_starter_plan/fish_farm_plan.
        allowed_tools="Bash,Read,Glob,Grep,Skill",
    ),
]


def _parse_stream_json(stdout: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Split newline-delimited stream-json into (all events, final `result` event)."""
    events = []
    final = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        event = json.loads(line)
        events.append(event)
        if event.get("type") == "result":
            final = event
    return events, final


def run_task(
    task: Task,
    model: str,
    max_budget_usd: float,
    base_url: str | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    cmd = [
        "claude",
        "-p",
        task.prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--json-schema",
        json.dumps(task.schema),
        "--model",
        model,
        "--allowedTools",
        task.allowed_tools,
        "--max-budget-usd",
        str(max_budget_usd),
    ]
    env = None
    if base_url:
        # Points claude at a self-hosted Anthropic-API-compatible endpoint
        # (e.g. an Ollama server) instead of Anthropic's own API. Cost/budget
        # accounting downstream is meaningless here -- `claude`'s
        # total_cost_usd is a fallback estimate keyed off a model name it
        # doesn't recognize, not a real bill (self-hosted inference is free).
        # Token counts remain valid and comparable.
        env = {**os.environ, "ANTHROPIC_BASE_URL": base_url, "ANTHROPIC_API_KEY": "local"}
    proc = subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout, env=env
    )
    base = {
        "task": task.name,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "trace_id": None,
    }
    if proc.returncode != 0:
        return {
            **base,
            "passed": False,
            "reason": f"claude exited {proc.returncode}: {proc.stderr.strip()[:500]}",
        }

    events, final = _parse_stream_json(proc.stdout)
    if final is None:
        return {**base, "passed": False, "reason": "no 'result' event in stream-json output"}

    usage = final.get("usage", {})
    input_tokens = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cost_usd = final.get("total_cost_usd", 0.0)

    try:
        result = json.loads(final["result"])
    except (KeyError, json.JSONDecodeError) as exc:
        reason = (
            f"result wasn't valid structured JSON: {exc} (raw: {final.get('result', '')[:300]})"
        )
        trace_id = mlflow_trace.log_trace(
            task.name,
            model,
            task.prompt,
            events,
            None,
            False,
            reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )
        return {
            **base,
            "passed": False,
            "reason": reason,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
            "trace_id": trace_id,
        }

    passed, reason = task.check(result)
    trace_id = mlflow_trace.log_trace(
        task.name,
        model,
        task.prompt,
        events,
        result,
        passed,
        reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )
    return {
        "task": task.name,
        "passed": passed,
        "reason": reason,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "trace_id": trace_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument(
        "--tasks", default=None, help="comma-separated task names to run (default: all)"
    )
    parser.add_argument("--max-budget-usd", type=float, default=1.0)
    parser.add_argument(
        "--base-url",
        default=None,
        help=(
            "point `claude` at a self-hosted Anthropic-API-compatible endpoint (e.g. an "
            "Ollama server) instead of Anthropic's own API -- for evaluating local models. "
            "cost_usd in the results is meaningless in this mode (fallback estimate for an "
            "unrecognized model name, not a real bill); only token counts are comparable."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="per-task subprocess timeout in seconds (default: 300; local models may need more)",
    )
    parser.add_argument(
        "--verbose-trace",
        action="store_true",
        help="print the full turn-by-turn trace (tools called, tokens per turn) for each task",
    )
    args = parser.parse_args()

    selected = TASKS
    if args.tasks:
        wanted = {t.strip() for t in args.tasks.split(",")}
        selected = [t for t in TASKS if t.name in wanted]
        missing = wanted - {t.name for t in selected}
        if missing:
            print(f"error: unknown task(s): {', '.join(sorted(missing))}", file=sys.stderr)
            return 1

    results = [
        run_task(t, args.model, args.max_budget_usd, base_url=args.base_url, timeout=args.timeout)
        for t in selected
    ]

    print(f"\n{'task':<22} {'result':<6} {'in_tok':>8} {'out_tok':>8} {'cost':>8}  reason")
    total_in = total_out = 0
    total_cost = 0.0
    n_passed = 0
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        n_passed += r["passed"]
        total_in += r["input_tokens"]
        total_out += r["output_tokens"]
        total_cost += r["cost_usd"]
        print(
            f"{r['task']:<22} {status:<6} {r['input_tokens']:>8} {r['output_tokens']:>8} "
            f"${r['cost_usd']:>6.3f}  {r['reason']}"
        )
        if r["trace_id"]:
            print(f"  trace: {r['trace_id']}  (mlflow.get_trace / `make trace-ui`)")
        if args.verbose_trace and r["trace_id"]:
            mlflow_trace.print_trace_summary(r["trace_id"])
    print(
        f"\n{n_passed}/{len(results)} passed, {total_in + total_out} total tokens, ${total_cost:.3f} total cost"
    )
    return 0 if n_passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
