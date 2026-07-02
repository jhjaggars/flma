---
name: factory-planner
description: Design Factorio production lines (machine counts, raw inputs, belt counts) using recipe-mcp's calculation engine, cross-referenced against flma's live game state — no MCP server, no Hermes, just a local CLI.
---

# flma factory planner

## Purpose

Answers "I want to make N/sec of some product — how many machines do I need,
what raw inputs do I have to bring in, how many belts, and what am I already
producing that I could tap into instead of building new?" without an MCP
server or Hermes — a local CLI (`planner/`) invoked directly.

This exists because the arithmetic involved (recipe-chain expansion, batches
→ machine counts, raw-input rollup) should be **baked into deterministic
code, not generated on the fly in conversation**. The heavy math is not new
code: it's `recipe-mcp`'s existing, tested `engine.plan_product` /
`engine._expand_node` (in `~/code/homelab/apps/recipe-mcp/src/engine.py`),
imported directly — no HTTP, no MCP protocol, just a plain async function
call. `planner/` only adds what didn't already exist: live-state netting,
belt/pipe counts, and the CLI itself.

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
uv run python -m planner expand iron-plate --rate 2 --stop-items iron-ore
uv run python -m planner recipe electronic-circuit
uv run python -m planner producers iron-ore
uv run python -m planner consumers iron-plate
uv run python -m planner have iron-plate
```

`uv run flma-planner ...` (the installed console script) works identically.
Every subcommand exits non-zero on error/ambiguity and prints candidates or a
next-step hint rather than a bare stack trace or empty output.

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

**"What's the full ingredient tree, not just the summary?"** Run `expand
<product> --rate <n>` for the nested BOM instead of `plan`'s flattened
machine bill.

**"I don't know the exact item id."** `producers`/`consumers`/`recipe` do
fuzzy/exact lookups against the `names`/`recipes` tables and will print
candidates if ambiguous — resolve the id there before calling `plan`/`expand`
with it (they also fuzzy-resolve internally, but exact ids avoid ambiguity
prompts).

## Known caveats (don't let these surprise you mid-conversation)

1. **Modpack alignment.** The committed recipe dump
   (`~/code/homelab/apps/recipe-mcp/recipes.json` → `recipes.db`) is a
   **Pyanodons** dump. The live game may be running a different modpack
   entirely (verified: this machine's dev-server save is **Space Age**,
   which shares almost no non-vanilla content with Pyanodons despite a
   deceptively high raw tech-id overlap from the shared vanilla core — see
   `planner/live_state.py:modpack_alignment`'s docstring for why a naive
   overlap ratio is the wrong metric and Jaccard similarity is used
   instead). When they don't match, `plan`/`have` still run, but live
   tech-scoping and production-netting are skipped/empty rather than silently
   wrong — `status` reports this plainly. **The fix**: as of flma mod 0.3.0
   the mod exports its own RecipeExporter-compatible dump from the running
   game (`~/.factorio/script-output/flma/recipes.json`, see `SCHEMA.md`), so
   rebuild the DB from that and alignment is guaranteed:
   `cd $RECIPE_MCP_DIR && uv run python -m src.build_db
   ~/.factorio/script-output/flma/recipes.json recipes.db` (or build to a
   separate path and point `RECIPES_DB` at it).
2. **Recipe selection can pick a surprising alternate.** Pyanodons has
   synthetic/alternate recipes for many things that would normally be raw
   (e.g. ores producible from biomass byproduct chains). The engine's
   producer-selection heuristic (first enabled, alphabetically, absent a
   direct name match — see `engine._pick_producer`) can pick one of these
   over the "obvious" recipe, expanding rate calculations through a long
   unrelated chain instead of stopping at the raw resource. Use `--stop-items
   <comma-separated ids>` on `plan`/`expand` to pin known raw inputs (ores,
   basic raw resources) and short-circuit this. If a plan's `raw_inputs`
   look wrong, run `expand` on the same product to see which recipe chain
   was actually selected.
3. **Belt/pipe throughput is a placeholder.** Neither `recipes.json` nor the
   DB contains belt-speed or pipe-throughput data (confirmed absent from the
   RecipeExporter dump). `planner/throughput.py` hardcodes base/Space-Age
   belt tiers and a rough pipe figure; Pyanodons' actual belt tiers and its
   `py-transport-belt-capacity-N` research multipliers aren't filled in yet.
   Treat belt counts as order-of-magnitude, not exact, until that module is
   updated — it's flagged in `plan`/`status` output too.
4. **Drill/raw estimates ignore modules, productivity, and ore purity** — the
   engine already labels these `approximate`; don't present them as exact.

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
