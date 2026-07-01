# flma — Factorio Live MCP Agent

A Factorio 2.0 mod + local MCP bridge that exposes **live** game state — tech tree,
production statistics, logistics network contents, player inventories, placed
buildings — so an AI agent (Claude, or anything else speaking MCP) can query what's
happening in a running game.

```
Factorio (server + all clients, synced mod)
   |  control.lua: event-driven + on_nth_tick(N)
   |  helpers.write_file('flma/*.json'/'*.ndjson', ...)
   v
~/.factorio/script-output/flma/        (local disk, this machine only)
   |  tailed by
   v
flma bridge (FastMCP)  --MCP-->  Claude / agent
```

See [CLAUDE.md](CLAUDE.md) for full architecture, efficiency design constraints, mod
settings, file formats, and the MCP tool reference.

## Why a mod, not RCON?

RCON needs *hosting* a game (dedicated server, or a GUI-hosted MP game with local
RCON enabled) — a pure client joining someone else's server can't reach back into it.
The mod's local file writes work in every configuration (single-player, hosting, or
joining) and require no network access. Because Factorio multiplayer is deterministic
lockstep, the mod's control-stage code runs identically on every peer — so it must be
installed on the server too, not just your own client.

## Efficiency

The mod's per-tick cost is shared by the whole server, so it's built to cost as close
to nothing as possible when idle:

- No `on_tick` — only `script.on_nth_tick(N)` with a large, configurable interval.
- Tech/production/logistics/inventories use engine-aggregated reads
  (`LuaFlowStatistics`, `LuaLogisticNetwork:get_contents()`), not entity scans.
- Buildings are tracked via an incrementally-maintained index (one time-sliced
  baseline scan when first enabled, then O(1) per build/mine event) instead of a
  scheduled `find_entities_filtered{}` scan.
- Everything is gated behind a single synced `flma-export-enabled` setting —
  disabled means zero registered event handlers.

## Quick start

```bash
# Install the mod
make mod-zip                              # -> flma_<version>.zip
# unzip/copy into ~/.factorio/mods/, or symlink mod/ for live development

# In-game: Mod settings -> Map -> enable "flma-export-enabled"

# Run the bridge, pointed at your local script-output
uv sync --group dev
SCRIPT_OUTPUT_DIR=~/.factorio/script-output/flma make run

# Try it with the MCP inspector
npx @modelcontextprotocol/inspector http://localhost:8080/mcp
```

## Development

```bash
uv sync --group dev
make quick     # lint + typecheck + tests
```

## Status

Verified against a real running Factorio 2.0 client + save: mod loads cleanly,
settings toggle live, and all MCP tools return real data (tech tree, research
status, production stats, logistics, player inventory, building counts/queries).
Not yet exercised: building add/remove events under real construction, and a
`show-time-usage` cost check.

## License

MIT
