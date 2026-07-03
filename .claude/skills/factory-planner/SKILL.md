---
name: factory-planner
description: Design Factorio production lines (machine counts, raw inputs, belt counts) using recipe-mcp's calculation engine, cross-referenced against flma's live game state — no MCP server, no Hermes, just a local CLI.
---

# flma factory planner

## Purpose

Answers "I want to make N/sec of some product — how many machines, what raw
inputs, how many belts, and what am I already producing toward it?" via a
local CLI (`planner/`) — no MCP server, no Hermes, no hand-computed
recipe-chain math (that's baked into `recipe-mcp`'s tested engine, called
directly).

## When to use this skill

- The user asks to design/plan a production line for a target rate ("how do
  I make N/sec of X", "how many assemblers do I need for Y").
- The user asks what they're already producing/consuming/storing for a
  specific item (tap into existing supply instead of building new).
- The user asks for a recipe's ingredients/products, what produces/consumes
  an item, or wants the full bill-of-materials tree for something.
- Before any of the above: if `uv run python -m planner status` hasn't been
  run yet this session, run it first — it reports whether live game state
  and the recipe data actually describe the same modpack (see caveat below).

## Running it

From this repo's root:

```bash
uv run python -m planner status                              # health check (also the no-args default)
uv run python -m planner plan "processing unit" --rate 10     # rate defaults to items/sec
uv run python -m planner plan iron-plate --rate 2 --stop-items iron-ore
uv run python -m planner plan sand --rate 1 --recipe sand=gravel-to-sand  # force a specific recipe
uv run python -m planner expand iron-plate --rate 2 --stop-items iron-ore
uv run python -m planner recipe electronic-circuit
uv run python -m planner producers iron-ore
uv run python -m planner consumers iron-plate
uv run python -m planner have iron-plate
```

`uv run flma-planner ...` (the installed console script) works identically.
Every subcommand exits non-zero on error/ambiguity and prints candidates or a
next-step hint rather than a bare stack trace or empty output. `plan`
defaults to a one-line-per-section summary (same data, cheaper to read) —
add `--full` when something in it looks wrong and you need the per-row
breakdown to see why.

## Common workflows

**"Design a line for X at rate Y."** Run `plan <product> --rate <n>`. Read
the `machines`/`raw inputs`/`drills` sections back to the user directly —
don't recompute or sanity-check the arithmetic by hand, that's exactly what
this tool exists to avoid. Do sanity-check *which recipe chain* was used
(see the Pyanodons caveat below) if the raw inputs look like an unrelated
ingredient chain (e.g. asking for iron-plate and getting acetone/propene).

**"What am I already making of X?"** Run `have <item>` for a quick net
production + buffered-stock check, or look at the "already have" annotations
`plan` prints next to each raw input.

**Before treating a plan's raw inputs as all-new-build.** `plan` also prints
"existing production" (intermediate items in the chain, not just flattened
raw inputs) and "existing buildings" (live counts of machine types the plan
calls for) whenever nonzero — read it and **ask the user** which reuse
opportunities to apply before recommending new capacity. The tool
deliberately doesn't net these out itself (duty cycle, backlog, and whether
capacity is already spoken for are judgment calls).

**"What's the full ingredient tree, not just the summary?"** Run `expand
<product> --rate <n>` for the nested BOM instead of `plan`'s flattened
machine bill.

**"I don't know the exact item id."** `producers`/`consumers`/`recipe` do
fuzzy/exact lookups against the `names`/`recipes` tables and will print
candidates if ambiguous — resolve the id there before calling `plan`/`expand`
with it (they also fuzzy-resolve internally, but exact ids avoid ambiguity
prompts).

## Known caveats (don't let these surprise you mid-conversation)

1. **Modpack alignment.** The committed recipe dump defaults to Pyanodons;
   the live save may run a different modpack. `status` reports whether they
   match — when they don't, `plan`/`have` still run but tech-scoping/netting
   are skipped rather than silently wrong. Fix: rebuild the DB from the
   mod's own live export — `cd $RECIPE_MCP_DIR && uv run python -m
   src.build_db ~/.factorio/script-output/flma/recipes.json recipes.db`.
2. **Recipe auto-pick can still be a surprising tie.** The picker prefers a
   recipe whose `main_product` is the item you asked for (fixes low-probability
   byproduct recipes winning by alphabetical accident), but when *multiple*
   recipes legitimately have the item as their main product, it's still
   alphabetical among them — not necessarily the cheapest chain. If
   `raw_inputs` look wrong or needlessly expensive: `expand` to see the
   actual chain, `producers <item>` to see candidates (tagged `[main
   product]`), `--recipe item=recipe_id` to force one (comma-separated for
   multiple).
3. **`plan`/`expand` filter by your actual research, live** (when aligned) —
   a tech-locked-only producer falls back to a raw input
   (`tech-locked (falling back to raw input ...)` note); a tech-locked drill
   for an item with another eligible drill shows under `blocked drills`
   instead of vanishing. The DB's own `enabled`/`researched` columns are
   stale (built near game start) — live `tech.json` drives this instead.
4. **Mining can have more than one extraction path** — `drills` lists every
   currently-buildable one (tagged `[resource_category]` when >1), including
   fluid raw inputs (e.g. geothermal-water → Geothermal plant). Required-fluid
   cost for fluid-fed patches is shown inline, not folded into `raw inputs`.
5. **Belt/pipe throughput is a placeholder** (no throughput data in the
   RecipeExporter dump) — treat belt counts as order-of-magnitude.
6. **Drill/raw estimates ignore modules, productivity, and ore purity.**
7. **Offshore-pump-style "free" fluids (`water`, `pressured-water`) aren't in
   the recipe DB at all** — no resource-patch row, no drill. The engine can
   invent a weird synthetic chain to "produce" one instead (e.g. a fake
   soil/mud cycle). If you know it's free/pumped in your game, pass it via
   `--stop-items`.

## Configuration

Two env vars (both optional, sensible defaults):

- `RECIPE_MCP_DIR` — path to the `recipe-mcp` checkout. Default:
  `~/code/homelab/apps/recipe-mcp`.
- `RECIPES_DB` — path to its built `recipes.db`. Default:
  `$RECIPE_MCP_DIR/recipes.db`. Not committed to either repo — build it with
  `cd $RECIPE_MCP_DIR && make build-db` before first use (needs
  `recipes.json` to already be present there).

`SCRIPT_OUTPUT_DIR` (flma's own existing env var) controls where live
snapshots are read from, same as the MCP bridge.

Debugging internals (import-aliasing trick, where the netting math lives,
how the CLI and MCP server share engine code) live in `planner/CLAUDE.md`,
not here — not needed for normal use of this skill.
