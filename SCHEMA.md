# flma data schema

This document is the contract between the two halves of this project: the
**producer** (the `flma` Factorio mod, `mod/`) and any **consumer** (the
live-state reading layer in `src/`, the planner CLI in `planner/`,
`dev/summary.py`, or anything you write yourself). The mod writes the files
described here; consumers read them and nothing else — there is no other
channel between the two.

Describes the format as written by mod version **0.3.5** (`mod/info.json`).
Shape changes are noted in `mod/changelog.txt`; additions of new fields or new
files are backwards-compatible and consumers must ignore keys they don't
recognize.

## Where the files live

The mod writes into `flma/` under Factorio's `script-output` directory of the
machine it's running on — e.g. `~/.factorio/script-output/flma/` on Linux.
Every peer in a multiplayer game (server and each client) writes its own local
copy; a consumer reads its own machine's files.

**Every data file lives under a per-save subdirectory, `flma/<save_id>/`.**
`save_id` is a short token the mod generates once (`math.random`-derived hex)
and persists in `storage`, so it survives forever with that save — Lua has no
API to read the save's actual filename, and filenames aren't stable across
renames/copies/autosave rotation anyway. This is what stops switching which
save/server you're pointing a consumer at from silently mixing or clobbering a
*different* save's `buildings.ndjson`, `tech.json`, etc. into the same files.

A consumer that doesn't want to track `save_id` itself reads the small,
fixed-location `flma/current-save.json` pointer first:

```json
{"save_id": "3fa1c9b2", "tick": 22516200}
```

— refreshed every `flma-tick-interval` cycle and immediately when
`flma-export-enabled` turns on — then reads `flma/<save_id>/*` for the actual
data. A consumer given `flma/` itself should re-check this pointer
periodically (not just once at startup): if the operator points it at a
different save/server without restarting the consumer, `save_id` changes and
every snapshot/index needs to be re-opened against the new subdirectory rather
than continuing to read stale files at the old path.

| File | Kind | Written |
|---|---|---|
| `current-save.json` | full-overwrite JSON, fixed location (`flma/`, not namespaced) | every `flma-tick-interval` ticks, and immediately when `flma-export-enabled` turns on |
| `<save_id>/tech.json` | full-overwrite JSON | on research started/finished/queued/cancelled/reversed, and when exporting is (re)enabled |
| `<save_id>/research.json` | full-overwrite JSON | every `flma-tick-interval` ticks |
| `<save_id>/production.json` | full-overwrite JSON | every `flma-tick-interval` ticks |
| `<save_id>/logistics.json` | full-overwrite JSON | every `flma-tick-interval` ticks |
| `<save_id>/inventories.json` | full-overwrite JSON | every `flma-tick-interval` ticks, only if `flma-export-inventories` is on |
| `<save_id>/recipes.json` | full-overwrite JSON | on init, on mod-configuration change, when a finished/reversed research unlocks recipes or changes recipe productivity (coalesced to the next tick-interval), on translation-pass completion, and on `remote.call("flma", "export_recipes")` — never periodic |
| `<save_id>/buildings.ndjson` | append-only NDJSON event log | on each build/mine event; periodically compacted (see below), only if `flma-export-buildings` is on |

Nothing is written at all unless the `flma-export-enabled` map setting is on
(`current-save.json` included — a consumer sees no pointer file at all until
then). `remote.call("flma", "export_now")` from the Factorio console forces
one export cycle immediately (useful on a paused or player-less server);
`remote.call("flma", "status")` reports the current `save_id` and resolved
output directory.

## Conventions (apply to every file)

- **`tick`** — every snapshot carries the game tick it was written at
  (`t` in `buildings.ndjson` records). 60 ticks ≈ 1 second at normal speed.
- **Writes are truncate-then-write, not atomic rename.** A consumer can catch
  a file mid-write (empty, or truncated JSON). Keep the last good parse and
  retry on the next poll; don't treat a torn read as "data gone".
- **Absent, not `null`.** Lua `nil` fields are omitted from the JSON entirely.
  E.g. a force with no active research has no `current_research` key at all.
