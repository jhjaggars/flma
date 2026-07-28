"""Bridges MCP tool calls into `planner/cli.py`'s existing async command
handlers, by building a real argv list and routing it through the CLI's own
`build_parser()` -- never a hand-built `argparse.Namespace`.

Why not a Namespace: a handler like `cmd_plan` reads a dozen-plus attributes
(`args.force`, `.stop_items`, `.auto_stop_raw`, `.recipe`, `.rate`, `.cap`,
`.belts`, `.belt_tier`, `.max_depth`, `.top`, `.full`, `.product`, `.unit`),
each supplied by a subparser's own `default=`. Missing just one produces an
AttributeError at request time, on whatever flag combination happens to hit
it first. Routing every call through the real `ArgumentParser.parse_args`
means argparse itself supplies every default and applies every `type=`/
`choices=` conversion -- the same defaults `flma-planner` gets on the
command line, guaranteed to never drift out of sync with cli.py.

Why not subprocess: a cold GameState load (a large save's buildings.ndjson
replay) costs hundreds of milliseconds and can use hundreds of MB of RSS
(see SCHEMA.md); several planning commands open the live game state more
than once per invocation. Paying that per MCP call, per process, would
undo the entire point of flma_mcp.state's shared GameState. Running
in-process instead means `state.warm()` (see state.py) already made that
refresh a no-op by the time a handler's own `open_game_state()` call runs.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import traceback
from typing import Any

import planner.cli as planner_cli

_PARSER = planner_cli.build_parser()
_CLI_LOCK = asyncio.Lock()

# `build-db` is deliberately excluded: it rewrites the 10+MB recipes.db a
# long-lived server holds an `immutable=1` connection open on (see
# planner/recipedb/db.py), and a remote agent must not be able to trigger a
# rewrite of a file it's concurrently reading from.
EXPOSED_COMMANDS = frozenset(set(planner_cli._HANDLERS) - {"build-db"})


def _find_subparser(command: str) -> argparse.ArgumentParser:
    for action in _PARSER._actions:
        if isinstance(action, argparse._SubParsersAction):
            try:
                subparser: argparse.ArgumentParser = action.choices[command]
            except KeyError:
                raise KeyError(f"no such planner subcommand: {command!r}") from None
            return subparser
    raise RuntimeError("build_parser() has no subparsers action -- cli.py changed shape")


def build_argv(command: str, positionals: list[str] | None = None, **flags: Any) -> list[str]:
    """Build an argv list for `command` from keyword flag values, deriving
    each flag's `--option-string` and store_true/store_false-ness from the
    REAL subparser (`_find_subparser`) rather than a hand-maintained table --
    so a flag renamed or added in cli.py either still works (same `dest`) or
    fails loudly here (`ValueError`) instead of silently mis-marshalling.

    `flags` keys are argparse `dest` names (what cli.py's handlers read),
    which for every flag in cli.py already matches its Python-safe long-flag
    name (e.g. `--no-auto-stop-raw` has `dest="auto_stop_raw"`, `--max-depth`
    derives dest `max_depth`) -- exactly the kwarg names each MCP tool
    function below declares. A value of None is skipped (not passed), which
    is what lets a tool's own default (usually just mirroring the CLI's)
    fall through to argparse's default.
    """
    subparser = _find_subparser(command)
    actions_by_dest = {a.dest: a for a in subparser._actions if a.option_strings}
    argv = [command, *(positionals or [])]
    for key, value in flags.items():
        if value is None:
            continue
        action = actions_by_dest.get(key)
        if action is None:
            raise ValueError(f"planner {command}: no such flag (dest={key!r})")
        flag = action.option_strings[-1]
        if isinstance(action, argparse._StoreTrueAction):
            if value:
                argv.append(flag)
        elif isinstance(action, argparse._StoreFalseAction):
            if not value:
                argv.append(flag)
        else:
            argv += [flag, str(value)]
    return argv


async def run_cli(argv: list[str]) -> dict[str, Any]:
    """Run one planner subcommand end to end, returning its rendered text.
    Never raises -- a bad argv (SystemExit from argparse), a handler
    exception, anything -- all come back as `{"ok": False, ...}` rather than
    propagating, since an MCP tool that raises is a worse experience for the
    calling model than one that reports what went wrong.

    Serialized by `_CLI_LOCK`: `contextlib.redirect_stdout`/`redirect_stderr`
    swap `sys.stdout`/`sys.stderr` PROCESS-WIDE, not per-task, so two
    concurrent planning calls would otherwise interleave into each other's
    buffers. The live-observe tools (live_tools.py) never print and so never
    take this lock -- a `plan` running long doesn't block a concurrent
    `research` query.
    """
    async with _CLI_LOCK:
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    args = _PARSER.parse_args(argv)
                except SystemExit as exc:
                    return {
                        "ok": False,
                        "exit_code": int(exc.code or 2),
                        "output": out.getvalue(),
                        "error": err.getvalue(),
                    }
                handler = planner_cli._HANDLERS[args.command]
                rc = await handler(args)
        except Exception:
            return {
                "ok": False,
                "exit_code": 1,
                "output": out.getvalue(),
                "error": err.getvalue() + "\n" + traceback.format_exc(limit=5),
            }
        return {
            "ok": rc == 0,
            "exit_code": rc,
            "output": out.getvalue(),
            "error": err.getvalue(),
        }


__all__ = ["build_argv", "run_cli", "EXPOSED_COMMANDS"]
