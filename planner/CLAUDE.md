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
accuracy, the `src`-package import-aliasing trick in `_recipe_mcp_loader.py`,
etc.).