- **Empty arrays serialize as `{}`.** Factorio's `table_to_json` can't
  distinguish an empty array from an empty object, so a field that is normally
  an array (e.g. `research_queue`) appears as `{}` when empty. Treat `[]` and
  `{}` both as "empty".
- **Two different item-count shapes.** Anything backed by an inventory-like
  read (`logistics.json` `contents`, `inventories.json` `contents`) is an
  **array** of `{"name", "quality", "count"}` objects (Factorio 2.0 breaks
  item counts out per quality tier). Production statistics are plain
  **`name → number` maps** with no quality dimension.
- **Forces.** Snapshots cover every force in the game, including `enemy` and
  `neutral` (whose entries are mostly empty). The force you almost always want
  is `player`. Mods can add more (e.g. blueprint-sandbox forces).

## `tech.json`

Full tech tree per force. Event-driven — refreshed on research
started/finished/queued/cancelled/reversed, so `research_progress` here goes
stale between events; use `research.json` for the live value.

```json
{
  "tick": 20653237,
  "forces": {
    "player": {
      "current_research": "laser-weapons-damage-7",
      "research_progress": 0.27,
      "research_queue": ["laser-weapons-damage-7"],
      "technologies": {
        "advanced-circuit": {
          "researched": true,
          "level": 1,
          "enabled": true,
          "prerequisites": ["plastics"]
        }
      },
      "mining_drill_productivity_bonus": 0.2
    }
  }
}
```

- `research_progress` — fraction 0–1 of the *current* research only.
- `technologies` — one entry per technology the force knows about.
  `enabled=false` means locked out (e.g. hidden by a mod). A technology is
  *available to research* when it's not researched, is enabled, and all its
  `prerequisites` are researched — the file doesn't precompute that;
  consumers derive it (see `tech_tree` in `planner/observe.py`).
- `level` — current level, only meaningful for repeatable/leveled techs.
- `mining_drill_productivity_bonus` — the force's current mining-drill yield
  bonus (`LuaForce::mining_drill_productivity_bonus`; e.g. `0.2` = +20% ore
  per mining operation, same energy/time cost). Read straight from the engine
  rather than summed from individual tech effects, so it's correct no matter
  which technologies (vanilla's single infinite research, Pyanodons' many
  discrete `mining-productivity-N` techs, or any other mod) contributed to
  it. Absent (mod build predates 0.3.2) should be treated as `0`.

## `research.json`

