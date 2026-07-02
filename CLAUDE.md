# flma

Exposes **live** Factorio game state — tech tree, production statistics, logistics
network contents, player inventories, placed buildings — over MCP, for a local agent
to query while a game is running.

## Architecture

Two components:

- **`mod/`** — the `flma` (Factorio Live MCP Agent) Factorio 2.0 mod. Runs as part of
  the synced mod set on the server *and* every client (it has a control stage, so its
  checksum must match — it cannot be a client-only mod). Writes JSON/NDJSON under
  `script-output/flma/` on **every peer's own machine**.
- **`src/`** — a local Python MCP bridge (FastMCP over Streamable HTTP) that reads
  one peer's local `script-output/flma/` and serves it as MCP tools.

```
Factorio (server + all clients, synced mod)
   |  control.lua: event-driven + on_nth_tick(N)
   |  helpers.write_file('flma/*.json'/'*.ndjson', ...)   -- no for_player filter;
   v                                                          every peer writes locally
~/.factorio/script-output/flma/        (local disk, this machine only)
   |  tailed by
   v
factorio-live-mcp bridge (FastMCP)  --MCP-->  Claude / agent
```

**Why this shape, not RCON:** RCON needs *hosting* a game (dedicated server, or a
GUI-hosted MP game with local RCON enabled) — a pure client joining someone else's
server can't reach back into it. The mod's local file writes work in every
configuration (single-player, hosting, or joining) and require no network access.

**Why the mod runs on the server too:** Factorio multiplayer is deterministic
lockstep — control-stage mod code executes identically on every peer, and its
checksum is part of mod sync. A client can't unilaterally add exporter code; the
server operator installs the same mod. Each peer that wants live data just runs the
bridge against its own local `script-output/flma/`.

## Efficiency — the core design constraint

Because the mod's code runs on the server for every player, not just the one running
the bridge, per-tick cost is shared by everyone. `control.lua` follows these rules
throughout (see its top-of-file comment for the full rationale):

1. No `on_tick` hook — only `script.on_nth_tick(N)` with a large, configurable `N`
   (`flma-tick-interval`, default 300 ticks / ~5s).
