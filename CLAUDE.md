# flma

Exposes **live** Factorio game state — tech tree, production statistics, logistics
network contents, player inventories, placed buildings — via a local CLI, for an
agent to query while a game is running.

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
   |  read by
   v
python -m planner <command>  -->  Claude / agent (via Bash + the factorio-live skill)
```

**Why this shape, not RCON:** RCON needs *hosting* a game (dedicated server, or a
GUI-hosted MP game with local RCON enabled) — a pure client joining someone else's
server can't reach back into it. The mod's local file writes work in every
configuration (single-player, hosting, or joining) and require no network access.

**Why the mod runs on the server too:** Factorio multiplayer is deterministic
lockstep — control-stage mod code executes identically on every peer, and its
checksum is part of mod sync. A client can't unilaterally add exporter code; the
server operator installs the same mod. Each peer that wants live data just runs the
CLI against its own local `script-output/flma/`.

## Directory map

Each directory below has its own `CLAUDE.md` with the details; this is just the
routing table.

| Path | Role | Look inside for |
|---|---|---|
| `mod/` | producer | the Factorio mod (Lua): efficiency design rules, mod settings, what files it writes, in-game debugging/verification |
| `SCHEMA.md` | contract | authoritative format of every exported file — read this before writing or changing any consumer |
| `src/` | consumer | shared live-state file-reading layer (`game_state.py`): the snapshot/tail file-reading model, consumed by `planner/` |
| `planner/` | consumer | the CLI: factory-planning commands (`recipedb/` vendors the recipe-calculation engine, live-state netting, modpack-alignment caveats) and live-observe commands (`observe.py`) reading `src/game_state.py` directly |
| `dev/` | tooling | isolated local server+client for developing the mod; RCON access (guide: `.claude/skills/factorio-dev/SKILL.md`) |
| `tests/` | tests | pytest suite for the Python side (`make quick` runs it) |
| `.claude/skills/` | tooling | `factorio-dev` (dev environment workflow), `factory-planner` (planning commands), `factorio-live` (live-observe commands), `mod-release` (version bump + changelog + tag; CI/CD in `.github/workflows/` takes it from there) |

## Development

```bash
uv sync --group dev
make quick          # lint + typecheck + tests

# Package the mod for local install or the mod portal:
make mod-zip         # -> flma_<version>.zip; drop into ~/.factorio/mods/

# Point the CLI at wherever Factorio's script-output/flma actually is
# (default ~/.factorio/script-output/flma) and query live state:
SCRIPT_OUTPUT_DIR=~/.factorio/script-output/flma uv run python -m planner research

# Factory planner (see planner/CLAUDE.md) — build its recipe DB once, from
# flma's own live export (guaranteed to match the running save, see
# SCHEMA.md's `recipes.json` section). Resolves the export automatically,
# including the mod's per-save subdirectory.
make build-db
uv run python -m planner status
```
