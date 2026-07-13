# planner/ — factory-planner CLI (a consumer)

A local CLI — **no MCP server, no Hermes** — that answers "how do I build a
production line for X at rate Y, and what am I already producing toward it?"
by combining this repo's live game state with `planner/recipedb/`'s static
recipe/machine data (a `recipes.json` dump the flma mod itself exports, in
the **RecipeExporter** format — see `../SCHEMA.md`).

The heavy arithmetic (recipe-chain expansion, batches → machine counts,
raw-input rollup) is **not reimplemented here** — it's `planner/recipedb/
engine.py`'s `plan_product`/`_expand_node`, vendored from
[recipe-mcp](https://github.com/jhjaggars/recipe-mcp) (a standalone project
by the same author — its MCP server originally extracted this exact code
from its own `server.py` into a plain, FastMCP-independent `engine.py`;
flma now vendors that module directly rather than importing it from a
sibling checkout at runtime, so this repo is fully self-contained — see
`planner/recipedb/__init__.py` for provenance). `planner/` only adds what
didn't exist anywhere: live-production netting,
buffered-logistics-stock lookup, tech-scoping from the live save, belt/pipe
count constants (recipes.json has no throughput data at all),
module-accelerated crafting-speed assumptions for specific building
families the recipe DB has no module-bonus data for at all (see
`planner/module_bonus.py`), a `plan --cap` mode that solves for the output
rate keeping the worst raw input within a belt budget instead of picking an
arbitrary rate first, a reuse-before-build report — `plan` cross-references the recipe chain's
intermediate items and machine types against live production/buffered stock
and `buildings.ndjson` counts (mod 0.3.5+ additionally cross-references
`buildings.ndjson`'s per-machine `recipe` field, via
`live_state.buildings_by_recipe`, to report machines already configured for
one of the plan's exact recipes — a strictly stronger signal than merely
owning the right machine type; `producers`/`consumers` tag candidate
recipes with `[N built]` the same way), and prints what already exists so
you're asked "reuse this?" instead of the tool silently assuming a
from-scratch build — and `options`/`planner/options.py`, a presentation
layer over data the engine already computes but every other caller
discards:
`_expand_node`'s `alternates_map` (every candidate recipe per item, tagged
available/tech_locked/excluded/selected) and `_pick_producer`'s
`main_product` signal. `options` classifies each candidate as
byproduct/impractical or not (`options.classify_producer`) and expands each
viable one (forcing it via the same `--recipe` override mechanism `plan`
already exposes) to build a side-by-side menu — no new chain-selection or
yield math, just reframing engine output as a decision menu instead of a
single auto-picked answer.

`planner/techbundle.py` (behind the `tech` command) fills a real gap in the
engine's own math, not just a presentation gap: `_pick_producer`/
`_expand_node` pick exactly one recipe per item and only look at the single
product row matching what was requested — every OTHER product a chosen
recipe yields (a Factorio recipe can have several joint outputs in one
batch) is silently discarded, never credited against demand elsewhere. Some
Pyanodons recipes are only economical in combination (e.g. a "screener" that
yields two ore grades at once, plus a separate "crusher" that converts the
lower grade into more of the higher one — run both and route the crusher's
input from the screener's own byproduct, and raw-ore use drops ~20% versus
either recipe alone). `techbundle.py` detects this by building a small
dependency graph among ONLY the recipes one Factorio technology unlocks
together (not the whole recipe graph — a whole-graph attempt was explored
and abandoned: from any single item it reaches ~4159 of ~4160 total
Pyanodons recipes, essentially the whole modpack) and solves the exact
batch-rate blend that closes the loop with zero waste, via a small
hand-written exact-`Fraction` linear solver (no scipy/pulp dependency — the
bundles are small by construction, median 4 recipes/tech).

`planner/recommend.py` (behind the `recommend` command) is the synthesis
layer that closes the loop between the two: `options` alone doesn't reveal
that one of its single-recipe candidates is actually the cheaper half of a
`tech` bundle (you have to separately notice its unlocking tech and check).
`cmd_recommend` reuses `options`' own candidate classification+expansion
(`_classify_and_expand_candidates`, extracted out of `cmd_options` so both
share it — refactor is behavior-preserving, `options`' output is unchanged),
then for every currently-researched candidate looks up its unlocking tech
and re-checks it through `techbundle` — swapping in the bundle-solved raw
cost wherever one applies — before ranking (`recommend.rank_candidates`, a
deliberately narrow heuristic: usable-now beats locked, fewer distinct raw
types beats more, lower total quantity beats higher, fewer stages breaks
ties — see that module's docstring for why this isn't a general optimizer).
No new engine or DB math — just correctly wiring `options` + `techbundle`
together instead of leaving that cross-reference to whoever's driving the
CLI. One caveat discovered while building this: `plan --recipe a=x,b=y`
CANNOT reproduce a bundle's blended answer — forcing overrides down a chain
still picks one full recipe per item, so a "combo" recommendation's numbers
(machine counts, batch rates) are already the actionable build; there's no
further `plan` invocation that gets you the same thing.

```bash
uv run python -m planner build-db                         # build recipes.db from the live save's own recipes.json export
uv run python -m planner status                          # health check (also the no-arg default)
uv run python -m planner recommend copper-plate           # the single best plan for your tech level -- start here
uv run python -m planner options copper-plate             # viable ways to make X, before committing to a chain
uv run python -m planner tech "Copper processing - Stage 1" # what a tech unlocks & whether it's a recycling bundle
uv run python -m planner plan "processing unit" --rate 10 # rate is items/sec by default
uv run python -m planner plan sand                        # no --rate: sizes for 1x the top-level machine instead
uv run python -m planner plan sand --recipe sand=gravel-to-sand # force a specific recipe when the auto-pick is wrong
uv run python -m planner plan battery-mk01 --cap 1        # solve for the rate where the worst raw input needs 1 belt
uv run python -m planner plan ore-lead --belts 1           # size drills/machines for N belts of the item itself
uv run python -m planner have iron-plate                  # what am I already producing/storing?
uv run python -m planner recipe sand-01 sand-02 sand-03    # compare several recipes in one call
uv run python -m planner belts 2                          # N belts -> achievable rate, for `plan --rate`
```

**Live-observe commands** (`planner/observe.py`, behind the `research` /
`tech-tree` / `production` / `logistics` / `inventory` / `buildings`
subcommands) read the running game's state directly — no recipe-chain math,
no DB involved. These replaced `src/server.py`'s former MCP tools of the
same shapes (`get_research_status`, `get_tech_tree`, `get_production_stats`,
`get_logistics`, `get_player_inventory`, `get_building_counts`/
`query_buildings`) when the MCP bridge was removed in favor of a single CLI +
skills, since the only consumer has ever been Claude Code. See
`.claude/skills/factorio-live/SKILL.md` for the workflow guide; every command
accepts `--json` for machine-readable output.

Config: `RECIPES_DB` (default `$SCRIPT_OUTPUT_DIR/recipes.db` — build once via
`make build-db`, or `uv run python -m planner build-db`). Reuses this repo's
own `SCRIPT_OUTPUT_DIR` for live state.

**Modpack alignment matters — and is fully solvable in-repo now.** The
recipe DB only matches the live save if it was built from the same modpack.
Since mod 0.3.0 the flma mod itself exports a RecipeExporter-compatible
`recipes.json` from the running game (see `../SCHEMA.md`); `build-db`
resolves that live export automatically (it reads the mod's own
`current-save.json` pointer the same way every other live-observe command
does, via `live_state.open_game_state(...).recipes_path` — handles the
per-save `<save_id>` subdirectory mod 0.3.1+ uses, see `src/game_state.py`),
so the aligned workflow is just:

```bash
make build-db
```

`--recipes-json`/`--recipes-db` (on `planner build-db`) override either path
— e.g. to build from a foreign/committed dump instead. If the DB comes from
a stale/foreign export (a different modpack or an old save), `status`'s
alignment check correctly reports "no match" rather than silently mixing
data across incompatible games. See `.claude/skills/factory-planner/SKILL.md`
for the full workflow guide and caveats.

## Architecture notes (only if something needs debugging)

- `planner/recipedb/` vendors recipe-mcp's `engine.py`/`db.py`/`build_db.py`
  verbatim (see `planner/recipedb/__init__.py` for provenance) — no external
  checkout, no `importlib` aliasing trick needed anymore; `planner/cli.py`
  imports them as ordinary submodules (`from planner.recipedb import
  engine`).
- `planner/live_state.py` computes net production (output − input, summed
  across surfaces) and buffered logistics stock — this math doesn't exist
  anywhere else; `planner/observe.py`'s `production_stats` (the `production`
  command) only passes the raw per-surface counts through untouched.
- `planner/recipedb/engine.py` was originally extracted from recipe-mcp's
  `server.py` specifically so both its MCP server and this CLI could call the
  *same* calculation code — `plan_product`/`_expand_node` are that single
  source of truth; `planner/cli.py` is the only caller now.
