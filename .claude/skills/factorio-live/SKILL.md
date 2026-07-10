---
name: factorio-live
description: Query a running Factorio game's live state (research, production rates, logistics contents, player inventory, placed buildings) via the flma planner CLI — no MCP server, just local commands reading the mod's exported snapshot files.
---

# flma live-observe commands

## Purpose

Answers "what's happening in my game right now" — current research and
progress, item/fluid production rates, what's stored in logistics networks,
a player's inventory, or where buildings are placed — by reading the flma
mod's exported snapshot files (`script-output/flma/`) directly, no server
process involved. These are `python -m planner` subcommands, siblings of the
factory-planning ones covered by the `factory-planner` skill.

## When to use this skill

- The user asks what they're researching, or how far along it is.
- The user asks about current production/consumption rates for an item or
  fluid ("what's my iron plate rate", "am I fluid-negative on sulfuric
  acid").
- The user asks what's stored in their logistics network, or how many
  construction/logistic bots they have.
- The user asks what's in a player's inventory.
- The user asks how many of a building they've placed, or where a specific
  one is.
- Before trusting a "not researching anything" or "zero production" answer:
  check staleness first (see below) — a feed that's gone dark reads the same
  as "genuinely idle" otherwise.

For "how do I build X" / production-line design, that's the `factory-planner`
skill (`plan`/`options`/`recommend`/`tech`), not this one — the two compose:
observe what exists here, then plan what's missing there.

## Running it

From this repo's root:

```bash
uv run python -m planner status                                # feed staleness + modpack alignment (run this first)
uv run python -m planner research                              # current research, progress, queue
uv run python -m planner tech-tree --status available           # researched / available / locked technologies
uv run python -m planner production --kind items                # per-minute item production/consumption rates
uv run python -m planner production --kind fluids --surface vulcanus
uv run python -m planner logistics                              # network contents, bot counts
uv run python -m planner logistics --surface nauvis
uv run python -m planner inventory                               # a connected player's inventory
uv run python -m planner inventory --player jhjaggars
uv run python -m planner buildings                               # counts by name/type
uv run python -m planner buildings --type assembling-machine --list  # positions, filtered
```

Every command accepts `--force` (default `player`) and `--json` (prints the
exact result dict instead of the compact text rendering — use this when
you're going to parse or further compute over the output rather than just
reading it). `status --json` covers the former `get_snapshot_age` MCP tool —
its `age_seconds` block reports how stale each feed is.

## Common workflows

**Sanity-check before trusting "nothing's happening".** Run `status` (or
`status --json`) first if it hasn't been run this session — its `age_seconds`
block reports seconds-since-last-write per feed. A feed that's `null`/very
old means the mod isn't exporting it (wrong `SCRIPT_OUTPUT_DIR`, the relevant
map setting is off, or the mod isn't enabled) — don't report "idle" or "zero"
from stale/missing data without saying so.

**"What's my production rate for X?"** `production --kind items` (or
`fluids`) prints every item/fluid with nonzero `input_rates_per_min`
(produced) or `output_rates_per_min` (consumed) on the given surface
(defaults to `nauvis`, else whatever surface exists). These are real
per-minute flow rates, not the cumulative totals — use `--json` if you need
the cumulative `input_counts`/`output_counts` too (both are always present in
the JSON form, only the rates are shown in text).

**Opt-in feeds.** `inventory` needs the `flma-export-inventories` map setting
(off by default — more sensitive than aggregate stats); `buildings` needs
`flma-export-buildings`. Both print a clear hint instead of an empty/wrong
answer when the setting is off — relay that hint to the user rather than
guessing why the result is empty.

**Buildings: counts vs. listing.** With no `--name`/`--type`/`--surface`
filter and no `--list`, `buildings` prints aggregate counts by name and by
type (cheap, good for "how many X do I have"). Any filter, or `--list`,
switches to a positioned listing (capped at `--limit`, default 100, max 200)
— use this for "where is my Nth assembler" style questions.

**Multiple players connected.** `inventory` with no `--player` only succeeds
if exactly one player is connected; otherwise it lists `connected_players` —
pass `--player <name>` explicitly once you know which one.

## Configuration

`SCRIPT_OUTPUT_DIR` (shared with the factory-planner commands) controls where
live snapshots are read from — normally
`~/.factorio/script-output/flma`. See `../factory-planner/SKILL.md` for the
planning-side commands and `SCHEMA.md` for the exact shape of every snapshot
file these commands read.
