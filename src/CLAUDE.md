# src/ — the MCP bridge (a consumer)

A local Python MCP server (FastMCP over Streamable HTTP, `/mcp` on port 8080,
loopback-only by default) that reads one peer's local `script-output/flma/`
files — written by `../mod/`, formats documented in `../SCHEMA.md` — and serves
them as MCP tools. Pure consumer: never imports from `mod/`.

```bash
SCRIPT_OUTPUT_DIR=~/.factorio/script-output/flma make run   # from repo root
npx @modelcontextprotocol/inspector http://localhost:8080/mcp
```

Config is all env vars (`config.py`): `SCRIPT_OUTPUT_DIR`, `PORT`, `HOST`,
`LOG_LEVEL`, `MIN_REFRESH_INTERVAL_SECONDS`.

## Tools (`server.py`)

| Tool | Purpose |
|---|---|
| `get_research_status` | Current research, progress, queue (prefers the live `research.json`, falls back to `tech.json`) |
| `get_tech_tree` | Researched / available / locked technologies |
| `get_production_stats` | Item/fluid cumulative totals and per-minute rates |
| `get_logistics` | Logistic network contents and robot counts |
| `get_player_inventory` | A connected player's main inventory |
| `get_building_counts` | Placed-building counts by name/type |
| `query_buildings` | Filter placed buildings by name/type/surface/force, with positions |
| `get_snapshot_age` | Staleness (seconds) of each feed (including `buildings`, `research`, and `recipes` — the last via a bare `stat()`; the bridge never parses the ~11 MB `recipes.json`) — sanity-check the mod is running |

## File-reading model (`game_state.py`)

`SnapshotFile` re-reads a full JSON snapshot only when its mtime/size changes;
`BuildingIndex` tails `buildings.ndjson` by byte offset and detects mod-side
compaction both by size shrinking and by a leading-bytes fingerprint (catches a
same-or-larger-size rewrite too), replaying from scratch when either fires.
`GameState.refresh()` throttles disk hits to `MIN_REFRESH_INTERVAL_SECONDS`
regardless of tool-call burstiness, and holds a coarse lock across its whole
body so concurrent MCP tool calls (dispatched via `asyncio.to_thread`) can't
race on `BuildingIndex`'s byte offset.

`GameState` is constructed with `SCRIPT_OUTPUT_DIR` (the parent `flma/`
directory) but every data file actually lives one level deeper, under a
per-save `<save_id>` subdirectory the mod maintains (mod 0.3.1+, see
`../SCHEMA.md`). `GameState._resolve_active_dir()` reads the mod's
`current-save.json` pointer on every `refresh()`/`health_check()` and rebinds
all the `SnapshotFile`/`BuildingIndex` instances to `<base>/<save_id>` when it
changes — so pointing the bridge at a different save/server, or restarting the
mod's server against a new save, is picked up live without restarting the
bridge or touching `SCRIPT_OUTPUT_DIR`. No pointer yet (mod not enabled, or an
old mod version) falls back to treating `SCRIPT_OUTPUT_DIR` itself as the data
directory.

## Deployment

The bridge reads a **local** Factorio client's `script-output/`, so it runs as a
**local process on the machine playing the game** — there's no Containerfile or k8s
manifest. If it later needs to serve a remote/in-cluster agent, the likely path is
running Factorio as a headless server and sharing its `script-output` on a volume (or
switching the data layer to RCON) — `GameState`'s file-based interface was kept
narrow enough to swap out later.
