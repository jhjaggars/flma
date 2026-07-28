"""Unit tests for flma_mcp/cli_bridge.py -- the argv-introspection bridge
between MCP tool calls and planner/cli.py's existing command handlers.

None of these import the `mcp` package (cli_bridge.py itself doesn't either
-- only flma_mcp/server.py does), so this file runs in the default
stdlib-only environment same as every other test in this repo."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from flma_mcp import cli_bridge
from planner import config, live_state
from planner.cli import _HANDLERS

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_shared_game_state():
    # cli_bridge.run_cli ultimately calls handlers that call
    # live_state.open_game_state() -- make sure no test's shared-state
    # opt-in leaks into another test via that module-global.
    live_state.reset_shared_game_state()
    yield
    live_state.reset_shared_game_state()


class TestExposedCommands:
    def test_excludes_build_db(self) -> None:
        assert "build-db" not in cli_bridge.EXPOSED_COMMANDS

    def test_covers_every_other_cli_handler(self) -> None:
        # Anti-drift guard: a new planner subcommand must be explicitly
        # accounted for here (exposed or deliberately excluded) rather than
        # silently missing from the MCP surface.
        assert frozenset(_HANDLERS) - {"build-db"} == cli_bridge.EXPOSED_COMMANDS

    def test_planning_tools_plus_live_observe_commands_covers_exposed_commands(self) -> None:
        # planning_tools.py only wraps 12 of EXPOSED_COMMANDS' 18 members as
        # MCP tools -- the other 6 (research/tech-tree/production/logistics/
        # inventory/buildings) are covered by live_tools.py calling
        # planner/observe.py directly instead (richer: returns structured
        # dicts, skips a stdout-capture round trip). This is the anti-drift
        # guard for THAT split: a new CLI subcommand must land in one set or
        # the other, not neither.
        from flma_mcp.planning_tools import PLANNING_COMMANDS

        live_observe_commands = frozenset(
            {"research", "tech-tree", "production", "logistics", "inventory", "buildings"}
        )
        assert PLANNING_COMMANDS | live_observe_commands == cli_bridge.EXPOSED_COMMANDS
        assert PLANNING_COMMANDS.isdisjoint(live_observe_commands)


class TestFindSubparser:
    def test_known_command_returns_its_subparser(self) -> None:
        subparser = cli_bridge._find_subparser("plan")
        dests = {a.dest for a in subparser._actions if a.option_strings}
        assert "rate" in dests
        assert "auto_stop_raw" in dests

    def test_unknown_command_raises_keyerror(self) -> None:
        with pytest.raises(KeyError):
            cli_bridge._find_subparser("no-such-command")


class TestBuildArgv:
    def test_positionals_come_first(self) -> None:
        argv = cli_bridge.build_argv("plan", ["iron-plate", "copper-plate"])
        assert argv == ["plan", "iron-plate", "copper-plate"]

    def test_none_valued_flags_are_omitted(self) -> None:
        argv = cli_bridge.build_argv("plan", ["iron-plate"], rate=None, cap=None)
        assert argv == ["plan", "iron-plate"]

    def test_value_flag_is_appended_as_flag_and_stringified_value(self) -> None:
        argv = cli_bridge.build_argv("plan", ["iron-plate"], rate="10/s")
        assert "--rate" in argv
        assert argv[argv.index("--rate") + 1] == "10/s"

    def test_float_value_is_stringified(self) -> None:
        argv = cli_bridge.build_argv("plan", ["iron-plate"], cap=1.5)
        assert argv[argv.index("--cap") + 1] == "1.5"

    def test_store_true_flag_appended_only_when_true(self) -> None:
        assert "--full" in cli_bridge.build_argv("plan", ["x"], full=True)
        assert "--full" not in cli_bridge.build_argv("plan", ["x"], full=False)

    def test_store_false_flag_appended_only_when_false(self) -> None:
        # --no-auto-stop-raw has dest="auto_stop_raw", default True -- the
        # flag should appear ONLY when the caller passes False (matching
        # its CLI meaning: "turn auto_stop_raw off").
        argv_default = cli_bridge.build_argv("plan", ["x"], auto_stop_raw=True)
        argv_disabled = cli_bridge.build_argv("plan", ["x"], auto_stop_raw=False)
        assert "--no-auto-stop-raw" not in argv_default
        assert "--no-auto-stop-raw" in argv_disabled

    def test_unknown_flag_dest_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="no such flag"):
            cli_bridge.build_argv("plan", ["iron-plate"], not_a_real_flag=1)

    def test_every_planning_command_argv_parses_with_real_parser(self) -> None:
        # The core guarantee this module exists to provide: whatever argv
        # build_argv produces for any exposed command, the REAL parser
        # (build_parser(), not a hand-built Namespace) accepts it and
        # supplies every default -- this is what makes cli_bridge robust
        # against cli.py adding/renaming a flag out from under it.
        samples: dict[str, tuple[list[str], dict]] = {
            "status": ([], {"force": "player"}),
            "recommend": (["iron-plate"], {"rate": "10/s", "auto_stop_raw": False}),
            "options": (["iron-plate"], {"include_byproducts": True}),
            "plan": (["iron-plate"], {"cap": 1.0, "full": True}),
            "expand": (["iron-plate"], {"alternates": True}),
            "recipe": (["iron-plate"], {}),
            "producers": (["iron-plate"], {}),
            "consumers": (["iron-plate"], {}),
            "power": (["assembling-machine-2"], {}),
            "have": (["iron-plate"], {}),
            "belts": (["2"], {"tier": "fast-transport-belt"}),
            "tech": (["Automation"], {"consume": "copper-ore=15/s"}),
        }
        for command, (positionals, flags) in samples.items():
            argv = cli_bridge.build_argv(command, positionals, **flags)
            args = cli_bridge._PARSER.parse_args(argv)
            assert args.command == command


class TestRunCli:
    async def test_bad_argv_returns_ok_false_without_raising(self) -> None:
        result = await cli_bridge.run_cli(["not-a-real-command"])
        assert result["ok"] is False
        assert result["exit_code"] == 2

    async def test_missing_positional_returns_ok_false_without_raising(self) -> None:
        result = await cli_bridge.run_cli(["plan"])  # `product` is required (nargs="+")
        assert result["ok"] is False

    async def test_no_live_data_reports_hint_without_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "SCRIPT_OUTPUT_DIR", tmp_path)
        result = await cli_bridge.run_cli(["research"])
        assert result["ok"] is False
        assert "no data yet" in result["error"] or "hint" in result["error"]

    async def test_concurrent_calls_do_not_interleave_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # contextlib.redirect_stdout swaps sys.stdout PROCESS-WIDE, not
        # per-task -- this proves _CLI_LOCK actually serializes access to it
        # rather than merely existing unused.
        async def slow_handler(args) -> int:
            print(f"{args.command}-line1")
            await asyncio.sleep(0.05)
            print(f"{args.command}-line2")
            return 0

        monkeypatch.setitem(_HANDLERS, "research", slow_handler)
        monkeypatch.setitem(_HANDLERS, "logistics", slow_handler)

        results = await asyncio.gather(
            cli_bridge.run_cli(["research"]),
            cli_bridge.run_cli(["logistics"]),
        )
        for result in results:
            lines = [line for line in result["output"].splitlines() if line]
            assert len(lines) == 2
            assert lines[0] == lines[1].replace("line2", "line1")
