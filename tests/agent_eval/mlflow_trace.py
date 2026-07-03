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
    prompt: str,
    events: list[dict[str, Any]],
    structured_result: dict[str, Any] | None,
    passed: bool,
    reason: str,
) -> str:
    """Replay one task's stream-json events into an MLflow trace. Returns
    the trace id."""
    root = mlflow.start_span_no_context(
        name=task_name,
        span_type="AGENT",
        inputs={"prompt": prompt},
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
    root.set_outputs(structured_result)
    root.end()
    mlflow.flush_trace_async_logging()
    return root.trace_id


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
