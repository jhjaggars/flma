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

- The user asks to design/plan a production line, especially open-ended asks
  ("help me set up copper processing", "how should I make X", "what's the
  best way to make X") — run `recommend <product>` FIRST (see the
  **recommend workflow** below). It already cross-references every viable
  recipe against its unlocking technology and swaps in a tech-bundle's
  combined cost when one exists, so you get the actual best plan for the
  current save's tech level in one call — don't reconstruct that reasoning
  yourself, and don't stop at `options` alone (it never checks for combos).
- The user wants to compare alternatives `recommend` didn't pick, or explore
  the full decision space — see the **options-first workflow** below.
- The user just researched something (or asks "what does tech X unlock/do"),
  or wants to see a specific bundle's own machine counts — run `tech <name>`
  (see the **tech-bundle workflow** below).
- The user asks to design/plan a production line for a target rate ("how do
  I make N/sec of X", "how many assemblers do I need for Y") when the recipe
  choice is already settled.
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
uv run python -m planner recommend copper-plate                # the single best plan for your tech level — start here for open-ended asks
uv run python -m planner options copper-plate                 # viable ways to make X, if you want to compare alternatives yourself
uv run python -m planner options copper-plate --include-byproducts  # also show hidden byproduct/impractical recipes
uv run python -m planner belts 2                               # N belts of input -> achievable rate, for the belt-budget question
uv run python -m planner tech "Copper processing - Stage 1"    # what a tech unlocks & whether the pieces combine
uv run python -m planner plan "processing unit" --rate 10     # rate is items/sec by default (--unit per-min for /min)
uv run python -m planner plan sand                            # no --rate: sizes for 1x the top-level recipe's machine instead
uv run python -m planner plan iron-plate --rate 2 --stop-items iron-ore
uv run python -m planner plan sand --rate 1 --recipe sand=gravel-to-sand  # force a specific recipe
uv run python -m planner expand iron-plate --rate 2 --stop-items iron-ore
uv run python -m planner expand copper-plate --alternates       # inline per-node alternates instead of `options`' menu view
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

**Recommend workflow — the default first move for open-ended asks ("design a
copper processing line", "how should I make X", "what's the best way to make
X").** Run `recommend <product>` (no `--rate` needed — same 60/min default
yardstick as `options`). It already does the cross-referencing an agent used
to have to do by hand: for every currently-usable recipe, it checks whether
that recipe's unlocking technology bundles it with a sibling recipe into a
zero-waste combo (via `techbundle`), swaps in the bundle's true combined cost
where one applies, and prints the single best plan for the save's current
tech level — plus a "no research needed" baseline for context, and a count of
how many locked options exist if the user wants to research further. Read its
output back directly:
- A **combo** recommendation ("`recommended: X, Y, Z (combo via "<tech>")`")
  gives you the per-recipe batch rates at whatever rate you ran `recommend`
  at, plus a `next: tech "<tech>" --rate <n> --unit per-min` hint with
  machine counts — that IS the build, nothing further to compute (see the
  caveat below on why `plan --recipe` can't reproduce it).
- A **plain** recommendation gives you a `next: plan <product> --recipe
  <product>=<recipe> --rate <n>` to size it for a real target rate.
- **Figure out the target rate BEFORE running `recommend`/`tech`, if it
  matters** (a stated rate, or a belt-supply constraint — convert with
  `belts <n>` first), and pass it via `--rate`/`--unit` on the SAME
  invocation. **Never multiply a printed machine count by hand to reach a
  different rate** — every count is already rounded up (ceiling) per
  machine, and scaling rounded counts overcounts (confirmed by a fresh-agent
  eval: computing 60/min counts then ×3'ing them for a 180/min target gave
  9/6/3 instead of the correct 9/5/1 — re-running `tech`/`recommend` with
  `--rate 180 --unit per-min` directly is what gives the tight answer). If
  you already ran without a target rate and the user then states one,
  re-run the whole command with `--rate` rather than doing the arithmetic
  yourself.

Use `options`/`tech` individually instead when: the user wants to see/compare
every alternative (not just the winner), you need to resolve a `deeper
choice` further down a chain, or you want a specific bundle's own machine
counts rather than just its raw-input total.

**Options-first workflow — for comparing alternatives yourself, or when
`recommend` doesn't cover what you need** (e.g. every candidate needs
research, or the user wants to see trade-offs `recommend` didn't surface):

1. Run `options <product>` (no `--rate` needed — it defaults to a 60/min
   comparison yardstick applied identically to every candidate, precisely so
   the options are comparable; this differs from `plan`/`expand`'s "size for
   1 machine" default, which would make a slow recipe look falsely cheap).
   It lists every distinct *viable* way to make the product — e.g. a
   1-stage `ore -> plate` smelt next to a 3-stage `ore -> crush -> screen ->
   smelt` line — each tagged with tech status (`[researched]`,
   `[needs: X]`), stage count, raw-input rollup, and machine categories
   involved. Byproduct and wildly-impractical recipes (a low-probability
   secondary output that would need dozens of machines just to trickle out
   the target) are hidden by default — `--include-byproducts` reveals them,
   and the footer says how many were hidden.
2. **Present the options and their trade-offs to the user, don't just pick
   one.** This is the reasoning the LLM is actually for: "option A is the
   simple 1-stage smelt at 480 ore/min; option B needs research you don't
   have yet but uses ~20% less ore across a longer chain — which do you
   want, or do you want the most efficient path regardless of tech cost?"
3. **Ask about scale**, if it matters: a target rate ("how much do you want
   per second?") or a supply constraint ("how many yellow belts of ore do
   you want to dedicate to this?" — convert with `belts <n>`, see below).
   Don't invent a number if the user hasn't indicated scale matters — same
   "just build one" logic `plan`'s own no-`--rate` default uses.
4. If an option's menu entry flagged a `deeper choice: <item> (N viable
   recipes)` — a decision point further down that same chain — run
   `options <item>` to resolve it the same way before finalizing.
5. If the user says they can already supply some input directly (a free/
   pumped fluid, an ore they consider "given", an intermediate they already
   produce elsewhere), pass it via `--stop-items` (comma-separated) on
   `options` — it treats that item as a raw input and stops expanding past
   it, the same restriction `plan`/`expand` already support.
6. Once the user has picked a path (and rate), run `plan <product> --recipe
   <product>=<recipe_id> --rate <n>` for the exact machine/raw-input/drill
   bill. Read the `machines`/`raw inputs`/`drills` sections back to the user
   directly — don't recompute or sanity-check the arithmetic by hand, that's
   exactly what this tool exists to avoid.

**"How many belts of X do you want to supply?"** Convert with `belts <n>
[--tier <belt-id>]`, then feed the printed rate straight into `options
--rate`/`plan --rate`. Same base/Space-Age placeholder-accuracy caveat as
`plan`'s belt counts (see caveats below) — treat it as order-of-magnitude for
non-vanilla modpacks.

**Tech-bundle workflow, for "what did I just unlock" / recipes that only pay
off in combination.** Some recipes have multiple joint outputs per batch
(e.g. a "screener" that yields two ore grades at once) that a separate
recipe can consume to make more of the target with zero waste — `plan`/
`expand`/`options` never see this, since the underlying engine picks exactly
one recipe per item and silently discards every OTHER product a chosen
recipe also yields. Run `tech <name>` (id or human name, same fuzzy
resolution as everything else) to see this instead of missing it:
1. It lists everything the technology unlocks, tagged with live research
   status (`[researched]`, `[needs: X]` for missing prereqs).
2. If the unlocked recipes form a **detected bundle** (a connected group via
   shared intermediate items — not just a plain list), it solves the exact
   batch-rate blend that closes the loop with zero waste and reports it
   directly: per-recipe rate, machine count, and the bundle's true external
   raw inputs (already netted — no manual "reuse the byproduct" math needed).
   Present this to the user as the *actual* best answer, better than any
   single option `options` would show for the same item.
3. If it says the recipes **"could not combine into one blend"** (multiple
   valid blends exist, or they're a contradictory/negative system), say so
   plainly and fall back to `options <item>` to compare them as separate
   alternatives — don't try to guess a ratio yourself.
4. A tech unlocking a lone recipe (or many unrelated ones — some "grab-bag"
   techs unlock 50-900 recipes at once and are skipped as too large to
   analyze) just lists what it unlocks; nothing to combine.

**"Design a line for X at rate Y" (recipe already chosen/obvious).** Skip
straight to `plan <product> --rate <n>` and read the `machines`/`raw
inputs`/`drills` sections back to the user directly.

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
machine bill. Add `--alternates` to also see each node's other candidate
recipes inline (the same per-node data `options` summarizes at the top
level, without `options`' menu framing).

**"I don't know the exact item id."** `producers`/`consumers`/`recipe` do
fuzzy/exact lookups against the `names`/`recipes` tables and will print
candidates if ambiguous — resolve the id there before calling `plan`/`expand`
with it (they also fuzzy-resolve internally, but exact ids avoid ambiguity
prompts).

### Worked example: copper processing

```
$ uv run python -m planner recommend copper-plate
copper-plate (Copper plate) — recommended at 60/min (1/s):

recommended: copper-plate-4, grade-1-copper-crush, grade-2-copper  (combo via "Copper processing - Stage 1")
  copper-ore 300/min
    copper-plate-4               30/min
    grade-1-copper-crush         30/min
    grade-2-copper               60/min
  (this IS the build — `plan`/`expand` can't represent a blended multi-recipe combo yet)

next: `tech "Copper processing - Stage 1" --rate 60 --unit per-min` for machine counts at
this exact rate — do NOT multiply this command's counts by hand for a different rate,
re-run it with the new --rate instead.

runner-up (no research needed): copper-plate
  copper-ore 480/min

4 further option(s) need research not yet done — see `options copper-plate` for all of them.
```

One command already gives the actual answer: the screener (`grade-2-copper`)
and crusher (`grade-1-copper-crush`) are a matched pair unlocked by the same
tech — run both, and the crusher turns the screener's own low-grade byproduct
into more of the target grade, so nothing is wasted (300 ore/min vs. 480 for
the plain ungated recipe, a real ~37% saving). Present this directly; only
reach for `options`/`tech` if the user wants to see the runner-up's own
trade-offs in more detail, or compare the locked options too.

To get the exact same combo view again later, or the runner-up's own machine
count: `tech "Copper processing - Stage 1"` (for the combo) or `plan
copper-plate --recipe copper-plate=copper-plate --rate <n>` (for the plain
ungated fallback).

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
   alphabetical among them — not necessarily the cheapest chain. This is
   exactly what `options` (see the workflow above) exists to make visible up
   front, rather than discovering it after the fact: run `options <product>`
   to see every viable candidate side by side before `plan`/`expand` silently
   pick one. If you're already past that point and `raw_inputs` look wrong or
   needlessly expensive: `producers <item>` to see candidates (tagged `[main
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
8. **`tech`'s bundle detection is scoped to ONE technology's own unlock set,
   not the whole recipe graph** — deliberately: a from-scratch graph-wide
   search was tried and reached ~4159 of ~4160 total Pyanodons recipes from a
   single starting item (essentially the whole modpack), which is neither
   tractable nor the right scope. This means a co-product relationship that
   spans two *different* techs won't be found — only the copper-style case
   where the mod bundles the whole mini-pipeline behind one research. A tech
   unlocking more than 40 recipes at once (a "grab-bag" tech, not a coherent
   bundle) skips analysis entirely and just lists what's unlocked.
9. **A combo `recommend`/`tech` finds can't be built via `plan --recipe`.**
   `plan`'s `--recipe item=recipe,other=recipe` overrides still force exactly
   one recipe per item all the way down the chain — verified directly:
   running the "obvious" `plan copper-plate --recipe
   copper-plate=copper-plate-4,grade-2-copper=grade-1-copper-crush` doesn't
   reproduce the bundle's 300 ore/min at all, it gives **1500** ore/min
   (forces everything through both the screener AND the crusher in series
   instead of splitting the flow in the right ratio). A combo's own printed
   batch rates/machine counts (from `tech`/`recommend` directly) ARE the
   build — there's no `plan` invocation that gets you there instead.

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
