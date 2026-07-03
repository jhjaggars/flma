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
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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
    # 50 is well above what a sane plan needs (~12 machine types, low
    # single/double digits each) and well below the 1,102-separator
    # byproduct-fishing chain this eval exists to catch.
    if max_count >= 50:
        return False, f"machine count too high, likely the byproduct-fishing chain ({reason})"
    if has_ash:
        return False, f"ash in raw inputs — the byproduct-fishing sand chain ({reason})"
    if sand_recipe == "grade-2-zinc":
        return False, f"used grade-2-zinc for sand ({reason})"
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


def run_task(task: Task, model: str, max_budget_usd: float) -> dict[str, Any]:
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
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=300)
    base = {"task": task.name, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "trace_id": None}
    if proc.returncode != 0:
        return {**base, "passed": False, "reason": f"claude exited {proc.returncode}: {proc.stderr.strip()[:500]}"}

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
        reason = f"result wasn't valid structured JSON: {exc} (raw: {final.get('result', '')[:300]})"
        trace_id = mlflow_trace.log_trace(task.name, task.prompt, events, None, False, reason)
        return {**base, "passed": False, "reason": reason, "input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": cost_usd, "trace_id": trace_id}

    passed, reason = task.check(result)
    trace_id = mlflow_trace.log_trace(task.name, task.prompt, events, result, passed, reason)
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

    results = [run_task(t, args.model, args.max_budget_usd) for t in selected]

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
    print(f"\n{n_passed}/{len(results)} passed, {total_in + total_out} total tokens, ${total_cost:.3f} total cost")
    return 0 if n_passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
