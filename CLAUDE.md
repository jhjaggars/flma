# flma

Exposes **live** Factorio game state — tech tree, production statistics, logistics
network contents, player inventories, placed buildings — over MCP, for a local agent
to query while a game is running.

## Architecture

The repo is deliberately split into a **producer** and its **consumers**, with the
exported file formats as the only interface between them — **`SCHEMA.md` documents
that contract** (exact shape of every file, serialization quirks, compaction
semantics) and is what a new consumer or an agent should read first. Kept as one
repo for now, but the seam is clean enough to split later (the Python side never
imports from `mod/`; only the files couple them).

```
Factorio (server + all clients, synced mod)
   |  control.lua: event-driven + on_nth_tick(N)
   |  helpers.write_file('flma/*.json'/'*.ndjson', ...)   -- no for_player filter;
   v                                                          every peer writes locally
~/.factorio/script-output/flma/        (local disk, this machine only)
   |  tailed by
   v
flma bridge (FastMCP)  --MCP-->  Claude / agent
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

## Directory map

Each directory below has its own `CLAUDE.md` with the details; this is just the
routing table.

| Path | Role | Look inside for |
|---|---|---|
| `mod/` | producer | the Factorio mod (Lua): efficiency design rules, mod settings, what files it writes, in-game debugging/verification |
| `SCHEMA.md` | contract | authoritative format of every exported file — read this before writing or changing any consumer |
| `src/` | consumer | the MCP bridge: tool definitions, the snapshot/tail file-reading model, deployment shape |
| `planner/` | consumer | factory-planner CLI: recipe-mcp integration, live-state netting, modpack-alignment caveats |
| `dev/` | tooling | isolated local server+client for developing the mod; RCON access (guide: `.claude/skills/factorio-dev/SKILL.md`) |
| `tests/` | tests | pytest suite for the Python side (`make quick` runs it) |
| `.claude/skills/` | tooling | `factorio-dev` (dev environment workflow) and `factory-planner` (planner workflow) |

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

# Factory planner (see planner/CLAUDE.md) — build its recipe DB once:
cd ~/code/homelab/apps/recipe-mcp && make build-db
uv run python -m planner status
```
