# flma — Factorio Live MCP Agent

Ask an AI agent questions about your **running** Factorio game: what you're
researching, what your iron-plate rate is, what's buffered in your logistics
network, how many assemblers you've placed and where.

flma is two halves with a file-based contract between them:

- **The mod** (`mod/`) — runs inside Factorio and exports live game state as
  small JSON/NDJSON files under `script-output/flma/`. Built to cost ~nothing:
  no `on_tick` polling, engine-aggregated reads, event-driven building
  tracking, everything off by default.
- **The consumers** — anything that reads those files. This repo ships two: a
  local **MCP bridge** (`src/`) that serves the data as MCP tools for any
  agent, and a **factory-planner CLI** (`planner/`). The file formats are the
  whole interface, documented in [SCHEMA.md](SCHEMA.md) — you can build your
  own consumer against them without touching the mod.

```
Factorio (mod: event-driven + on_nth_tick exports)
   v  writes JSON/NDJSON             <- SCHEMA.md is the contract
~/.factorio/script-output/flma/
   v  read by
MCP bridge (src/)  --MCP over HTTP-->  Claude / any MCP client
```

## Using it

**1. Install the mod.** Build the zip and drop it in your mods folder (or
install `flma` from the mod portal, once published):

```bash
make mod-zip                    # -> flma_<version>.zip
cp flma_*.zip ~/.factorio/mods/
```

In multiplayer the mod must be installed on the server too (it's a synced
control-stage mod) — but then *every* player who runs it gets their own local
data export.

**2. Turn on exporting in-game.** Mod settings → Map → enable
`flma-export-enabled`. That's the master switch; optionally also enable
`flma-export-buildings` (placed-building tracking) and
`flma-export-inventories` (player inventories, off by default for privacy).
Confirm `~/.factorio/script-output/flma/tech.json` appears.

**3. Run the bridge** on the same machine as your Factorio client:

```bash
uv sync
SCRIPT_OUTPUT_DIR=~/.factorio/script-output/flma make run
# serves MCP at http://127.0.0.1:8080/mcp  (loopback-only by default)
```

**4. Connect your agent.** For Claude Code:

```bash
claude mcp add --transport http factorio http://localhost:8080/mcp
```

Any MCP client works the same way — the tools are self-describing. To poke at
them without an agent: `npx @modelcontextprotocol/inspector http://localhost:8080/mcp`.

**5. Ask questions.**

> "What am I researching and how far along is it?"
> "What's my iron plate production rate vs. consumption?"
> "How many logistic bots are idle on Nauvis?"
> "Where are my rocket silos?"

## What the agent can see

| Tool | Answers |
|---|---|
| `get_research_status` | current research, progress, queue |
| `get_tech_tree` | researched / available / locked technologies |
| `get_production_stats` | item/fluid lifetime totals and live per-minute rates |
| `get_logistics` | logistic network contents, robot counts |
| `get_player_inventory` | a connected player's main inventory (opt-in) |
| `get_building_counts` | placed-building counts by name/type (opt-in) |
| `query_buildings` | buildings filtered by name/type/surface/force, with positions |
| `get_snapshot_age` | staleness of each feed — is the mod actually running? |

## Mod settings (Mod settings → Map)

| Setting | Default | Purpose |
|---|---|---|
| `flma-export-enabled` | `false` | Master switch — off means zero registered handlers |
| `flma-tick-interval` | `300` (~5s) | Ticks between scheduled exports |
| `flma-export-inventories` | `false` | Player inventory contents (more sensitive) |
| `flma-export-buildings` | `false` | Building tracking (one-time baseline scan on enable) |
| `flma-buildings-compact-threshold` | `20000` | Event-log lines before compaction |

## Why a mod writing files, not RCON?

RCON requires *hosting* the game — a client joining someone else's server
can't reach back into it. Local file export works in every configuration
(single-player, hosting, or joining) with no network access. And because
Factorio multiplayer is deterministic lockstep, the mod's per-tick cost runs
on every peer — which is why it's engineered to be near-zero: no `on_tick`,
engine-aggregated reads instead of entity scans, an incremental building
index instead of scheduled `find_entities_filtered` sweeps, and full teardown
of all handlers when disabled. Details in [CLAUDE.md](CLAUDE.md).

## Repo layout

| Path | Half | What |
|---|---|---|
| `mod/` | producer | the Factorio mod — self-contained, Lua only |
| `SCHEMA.md` | contract | exact format of every exported file |
| `src/` | consumer | the MCP bridge (FastMCP, Streamable HTTP) |
| `planner/` | consumer | factory-planner CLI (needs a sibling [recipe-mcp](../homelab/apps/recipe-mcp) checkout) |
| `dev/` | mod dev | isolated local server+client environment for developing the mod |
| `.claude/skills/` | dev | Claude Code skills for mod development and the planner |

## Development

```bash
uv sync --group dev
make quick     # lint + typecheck + tests
```

See [CLAUDE.md](CLAUDE.md) for architecture and design constraints, and
`dev/` (plus the `factorio-dev` skill) for the live-game dev environment.

## Status

Verified against a real running Factorio 2.0 client + save: mod loads
cleanly, settings toggle live, all MCP tools return real data, and the
buildings baseline scan time-slices as designed (a 26k-building base wrote
its baseline across ~59 ticks, no single-frame spike). Not yet exercised:
incremental add/remove events under real construction, and a
`show-time-usage` cost check.

## License

MIT
