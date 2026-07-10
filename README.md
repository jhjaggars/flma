# flma — Factorio Live Agent

Ask an AI agent questions about your **running** Factorio game: what you're
researching, what your iron-plate rate is, what's buffered in your logistics
network, how many assemblers you've placed and where.

flma is two halves with a file-based contract between them:

- **The mod** (`mod/`) — runs inside Factorio and exports live game state as
  small JSON/NDJSON files under `script-output/flma/`. Built to cost ~nothing:
  no `on_tick` polling, engine-aggregated reads, event-driven building
  tracking, everything off by default.
- **The consumers** — anything that reads those files. This repo ships one: a
  local Python **CLI** (`planner/`, backed by the shared reading layer in
  `src/`) exposing both live-observe commands (research, production,
  logistics, inventory, buildings) and factory-planning commands. An agent
  drives it via `Bash` plus the `factorio-live`/`factory-planner` skills — no
  server process, no protocol handshake. The file formats are the whole
  interface, documented in [SCHEMA.md](SCHEMA.md) — you can build your own
  consumer against them without touching the mod.

```
Factorio (mod: event-driven + on_nth_tick exports)
   v  writes JSON/NDJSON             <- SCHEMA.md is the contract
~/.factorio/script-output/flma/
   v  read by
python -m planner <command>  -->  Claude / any agent that can run a shell
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

**3. Query it**, on the same machine as your Factorio client. No server to
start — each command is a one-shot read of the exported snapshot files.
Three ways to run the CLI, in increasing order of permanence:

```bash
# a) uvx, straight from GitHub -- no install, no clone, nothing left behind
uvx --from git+https://github.com/jhjaggars/flma flma-planner research
uvx --from git+https://github.com/jhjaggars/flma flma-planner production --kind items

# b) uv tool install -- puts `flma-planner` on PATH persistently (like pipx)
uv tool install git+https://github.com/jhjaggars/flma
flma-planner status   # feed staleness + modpack alignment

# c) from a clone, for development or to also get the mod/dev/ tooling
uv sync
uv run python -m planner research
```

`SCRIPT_OUTPUT_DIR` defaults to `~/.factorio/script-output/flma`; set it if
your Factorio config dir is elsewhere. Point Claude Code (or any agent that
can run a shell and read AgentSkills) at a clone of this repo and it picks up
the `factorio-live` and `factory-planner` skills, which teach it the full
command surface — the skills assume `uv run python -m planner ...` from a
checkout, since an agent working on/against this repo already has one.

The live-observe commands above (`research`, `tech-tree`, `production`,
`logistics`, `inventory`, `buildings`, `status`) work with just the mod
enabled — no other setup. The factory-planning commands (`plan`, `options`,
`recommend`, `tech`, …) additionally need a local
[recipe-mcp](https://github.com/jhjaggars/recipe-mcp) checkout with its
recipe DB built, regardless of how you installed flma itself — see
`planner/CLAUDE.md` for why (a filesystem-path import, not a package
dependency) and the setup steps.

**4. Ask questions.**

> "What am I researching and how far along is it?"
> "What's my iron plate production rate vs. consumption?"
> "How many logistic bots are idle on Nauvis?"
> "Where are my rocket silos?"

## Example session

The CLI is what an agent runs under the hood — here's what it looks like
run by hand:

```console
$ flma-planner status
recipes.db     : /home/jhjaggars/code/recipe-mcp/recipes.db  (312 technologies)
flma live data : /home/jhjaggars/.factorio/script-output/flma
  tech        : 3s ago
  production  : 3s ago
  logistics   : 8s ago
  buildings   : 41s ago
force 'player'   : 87 technologies known, current research: automation-3

modpack alignment: OK (309/312 live techs found in recipes.db)
`plan`/`have` live-scoping and netting are meaningful for this save.

next: `recommend <product>` for the single best way to make something right now; `plan <product> --rate <n>` to design a line; `have <item>` to check current production.

$ flma-planner research
force 'player' — current research: automation-3
  progress: 42.7%
  queue (2): automation-3, logistic-system

$ flma-planner production --kind items
force 'player', surface 'nauvis'

items (produced +/consumed -, per minute):
  copper-plate                                  +   842.0  -   790.5
  iron-plate                                    +  1200.0  - 1150.2

$ flma-planner plan electronic-circuit --rate 20
plan: electronic-circuit @ 1200.0/min (20.0/s)
machines: 24x assembling-machine-1
raw inputs: iron-plate (Iron plate) 600.0/min, copper-plate (Copper plate) 600.0/min
reuse candidates (production): copper-plate(842.0/min live)
flags: belts=approximate
```

## What the agent can see

| Command | Answers |
|---|---|
| `research` | current research, progress, queue |
| `tech-tree` | researched / available / locked technologies |
| `production` | item/fluid lifetime totals and live per-minute rates |
| `logistics` | logistic network contents, robot counts |
| `inventory` | a connected player's main inventory (opt-in) |
| `buildings` | placed-building counts by name/type, or filtered/listed with positions (opt-in) |
| `status --json` | staleness of each feed — is the mod actually running? |

Every command accepts `--json` for machine-readable output; see the
`factorio-live` skill for the full guide.

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
| `src/` | consumer | shared live-state file-reading layer, used by `planner/` |
| `planner/` | consumer | the CLI — live-observe commands (`observe.py`) plus the factory-planner (needs a sibling [recipe-mcp](https://github.com/jhjaggars/recipe-mcp) checkout, e.g. `~/code/recipe-mcp`) |
| `dev/` | mod dev | isolated local server+client environment for developing the mod |
| `.claude/skills/` | dev | Claude Code skills: `factorio-live` (live-observe commands), `factory-planner`, `factorio-dev` (mod development) |

## Development

```bash
uv sync --group dev
make quick     # lint + typecheck + tests
```

See [CLAUDE.md](CLAUDE.md) for architecture and design constraints, and
`dev/` (plus the `factorio-dev` skill) for the live-game dev environment.

## Status

Verified against a real running Factorio 2.0 client + save: mod loads
cleanly, settings toggle live, all CLI commands return real data, and the
buildings baseline scan time-slices as designed (a 26k-building base wrote
its baseline across ~59 ticks, no single-frame spike). Not yet exercised:
incremental add/remove events under real construction, and a
`show-time-usage` cost check.

## License

MIT