2. Tech/production/logistics/inventories use engine-aggregated reads
   (`LuaFlowStatistics`, `LuaLogisticNetwork:get_contents()`, force-level tables) —
   cost is O(#item types), not O(#entities) — and are overwritten as small full
   snapshots each cycle (cheap, no diffing needed).
3. Buildings are the one dataset proportional to base size. Instead of a scheduled
   `find_entities_filtered{}` scan, `flma` maintains an incremental index: one
   time-sliced baseline scan the first time it's enabled, then O(1) per build/mine
   event via filtered event handlers. The event log (`buildings.ndjson`) is
   periodically compacted from the in-memory index rather than re-scanned.
   - "Buildings" is a **blocklist by Factorio's built-in prototype `type`**, not by
     name — this is what makes it mod-agnostic: every mod's custom entities
     (including all of pyanodons') still have to declare one of the engine's fixed
     type categories, so a blocklist of types covers any mod's variants
     automatically. Besides non-placed entities (resources, trees, corpses, etc.),
     it also excludes high-cardinality "connective tissue" types — belts, pipes,
     poles, inserters, rails/signals — since those vastly outnumber actual
     production/logistics structures on any real base and aren't useful to track
     positionally. Mobile `unit`s and everything on the `enemy`/`neutral` forces
     (nests, worms, remnants) are excluded too — nothing non-factory is tracked.
   - The baseline scan's collecting phase iterates Factorio's own map chunk grid
     (32x32 tiles, via `surface.get_chunks()`), `BASELINE_CHUNKS_PER_TICK` chunks
     per tick, each queried with `find_entities_filtered{area=.., type=..,
     invert=true}` so the engine excludes non-buildings natively. This bounds each
     tick's cost by chunk density, not total base size — a megabase just takes
     more ticks, never a bigger single-tick spike. The draining phase then applies
     `BASELINE_CHUNK_SIZE` entities/tick to the in-memory index, and both the
     baseline dump and periodic compaction write in one batched `write_file` call
     rather than one syscall per building.
4. Everything is gated behind the synced `flma-export-enabled` runtime-global
   setting — disabled means zero registered handlers (verified via F4
   `show-time-usage`), not just an early-return inside a live handler.

**Debugging note:** mod-local `storage` is not readable from `/c` console commands
(those execute in the scenario's own separate storage scope, not the mod's) — use
the remote interface instead: `/c remote.call("flma", "status")` prints export
state and tracked-building count; `/c remote.call("flma", "reset_buildings")` clears
the index and forces a fresh baseline scan under current rules, without needing a
new save.

## Mod settings (Mod settings → Map, or `/c settings.global[...] = {value=...}`)

| Setting | Default | Purpose |
|---|---|---|
| `flma-export-enabled` | `false` | Master switch — gates every handler registration |
| `flma-tick-interval` | `300` | Ticks between production/logistics/inventory exports |
| `flma-export-inventories` | `false` | Player inventory contents are more sensitive than aggregate stats |
| `flma-export-buildings` | `false` | Triggers the one-time baseline scan; off by default |
| `flma-buildings-compact-threshold` | `20000` | Lines appended before `buildings.ndjson` is compacted |

## Files written (`script-output/flma/`)

| File | Written | Contents |
|---|---|---|
| `tech.json` | on research started/finished/queued/cancelled/reversed events (full overwrite) | per-force: current research, progress, queue, all technologies + prerequisites |
| `research.json` | every `flma-tick-interval` (full overwrite) | per-force: current research, progress, queue only (O(#forces), not the full tech table) — keeps `research_progress` live between the coarser research events that refresh `tech.json` |
| `production.json` | every `flma-tick-interval` (full overwrite) | per-force, per-surface item/fluid `input_counts`/`output_counts` (lifetime cumulative totals) **and** `input_rates_per_min`/`output_rates_per_min` (real per-minute flow, via `get_flow_count`) |
| `logistics.json` | every `flma-tick-interval` (full overwrite) | per-force logistic networks: contents, robot counts |
| `inventories.json` | every `flma-tick-interval`, if enabled (full overwrite) | connected players' main inventory contents |
| `buildings.ndjson` | on build/mine events (append), periodically compacted | `{"op":"add"/"remove", "entity":{...}}` event log |

## MCP Tools (`src/server.py`)

| Tool | Purpose |
|---|---|
| `get_research_status` | Current research, progress, queue (prefers the live `research.json`, falls back to `tech.json`) |
| `get_tech_tree` | Researched / available / locked technologies |
| `get_production_stats` | Item/fluid cumulative totals and per-minute rates |
| `get_logistics` | Logistic network contents and robot counts |
| `get_player_inventory` | A connected player's main inventory |
| `get_building_counts` | Placed-building counts by name/type |
| `query_buildings` | Filter placed buildings by name/type/surface/force, with positions |
| `get_snapshot_age` | Staleness (seconds) of each feed (including `buildings` and `research`) — sanity-check the mod is running |

`src/game_state.py` owns the file-reading model: `SnapshotFile` re-reads a full JSON
snapshot only when its mtime/size changes; `BuildingIndex` tails `buildings.ndjson`
by byte offset and detects mod-side compaction both by size shrinking and by a
leading-bytes fingerprint (catches a same-or-larger-size rewrite too), replaying
from scratch when either fires. `GameState.refresh()` throttles disk hits to
`MIN_REFRESH_INTERVAL_SECONDS` regardless of tool-call burstiness, and holds a
coarse lock across its whole body so concurrent MCP tool calls (dispatched via
`asyncio.to_thread`) can't race on `BuildingIndex`'s byte offset.

## Factory planner (`planner/`)

A local CLI — **no MCP server, no Hermes** — that answers "how do I build a
production line for X at rate Y, and what am I already producing toward it?"
by combining this repo's live game state with `recipe-mcp`'s static
recipe/machine data (`~/code/homelab/apps/recipe-mcp`, a sibling project —
its `recipes.json` is a dump from the **RecipeExporter** Factorio mod).

The heavy arithmetic (recipe-chain expansion, batches → machine counts,
raw-input rollup) is **not reimplemented here** — it's recipe-mcp's own
`engine.plan_product`/`engine._expand_node` (extracted from its MCP-tool
`server.py` into a plain, FastMCP-independent `engine.py` specifically so
both the MCP server and this CLI call the identical, already-tested code).
`planner/` only adds what didn't exist anywhere: live-production netting,
buffered-logistics-stock lookup, tech-scoping from the live save, and belt/pipe
count constants (recipes.json has no throughput data at all).

```bash
uv run python -m planner status                          # health check (also the no-arg default)
uv run python -m planner plan "processing unit" --rate 10 # rate is items/sec by default
uv run python -m planner have iron-plate                  # what am I already producing/storing?
```

Config: `RECIPE_MCP_DIR` (default `~/code/homelab/apps/recipe-mcp`),
`RECIPES_DB` (default `$RECIPE_MCP_DIR/recipes.db` — build once via
`cd $RECIPE_MCP_DIR && make build-db`). Reuses this repo's own
`SCRIPT_OUTPUT_DIR` for live state.

**Modpack alignment matters.** The committed recipe dump is a **Pyanodons**
game; this machine's live save may be running a different modpack (Space Age,
confirmed, at time of writing) — in which case live tech-scoping and
production-netting correctly report "no match" rather than silently mixing
data across incompatible games. See `.claude/skills/factory-planner/SKILL.md`
for the full workflow guide and caveats (recipe-selection quirks, belt-count
accuracy, etc.).

## Deployment

The bridge reads a **local** Factorio client's `script-output/`, so it runs as a
**local process on the machine playing the game** — there's no Containerfile or k8s
manifest. If it later needs to serve a remote/in-cluster agent, the likely path is
running Factorio as a headless server and sharing its `script-output` on a volume (or
switching the data layer to RCON) — `GameState`'s file-based interface was kept
narrow enough to swap out later.

## Development

```bash
uv sync --group dev
make quick          # lint + typecheck + tests

# Point the bridge at wherever Factorio's script-output/flma actually is:
SCRIPT_OUTPUT_DIR=~/.factorio/script-output/flma make run

# Package the mod for local install or the mod portal:
make mod-zip         # -> flma_<version>.zip; drop into ~/.factorio/mods/

# Test with MCP inspector
npx @modelcontextprotocol/inspector http://localhost:8080/mcp

# Factory planner (see "Factory planner" section above) — build its recipe DB once:
cd ~/code/homelab/apps/recipe-mcp && make build-db
uv run python -m planner status
```

## Verifying in-game

Confirmed working against a real running client (mod checksum loads clean, settings
toggle live via `on_runtime_mod_setting_changed`, all 8 MCP tools return real data
from a live save):

1. Copy `mod/` into `~/.factorio/mods/flma_<version>/` (or `make mod-zip` and use the
   in-game mod manager), enable it, start/load a save.
2. Enable the map setting `flma-export-enabled` (Mod settings → Map). Confirm
   `script-output/flma/tech.json` appears with a non-empty `forces.player` entry.
3. Research a technology; confirm `tech.json` updates without waiting a full
   `flma-tick-interval`.
4. Run the bridge (`make run` with `SCRIPT_OUTPUT_DIR` pointed at that
   `script-output/flma`) and exercise each tool via the MCP inspector or a client;
   confirm `get_snapshot_age` tracks the live game.

Still open (not yet exercised against a real base with construction happening):

5. Enable `flma-export-buildings`; confirm `buildings.ndjson` gets a burst of `add`
   events (the baseline scan) spread across a few ticks, not one frame spike.
   Build/mine something; confirm exactly one `add`/`remove` line appears.
6. Check cost: F4 → `show-time-usage`, toggle `flma-export-enabled` off/on and
   confirm the mod's line drops to ~0 when disabled.
