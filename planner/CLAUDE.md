# planner/ — factory-planner CLI (a consumer)

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
buffered-logistics-stock lookup, tech-scoping from the live save, belt/pipe
count constants (recipes.json has no throughput data at all), and a
reuse-before-build report — `plan` cross-references the recipe chain's
intermediate items and machine types against live production/buffered stock
and `buildings.ndjson` counts, and prints what already exists so you're
asked "reuse this?" instead of the tool silently assuming a from-scratch
build.

```bash
uv run python -m planner status                          # health check (also the no-arg default)
uv run python -m planner plan "processing unit" --rate 10 # rate is items/sec by default
uv run python -m planner plan sand --recipe sand=gravel-to-sand # force a specific recipe when the auto-pick is wrong
uv run python -m planner have iron-plate                  # what am I already producing/storing?
```

Config: `RECIPE_MCP_DIR` (default `~/code/homelab/apps/recipe-mcp`),
`RECIPES_DB` (default `$RECIPE_MCP_DIR/recipes.db` — build once via
`cd $RECIPE_MCP_DIR && make build-db`). Reuses this repo's own
`SCRIPT_OUTPUT_DIR` for live state.

**Modpack alignment matters — and is now solvable at the source.** The
recipe DB only matches the live save if it was built from the same modpack.
As of mod 0.3.0 the flma mod itself exports a RecipeExporter-compatible
`recipes.json` from the running game (see `../SCHEMA.md`), so the aligned
workflow is to build the DB from the live export:

```bash
cd ~/code/homelab/apps/recipe-mcp && \
  uv run python -m src.build_db ~/.factorio/script-output/flma/recipes.json recipes.db
```

(or build to a separate path and point `RECIPES_DB` at it). If instead the DB
comes from a stale/foreign dump (e.g. recipe-mcp's committed Pyanodons
`recipes.json` while the live save runs Space Age), live tech-scoping and
production-netting correctly report "no match" rather than silently mixing
data across incompatible games. See `.claude/skills/factory-planner/SKILL.md`
for the full workflow guide and caveats.

## Architecture notes (only if something needs debugging)

- `planner/_recipe_mcp_loader.py` imports recipe-mcp's `src/engine.py` under
  the alias `recipe_mcp_src` (via `importlib`) rather than a normal
  `import src.engine` — both projects name their package `src`, so a plain
  import would silently resolve to whichever one Python already cached.
- `planner/live_state.py` computes net production (output − input, summed
  across surfaces) and buffered logistics stock — this math doesn't exist
  anywhere else; `src/server.py`'s `get_production_stats` only passes the
  raw per-surface counts through untouched.
- `engine.py` in recipe-mcp was extracted from `server.py` specifically so
  both the MCP server and this CLI call the *same* calculation code — see
  that repo's `server.py` module docstring and `plan_factory`'s thin-wrapper
  body for how they stay in sync.