The "what's happening right now" subset of `tech.json`, per force, refreshed
every `flma-tick-interval` cycle so progress stays live. O(#forces) — it never
contains the `technologies` table.

```json
{
  "tick": 20860200,
  "forces": {
    "player": {
      "current_research": "laser-weapons-damage-7",
      "research_progress": 0.2777,
      "research_queue": ["laser-weapons-damage-7"]
    },
    "enemy": { "research_queue": {} }
  }
}
```

Prefer this file for current research/progress/queue; fall back to
`tech.json`'s copy of the same three fields if it's absent (older mod builds).

## `production.json`

Per-force, per-surface item and fluid production statistics, engine-aggregated.

```json
{
  "tick": 20860200,
  "forces": {
    "player": {
      "surfaces": {
        "nauvis": {
          "items": {
            "input_counts":  { "iron-plate": 5804438 },
            "output_counts": { "iron-plate": 4436164 },
            "input_rates_per_min":  { "iron-plate": 1450.2 },
            "output_rates_per_min": { "iron-plate": 1201.0 }
          },
          "fluids": { "...same four maps...": {} }
        }
      }
    }
  }
}
```

Two kinds of numbers — don't confuse them:

- `input_counts` / `output_counts` — **lifetime cumulative totals** since the
  force began, not rates. `input` = ever *produced*, `output` = ever
  *consumed* (matching the left/right split of the in-game production GUI —
  yes, the naming is inverted from what you'd guess; it's the engine's).
- `input_rates_per_min` / `output_rates_per_min` — real per-minute flow over
  roughly the last 60 seconds. Use these for anything rate-shaped
  ("how much am I making right now").

A surface may omit `items` or `fluids` if the engine call failed for it.

## `logistics.json`

Per-force list of logistic networks with contents and robot counts.

```json
{
  "tick": 20860200,
  "forces": {
    "player": [
      {
        "network_id": 3,
        "surface": "nauvis",
        "contents": [
          { "name": "steel-chest", "quality": "normal", "count": 100 }
        ],
        "available_logistic_robots": 4460,
        "available_construction_robots": 1516,
        "all_logistic_robots": 4501,
        "all_construction_robots": 1520
      }
    ]
  }
}
```

`contents` is everything stored in the network's logistic containers
(buffered stock), not what's on belts or in machines.

## `inventories.json`

Connected players' main inventories. Only written when
`flma-export-inventories` is on (off by default — more sensitive than
aggregate stats). Keyed by player name; a player's name can be the empty
string on some local/dev servers.

```json
{
  "tick": 20860200,
  "players": {
    "jhjaggars": {
      "contents": [
        { "name": "iron-plate", "quality": "normal", "count": 8 }
      ],
      "force": "player",
      "surface": "nauvis"
    }
  }
}
```

## `recipes.json`

Static recipe/prototype dump of the running game: recipes, items, fluids,
crafting machines/mining drills/resources, technologies, qualities, and item
groups. **Byte-compatible with the RecipeExporter mod's**
`script-output/recipes.json` (github.com/FactorioCalc/RecipeExporter), so
recipe-mcp's `build_db.py` consumes it unchanged — and because it comes from
the live save, it always matches the modpack actually being played.

Unlike every other snapshot this file is **big** (~11 MB on a Space Age game)
and changes only when mods change or research unlocks recipes. Consumers
should `stat()` it and rebuild derived data on mtime change — never
poll-parse it. Consecutive exports of identical state are not byte-identical
(Lua `pairs` iteration order varies); compare mtime, not content.

Top-level object:

```json
{
  "game_version": "2.0.66",
  "groups":        { "<name>": { "name", "type", "order", "order_in_recipe?" } },
  "quality":       { "<name>": { "name", "level", "next?", "next_probability?", "..." } },
  "quality_names": ["normal", "uncommon", "..."],
  "recipes":       { "<name>": { "...see below..." } },
  "items":         { "<name>": { "name", "type", "order", "group", "subgroup", "stack_size", "weight", "fuel_category?", "fuel_value", "module_effects?", "rocket_launch_products", "flags?" } },
  "fluids":        { "<name>": { "name", "order", "group", "subgroup", "fuel_value" } },
  "entities":      { "<name>": { "...four shapes, see below..." } },
  "technologies":  { "<name>": { "...see below..." } }
}
```

All five main sections are **maps keyed by internal name**, not arrays.
`game_version` is the `base` mod's version string.

A recipe:

```json
"wooden-chest": {
  "name": "wooden-chest",
  "category": "crafting",
  "ingredients": [ { "type": "item", "name": "wood", "amount": 2 } ],
  "products":    [ { "type": "item", "name": "wooden-chest", "probability": 1, "amount": 1 } ],
  "main_product": { "type": "item", "name": "wooden-chest", "probability": 1, "amount": 1 },
  "allowed_effects": { "consumption": true, "speed": true, "productivity": false, "pollution": true, "quality": true },
  "maximum_productivity": 1000000,
  "energy": 0.5,
  "order": "a[items]-a[wooden-chest]",
  "group": "logistics",
  "subgroup": "storage",
  "enabled": true,
  "productivity_bonus": 0,
  "translated_name": "Wooden chest"
}
```

- Products with a random yield carry `amount_min`/`amount_max` instead of
  `amount` (e.g. uranium processing), plus `probability`.
- `entities` holds four shapes distinguished by `type`: **crafting machines**
  (`beacon`/`furnace`/`assembling-machine`/`boiler`/`rocket-silo` —
  `crafting_categories`, per-quality `crafting_speed` map,
  `module_inventory_size`, `energy_consumption`/`drain` in W,
  `energy_source: "electric"|"burner"`, `width`/`height`, …), **mining drills**
  (`resource_categories`, `mining_speed`, energy fields), **resources**
  (`resource_category`, `mining_time`, `required_fluid?`, `fluid_amount?`,
  `product_name`), and **generators** (`type == "generator"` only — fluid-driven
  electricity generators like `steam-engine` or pyanodons' `steam-turbine-mk01`:
  `max_power_output` in W, `fluid_usage_per_sec`, `effectivity`,
  `maximum_temperature`, `input_fluid?` — the fluid name from the entity's input
  fluidbox filter, absent if the generator has no filtered fluidbox).
  Crafting machines and mining drills with `energy_source == "burner"` also
  carry `burner_effectivity?` (their `LuaBurnerPrototype.effectivity`, needed
  together with an item's `fuel_value` to compute an exact fuel burn rate).
  **Excluded on purpose:** `electric-energy-interface` entities (e.g.
  pyanodons' wind turbines) have no static prototype power figure — their
  output is live per-instance state (`LuaEntity.power_production`) that their
  owning mod adjusts at runtime, not something a static prototype dump can
  represent.
- A technology: `enabled`, `researched`, `prerequisites[]`,
  `recipes_unlocked[]` (recipe names from its `unlock-recipe` effects),
  `unit_count?`, `unit_count_formula?`, `unit_energy`, `unit_ingredients[]`
  (`{name, amount}`).

**Single-force.** The per-force fields — recipe `enabled`/`productivity_bonus`,
technology `enabled`/`researched` — come from the `player` force only (unlike
`tech.json`'s all-forces shape). That matches how the format's consumers treat
the data.

**`translated_name` is best-effort.** The file is written immediately with
internal names only (no player required — works on a headless server). When a
player is connected, an async localised-name translation pass runs and the
file is rewritten with `translated_name` filled in on each object. The pass
is time-sliced (requests issued in small per-tick chunks), so the translated
rewrite lands some tens of seconds after a player joins, not instantly. On a
server no player ever joins, the field never appears. Consumers must fall
back to `name` (recipe-mcp's `build_db.py` already does). The locale is
whichever connected player's client answered the pass.

The empty-array-as-`{}` convention applies here too, with one wrinkle
inherited from RecipeExporter: key-set fields built by its `keys()` helper
(`flags`, `crafting_categories`, `allowed_effects` on entities,
`fuel_categories`, `resource_categories`) are **absent entirely** when empty
rather than `{}`.

## `buildings.ndjson`

Append-only event log of placed buildings — the one dataset proportional to
base size, so it's incremental rather than a periodic snapshot. Only written
when `flma-export-buildings` is on. One JSON record per line:

```json
{"t": 20754937, "op": "add", "entity": {"id": 177991, "name": "stone-wall", "type": "wall", "surface": "nauvis", "position": {"x": -741.5, "y": 119.5}, "force": "player"}}
{"t": 20755100, "op": "remove", "id": 177991}
{"t": 20755300, "op": "add", "entity": {"id": 178004, "name": "assembling-machine-2", "type": "assembling-machine", "surface": "nauvis", "position": {"x": -700.5, "y": 90.5}, "force": "player", "recipe": "iron-gear-wheel"}}
```

- `entity.id` is the entity's `unit_number` — unique per entity for the life
  of the save. Fold the log into a `id → entity` map: `add` upserts,
  `remove` deletes. Re-`add` of a known id just overwrites (this happens:
  the baseline scan can emit an id the live build handler already wrote, and
  a recipe change re-emits an `add` for an id already in the map — see
  `entity.recipe` below).
- **What counts as a "building"**: any entity *not* in the mod's type
  blocklist (`mod/control.lua` `BUILDING_TYPE_BLOCKLIST`) and not on the
  `enemy`/`neutral` forces. Excluded: non-placed entities (resources, trees,
  corpses…), mobile units, and high-cardinality connective tissue — belts,
  pipes, poles, inserters, rails and signals. Filtering is by prototype
  *type*, not name, so modded entities are covered automatically.
- **Baseline scan**: the first time tracking is enabled, the mod emits an
  `add` for every existing building, time-sliced over many ticks (verified:
  a 26k-building base wrote its baseline across ~59 ticks).
- `entity.recipe` — the machine's currently-configured recipe (internal
  name, e.g. `"iron-gear-wheel"`). Only present on entities whose `type` is
  `assembling-machine`, `furnace`, or `rocket-silo` (the only types
  `LuaEntity.get_recipe()` supports — mod 0.3.5+), and only when a recipe is
  actually set; **absent, not `null`**, on any other type or on a
  recipe-capable machine with no recipe configured yet, per this file's
  general absent-not-null convention.

  A recipe change re-emits an `add` for the same `id` with the new
  `entity.recipe` (or the field newly absent, if the recipe was cleared) —
  same upsert semantics as everything else in this file, no new `op`.
  Coverage of *when* that re-emit happens depends on how the recipe changed:

  | How the recipe changed | Detected by | Latency |
  |---|---|---|
  | Machine built/revived with a recipe already set (manual placement, blueprint ghost revive) | Read at build time, same event as the `add` itself | Immediate |
  | Copy/paste tool (shift-click drag, "paste entity settings" hotkey) between two entities | `on_entity_settings_pasted` | Immediate |
  | Blueprint pasted over an *existing* entity/ghost | `on_blueprint_settings_pasted` (mod 2.0+; guarded absent-safe if the running Factorio build predates it) | Immediate |
  | Manual recipe pick from the machine's own GUI | `on_gui_closed` on the entity GUI (no dedicated recipe-change event exists) | Immediate |
  | Circuit-network "Set recipe" signal (Space Age) | **Not covered by any Factorio event** — the engine only re-evaluates the circuit condition internally when the machine finishes its current craft. Caught only by the periodic bounded rescan below. | Up to one full sweep — with the default `flma-tick-interval` (300 ticks) and the mod's `RECIPE_RESCAN_BATCH_SIZE` (200 entities/cycle), roughly `ceil(recipe_capable_building_count / 200) * 5s` |

  The periodic rescan (`rescan_recipes()` in `mod/control.lua`) re-checks a
  bounded batch of recipe-capable buildings every `flma-tick-interval`
  cycle, resuming where the previous cycle left off (Lua's stateless
  `next()`) rather than re-scanning everything — cost is bounded regardless
  of base size, same rationale as the baseline scan's chunking. It's the
  only backstop for circuit-driven changes, but also silently covers any
  other missed signal, so treat the event-driven paths above as
  "usually immediate" rather than a hard guarantee.

  **Upgrading an existing save** from a mod version before 0.3.5: buildings
  already indexed at upgrade time have no cached live entity reference to
  read a recipe from, so the mod forces one fresh baseline rescan
  automatically the first time the upgraded mod runs (time-sliced, same as
  any baseline scan) to backfill `recipe` for everything already placed.

### Compaction — what a tailing consumer must handle

The mod periodically rewrites the whole file from its in-memory index
(truncate + rewrite, dropping superseded add/remove pairs), and also
truncates it to empty when tracking is turned off. A consumer that tails by
byte offset must detect a rewrite and replay from offset 0. Size shrinking
below your offset catches most rewrites, but a compacted file can be the
same size or larger — so also fingerprint the file's leading bytes (the
first record always starts with a fresh `{"t":<tick>,...}`) and treat any
change as a rewrite. See `src/game_state.py` `BuildingIndex` for the
reference implementation.

## Console / RCON introspection

The mod's `storage` isn't reachable from `/c` commands; use the remote
interface instead:

```
/c remote.call("flma", "status")           -- export state + tracked-building count
/c remote.call("flma", "reset_buildings")  -- clear index, force a fresh baseline scan
/c remote.call("flma", "export_now")       -- force one export cycle immediately
/c remote.call("flma", "export_recipes")   -- force a recipes.json rewrite immediately
```
