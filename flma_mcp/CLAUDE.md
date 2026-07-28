# flma_mcp/ — MCP server (a consumer, over the network)

Exposes flma's live game state and factory planner to a **remote** consumer that
has no shell on this machine — the homelab Hermes agent — over Streamable HTTP.
Every other consumer in this repo (`planner/`'s CLI, the skills) reaches a local
process directly; this is the one place that crosses a network boundary, so it's
also the one place auth/staleness/concurrency actually matter.

This is additive, not a replacement: `planner/`'s CLI and `src/game_state.py`'s
file-reading model are unchanged and still work exactly as documented in their own
`CLAUDE.md`s. `flma_mcp/` is a thin server shell on top — it reuses
`planner/observe.py`'s functions and `planner/cli.py`'s command handlers verbatim
rather than reimplementing anything (see each module's docstring for specifics).

**History:** an MCP server (`src/server.py`) existed once before and was removed in
commit `da138a7` — at the time, the only consumer was Claude Code, which reaches a
CLI through `Bash` just as directly as it would MCP tools, so the extra layer had
no payoff. `flma_mcp/` reintroduces that layer because Hermes has no `Bash` into
this machine at all; the removal's own reasoning ("the only consumer... which
reaches a CLI through Bash") stops holding once the consumer is remote.

## Module map

| File | Role |
|---|---|
| `config.py` | env vars (`FLMA_MCP_HOST`/`_PORT`/`_TOKEN`/`_STALE_AFTER_SECONDS`); re-exports `SCRIPT_OUTPUT_DIR`/`RECIPES_DB` from `src.config`/`planner.config` rather than duplicating them |
| `state.py` | the one process-wide `GameState`, opted into `planner/live_state.py`'s `use_shared_game_state` so every CLI handler `cli_bridge.py` drives reuses it too; `warm()` refreshes it off the event loop before a handler's own synchronous `open_game_state()` call runs |
| `live_tools.py` | 7 tools wrapping `planner/observe.py`'s functions directly (same logic `src/server.py`'s deleted tools used), plus `flma_status` (health/freshness as structured data) |
| `cli_bridge.py` | `run_cli(argv)` — routes a real argv list through `planner.cli.build_parser()` + `_HANDLERS`, under stdout/stderr capture and a lock. **Never** builds an `argparse.Namespace` by hand (see its own docstring for why) |
| `planning_tools.py` | 12 tools wrapping the CLI's text-only planning commands (`plan`/`recommend`/`options`/`expand`/`recipe`/`producers`/`consumers`/`power`/`have`/`belts`/`tech`/`status`) via `cli_bridge`. Deliberately excludes `build-db` |
| `static_tools.py` | 9 tools ported from the homelab repo's `apps/recipe-mcp` (search/browse/machine/drill lookups + research-path expansion) — see below |
| `server.py` | `FastMCP` assembly, `/healthz`, the bearer-token middleware, `main()` |

Every tool result carries a `freshness` envelope (`age_seconds`/`stale`/
`game_running`/`save_id`/`note`) — see `state.freshness()`. Nothing errors when
Factorio isn't running or the export is stale; it's reported in-band instead, since
a remote caller has no other way to know.

## Why flma supersedes recipe-mcp (and the tradeoff that implies)

The homelab repo's `apps/recipe-mcp` already gave Hermes 16 Factorio recipe tools,
but its DB is baked into its container image at build time from a hand-copied,
git-committed `recipes.json` — it goes stale the moment the running save's modpack
or research state changes, and there's no way to tell from its answers alone.
flma's `recipes.db` is built from the **currently running save's own export**
(`make build-db`), and its schema is a verified strict superset of recipe-mcp's
(same 13 core tables, plus `fuels`/`generators`/`machine_fuel_categories`) — every
recipe-mcp query ports over unchanged. `static_tools.py` ports the 9 tools
`planner/cli.py` never grew subcommands for; the other 7 recipe-mcp tools
(`find_recipes_producing`/`find_recipes_using`/`get_recipe`/`plan_factory`/
`find_recipes_unlocked_by_technology`/`list_researchable_technologies`) are **not**
re-ported, because flma's own `producers`/`consumers`/`recipe`/`plan`/`recommend`/
`tech`/`tech-tree` (via `planning_tools.py`) already cover the same ground with LIVE
annotations (`[N built]` tags, live tech-scoping) a static DB can't provide.

**The accepted tradeoff:** recipe-mcp is always deployed in-cluster; flma_mcp only
runs when this desktop is up. Superseding it means Hermes loses ALL Factorio recipe
Q&A whenever this machine sleeps or is off. That's why retiring recipe-mcp in the
homelab repo is its own separate, later step — done only after flma_mcp is verified
working end to end, not bundled with standing this server up. If the outage proves
too disruptive in practice, the fallback is re-pointing recipe-mcp's own DB build at
flma's live export (killing its staleness without losing always-on), not resurrecting
the stale one as-is.

## Development

```bash
uv sync --extra mcp   # only this server needs the `mcp` package; planner/tests don't
make run-mcp          # foreground, binds 127.0.0.1:9110 by default

curl -s localhost:9110/healthz | python3 -m json.tool
```

`make quick` must stay green with the `mcp` extra **not** installed — that's the
check that the optional-dependency boundary (`pyproject.toml`'s
`[project.optional-dependencies] mcp`) is real. Only `server.py`/`__main__.py`
import `mcp`; every other module here (including all the tool-registration
modules) is importable without it, so unit tests for them don't need the extra —
see `tests/unit/test_mcp_cli_bridge.py`/`test_mcp_live_tools.py`.

## Desktop deployment (systemd --user)

Runs as a systemd **user** unit on the machine actually running Factorio (not a
container — the data is `$HOME/.factorio/script-output/flma`, owned by this same
user; a container would need a bind mount + uid alignment for zero isolation
benefit). Requires `loginctl enable-linger <user>` first, or the unit dies at
logout.

`~/.config/systemd/user/flma-mcp.service` (see the homelab repo's wiring below for
the values that must match): binds a **specific LAN interface** (never `0.0.0.0` —
this host may also be on a Tailscale tailnet with other people's nodes on it, and
`0.0.0.0` would expose the port there too; never loopback-only, since a remote
Hermes needs to reach it), a fixed port outside the range of every other service
already running on this desktop, `Restart=on-failure`, and `ExecStart` pointing at
the **prebuilt venv's** python directly (`uv run` wants to write into `.venv`/its
cache, which a hardened unit's `ProtectHome=read-only` blocks, and re-resolves the
lock on every start).

## Security

Two independent layers, since either one alone can fail silently:

1. **Bearer token** (`FLMA_MCP_TOKEN`) — required on every route except `/healthz`
   (which must stay open so it remains a pure liveness signal, not gated behind the
   same secret it's meant to help debug). If unset, the server logs a loud warning
   and runs open rather than refusing to start — convenient for local dev against
   `127.0.0.1`, never silent.
2. **Network-level restriction** (host firewall + the consuming cluster's
   NetworkPolicy) — this desktop's firewall may already have a broad port range
   open for other services; don't assume the token is redundant with it, and don't
   assume the firewall rule alone is sufficient either (an `ipBlock`/rich-rule
   ordering mistake is exactly the kind of thing that fails open, not closed).

## Homelab-side wiring (separate repo)

Hermes (the consumer) lives in `/home/jhjaggars/code/homelab`, not here. Its
`mcp_servers` config entry, the `NetworkPolicy` egress rule letting its pod reach
this desktop, and the SOPS-encrypted token live under that repo's
`clusters/homelab/hermes/`. Verify reachability from inside the cluster with two
separate checks — from any pod on the node Hermes runs on, and from Hermes' own pod
specifically — so a routing failure and a NetworkPolicy failure can't be confused
for each other.
