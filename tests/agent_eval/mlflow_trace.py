"""Replays a `claude -p --output-format stream-json` event stream into an
MLflow trace: one root span per eval task, one child span per turn (a TOOL
span for turns that called a tool, an LLM span for the rest), each carrying
that turn's actual token usage (input/output/cache) and — for tool spans —
the tool's input and result content. That's "what's contributing to context
and what tools are being called" for a given task, queryable after the fact
instead of re-derived by hand the way this session did it.

Backend: local file store (`tests/agent_eval/mlruns/`), not a running
server. The file store is upstream "maintenance mode" (blocked by default,
hence MLFLOW_ALLOW_FILE_STORE) but still fully functional for local,
no-server use — and it lets this repo depend on `mlflow-skinny` alone (a
light client, no sklearn/scipy/Flask) rather than the full `mlflow` package.
`mlflow.search_traces()`/`get_trace()` work directly against it. For the
visual Gantt-style UI, `make trace-ui` runs the real MLflow server via `uvx`
(ephemeral, not added to this project's own dependencies).

Known limitation: spans get replay-time timestamps, not the original call
times — the stream doesn't carry reliable per-turn wall-clock timestamps to
replay against. Order (the `turn` attribute) is preserved and is what
actually matters for "what contributed to context," so this doesn't affect
that; it does mean span durations in the UI aren't meaningful.

Every trace is tagged with `model` and `task` (trace-level tags, filterable
via `search_traces(filter_string=...)`, unlike span attributes) and the root
span carries that run's total input/output tokens and cost — so results are
comparable by model across every eval run ever logged, not just the one
just-run batch. `python mlflow_trace.py [--task NAME]` prints that
comparison; `summarize_by_model()` is the underlying function.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")

import mlflow  # noqa: E402 (env var above must be set before mlflow touches the store)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MLRUNS_DIR = REPO_ROOT / "tests" / "agent_eval" / "mlruns"
EXPERIMENT_NAME = "flma-agent-eval"

mlflow.set_tracking_uri(f"file:{MLRUNS_DIR}")
mlflow.set_experiment(EXPERIMENT_NAME)


def _as_text(content: Any, limit: int = 2000) -> str:
    text = content if isinstance(content, str) else json.dumps(content)
    return text[:limit]


def log_trace(
    task_name: str,
    model: str,
    prompt: str,
    events: list[dict[str, Any]],
    structured_result: dict[str, Any] | None,
    passed: bool,
    reason: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
) -> str:
    """Replay one task's stream-json events into an MLflow trace. Returns
    the trace id."""
    root = mlflow.start_span_no_context(
        name=task_name,
        span_type="AGENT",
        inputs={"prompt": prompt},
        attributes={"model": model},
    )

    # Pass 1: group assistant content blocks by message id (stream-json
    # emits one stream event per content block, several sharing one message
    # id/usage — grouping first avoids order-dependent guessing about
    # whether a given block is the last one for its turn).
    turns: dict[str, dict[str, Any]] = {}
    tool_results: dict[str, str] = {}
    order = 0
    for event in events:
        etype = event.get("type")
        if etype == "assistant":
            msg = event.get("message", {})
            msg_id = msg.get("id", "")
            if msg_id not in turns:
                order += 1
                turns[msg_id] = {"blocks": [], "usage": msg.get("usage") or {}, "order": order}
            turns[msg_id]["blocks"].extend(msg.get("content", []))
        elif etype == "user":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_result":
                    tool_results[block.get("tool_use_id", "")] = _as_text(block.get("content"))

    # Pass 2: one span per turn, in order.
    for turn in sorted(turns.values(), key=lambda t: t["order"]):
        usage = turn["usage"]
        common_attrs = {
            "turn": turn["order"],
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        }
        tool_blocks = [b for b in turn["blocks"] if b.get("type") == "tool_use"]
        text_blocks = [b for b in turn["blocks"] if b.get("type") == "text"]

        for tb in tool_blocks:
            span = mlflow.start_span_no_context(
                name=tb.get("name", "tool"),
                span_type="TOOL",
                parent_span=root,
                inputs=tb.get("input", {}),
                attributes={**common_attrs, "tool_use_id": tb.get("id", "")},
            )
            result_text = tool_results.get(tb.get("id", ""), "")
            span.set_attribute("result_chars", len(result_text))
            span.end(outputs=result_text)

        if text_blocks and not tool_blocks:
            span = mlflow.start_span_no_context(
                name="assistant_response",
                span_type="LLM",
                parent_span=root,
                inputs=None,
                attributes=common_attrs,
            )
            span.end(outputs=_as_text(" ".join(b.get("text", "") for b in text_blocks)))

    root.set_attribute("num_turns", order)
    root.set_attribute("passed", passed)
    root.set_attribute("reason", reason)
    root.set_attribute("input_tokens", input_tokens)
    root.set_attribute("output_tokens", output_tokens)
    root.set_attribute("cost_usd", cost_usd)
    root.set_outputs(structured_result)
    root.end()
    # Trace-level tags (as opposed to span attributes) are what
    # `mlflow.search_traces(filter_string=...)` can actually filter/group
    # on — that's the mechanism for "compare model X vs Y over time".
    client = mlflow.MlflowClient()
    client.set_trace_tag(root.trace_id, "model", model)
    client.set_trace_tag(root.trace_id, "task", task_name)
    mlflow.flush_trace_async_logging()
    return root.trace_id


def summarize_by_model(task_name: str | None = None) -> None:
    """Aggregate every logged trace by model (optionally scoped to one
    task), across all past eval runs — the "compare over time" view."""
    filter_string = f"tag.task = '{task_name}'" if task_name else None
    traces = mlflow.search_traces(filter_string=filter_string, return_type="list")

    by_model: dict[str, dict[str, Any]] = {}
    for trace in traces:
        model = trace.info.tags.get("model")
        if model is None:
            continue  # pre-tagging trace, no model recorded
        root = next((s for s in trace.data.spans if s.parent_id is None), None)
        if root is None:
            continue
        bucket = by_model.setdefault(
            model, {"n": 0, "passed": 0, "in_tok": 0, "out_tok": 0, "cost": 0.0}
        )
        bucket["n"] += 1
        bucket["passed"] += bool(root.attributes.get("passed"))
        bucket["in_tok"] += root.attributes.get("input_tokens", 0)
        bucket["out_tok"] += root.attributes.get("output_tokens", 0)
        bucket["cost"] += root.attributes.get("cost_usd", 0.0)

    if not by_model:
        print("no tagged traces found" + (f" for task {task_name!r}" if task_name else ""))
        return

    print(f"{'model':<10} {'runs':>5} {'pass':>7} {'avg_tok':>9} {'avg_cost':>9}")
    for model, b in sorted(by_model.items()):
        avg_tok = (b["in_tok"] + b["out_tok"]) / b["n"]
        avg_cost = b["cost"] / b["n"]
        pass_str = f"{b['passed']}/{b['n']}"
        print(f"{model:<10} {b['n']:>5} {pass_str:>7} {avg_tok:>9.0f} ${avg_cost:>8.3f}")


def print_trace_summary(trace_id: str) -> None:
    """What's contributing to context, and which tools were called — the
    report you'd otherwise need the UI for. Works with mlflow-skinny alone."""
    trace = mlflow.get_trace(trace_id, flush=True)
    if trace is None:
        print(f"  (trace {trace_id} not found)")
        return
    print(f"  trace: {trace_id}")
    print(
        f"  {'turn':<5} {'type':<6} {'name':<20} {'in_tok':>7} {'cache_new':>9} "
        f"{'cache_hit':>9} {'out_tok':>7} {'result_chars':>12}"
    )
    for span in sorted(trace.data.spans, key=lambda s: s.attributes.get("turn", 0)):
        attrs = span.attributes
        print(
            f"  {attrs.get('turn', '-'):<5} {span.span_type:<6} {span.name:<20} "
            f"{attrs.get('input_tokens', 0):>7} {attrs.get('cache_creation_input_tokens', 0):>9} "
            f"{attrs.get('cache_read_input_tokens', 0):>9} {attrs.get('output_tokens', 0):>7} "
            f"{attrs.get('result_chars', ''):>12}"
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Compare eval results by model across all past runs"
    )
    parser.add_argument("--task", default=None, help="scope to one task name (default: all)")
    args = parser.parse_args()
    summarize_by_model(args.task)
