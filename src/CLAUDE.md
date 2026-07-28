# src/ — the live-state reading layer (a consumer)

Not a server — a shared library. `game_state.py` reads one peer's local
`script-output/flma/` files (written by `../mod/`, formats documented in
`../SCHEMA.md`); `config.py` resolves `SCRIPT_OUTPUT_DIR` from the
environment. `planner/` is the direct consumer, via
`planner/live_state.open_game_state()` (`planner/observe.py` for the
research/production/logistics/inventory/buildings commands,
`planner/live_state.py` for the factory-planning netting/tech-scoping
commands); `../flma_mcp/` is an indirect one, entirely through `planner/`'s
own functions (never imports `game_state.py` directly). Pure consumer:
never imports from `mod/`.

Formerly this package also held `server.py`, an MCP server exposing the same
data as tools over Streamable HTTP — removed in favor of `planner`
subcommands + the `factorio-live` skill (see `.claude/skills/factorio-live/`
and `CLAUDE.md`'s architecture diagram), since the only consumer at the time
was Claude Code, which reaches CLI commands through `Bash` just as directly
as it would MCP tools. `../flma_mcp/` reintroduces an MCP server (as its own
package, not back in here) for the consumer that reasoning doesn't cover —
Hermes, which has no shell on this machine at all.

Config is one env var (`config.py`): `SCRIPT_OUTPUT_DIR`.

## File-reading model (`game_state.py`)

`SnapshotFile` re-reads a full JSON snapshot only when its mtime/size changes;
`BuildingIndex` tails `buildings.ndjson` by byte offset and detects mod-side
compaction both by size shrinking and by a leading-bytes fingerprint (catches a
same-or-larger-size rewrite too), replaying from scratch when either fires.
`GameState.refresh()` throttles disk hits to `min_refresh_interval` (a
constructor arg, default 0.5s) and holds a coarse lock across its whole body —
matters less for `planner`'s one-shot-CLI-process usage than it does for
`flma_mcp`, which holds one `GameState` open across concurrent requests for its
whole process lifetime (see `flma_mcp/state.py`) — exactly the concurrent-call
pattern this lock was originally written for.

`GameState` is constructed with `SCRIPT_OUTPUT_DIR` (the parent `flma/`
directory) but every data file actually lives one level deeper, under a
per-save `<save_id>` subdirectory the mod maintains (mod 0.3.1+, see
`../SCHEMA.md`). `GameState._resolve_active_dir()` reads the mod's
`current-save.json` pointer on every `refresh()`/`health_check()` and rebinds
all the `SnapshotFile`/`BuildingIndex` instances to `<base>/<save_id>` when it
changes — so pointing at a different save/server, or restarting the mod's
server against a new save, is picked up live without touching
`SCRIPT_OUTPUT_DIR`. No pointer yet (mod not enabled, or an old mod version)
falls back to treating `SCRIPT_OUTPUT_DIR` itself as the data directory.
