# mod/ — the flma Factorio mod (the producer)

A Factorio 2.0 mod that exports live game state as JSON/NDJSON under
`script-output/flma/`. Lua only, self-contained — nothing in here imports or
depends on the Python side; the exported files (documented in `../SCHEMA.md`)
are the entire interface.

Runs as part of the synced mod set on the server *and* every client (it has a
control stage, so its checksum must match — it cannot be a client-only mod).
Every peer writes its own local copy; there is no `for_player` filtering.

## Efficiency — the core design constraint

Because this code runs on the server for every player, not just the one running
the CLI, per-tick cost is shared by everyone. `control.lua` follows these
rules throughout (see its top-of-file comment for the full rationale):

1. No `on_tick` hook — only `script.on_nth_tick(N)` with a large, configurable `N`
   (`flma-tick-interval`, default 300 ticks / ~5s).
2. Tech/production/logistics/inventories use engine-aggregated reads
   (`LuaFlowStatistics`, `LuaLogisticNetwork:get_contents()`, force-level tables) —
   cost is O(#item types), not O(#entities) — and are overwritten as small full
   snapshots each cycle (cheap, no diffing needed).
3. Buildings are the one dataset proportional to base size. Instead of a scheduled
   `find_entities_filtered{}` scan, `flma` maintains an incremental index: one
   time-sliced baseline scan the first time it's enabled, then O(1) per build/mine
   event via filtered event handlers. The event log (`buildings.ndjson`) is
   periodically compacted from the in-memory index rather than re-scanned.
   - "Buildings" is a **blocklist by Factorio's built-in prototype `type`**, not by
     name — this is what makes it mod-agnostic: every mod's custom entities
     (including all of pyanodons') still have to declare one of the engine's fixed
     type categories, so a blocklist of types covers any mod's variants
     automatically. Besides non-placed entities (resources, trees, corpses, etc.),
     it also excludes high-cardinality "connective tissue" types — belts, pipes,
     poles, inserters, rails/signals — since those vastly outnumber actual
     production/logistics structures on any real base and aren't useful to track
     positionally. Mobile `unit`s and everything on the `enemy`/`neutral` forces
     (nests, worms, remnants) are excluded too — nothing non-factory is tracked.
   - The baseline scan's collecting phase iterates Factorio's own map chunk grid
     (32x32 tiles, via `surface.get_chunks()`), `BASELINE_CHUNKS_PER_TICK` chunks
     per tick, each queried with `find_entities_filtered{area=.., type=..,
     invert=true}` so the engine excludes non-buildings natively. This bounds each
     tick's cost by chunk density, not total base size — a megabase just takes
     more ticks, never a bigger single-tick spike. The draining phase then applies
     `BASELINE_CHUNK_SIZE` entities/tick to the in-memory index, and both the
     baseline dump and periodic compaction write in one batched `write_file` call
     rather than one syscall per building.
4. Everything is gated behind the synced `flma-export-enabled` runtime-global
   setting — disabled means zero registered handlers (verified via F4
   `show-time-usage`), not just an early-return inside a live handler.
5. Sanctioned exception to the single-tick budget: `export_recipes()` builds
   and serializes the ~11 MB `recipes.json` in one tick. Its triggers are
   strictly event-shaped — init, mod-configuration change, recipe-affecting
   research (coalesced via a dirty flag to at most one write per
   tick-interval), translation-pass completion, or an explicit remote call —
   never the periodic schedule.
6. Per-machine dynamic state (`building-contents.json` — ingredient/output
   contents, crafting progress) is inherently O(#machines) if read broadly,
   so it doesn't get its own scan like buildings' baseline: instead it
   piggybacks on `storage.flma.recipe_entities` (already cached for the
   recipe/rescan machinery above) and is explicitly scoped by
   `flma-contents-tracked-names` to just the entity names the player
   named — cost is O(#matched machines), the same "bound it to what's
   actually asked for" shape `export_inventories()` already uses for
   O(#connected players), never O(#all buildings).

## Settings (Mod settings → Map, or `/c settings.global[...] = {value=...}`)

| Setting | Default | Purpose |
|---|---|---|
| `flma-export-enabled` | `false` | Master switch — gates every handler registration |
| `flma-tick-interval` | `300` | Ticks between production/logistics/inventory exports |
| `flma-export-inventories` | `false` | Player inventory contents are more sensitive than aggregate stats |
| `flma-export-buildings` | `false` | Triggers the one-time baseline scan; off by default |
| `flma-buildings-compact-threshold` | `20000` | Lines appended before `buildings.ndjson` is compacted |
| `flma-contents-tracked-names` | `""` | Comma-separated exact entity names to export ingredient/output contents + crafting progress for; empty disables `building-contents.json` entirely. Requires `flma-export-buildings` too |

## Files written (`script-output/flma/<save_id>/`)

Summary only — **`../SCHEMA.md` is the authoritative format reference** (exact
JSON shapes with real examples, the empty-array-as-`{}` quirk, quality-tiered
contents arrays vs. plain count maps, torn-write handling, compaction detection,
and the per-save `<save_id>` namespacing below). Any change to what this mod
writes must be reflected there and noted in `changelog.txt`.

Every data file is namespaced under a `save_id` the mod generates once and
persists in `storage` (`save_id()`/`output_dir()` in `control.lua`) — this
stops switching between saves/servers on one machine from silently mixing or
clobbering a different save's files. A small fixed-location pointer,
`flma/current-save.json`, points a consumer at the currently-active
`save_id`.

| File | Written | Contents |
|---|---|---|
| `current-save.json` (fixed location, not namespaced) | every `flma-tick-interval`, and immediately when `flma-export-enabled` turns on | `{"save_id":..., "tick":...}` — lets a consumer find the active save's subdirectory without hardcoding it |
| `<save_id>/tech.json` | on research started/finished/queued/cancelled/reversed events (full overwrite) | per-force: current research, progress, queue, all technologies + prerequisites |
| `<save_id>/research.json` | every `flma-tick-interval` (full overwrite) | per-force: current research, progress, queue only (O(#forces), not the full tech table) — keeps `research_progress` live between the coarser research events that refresh `tech.json` |
| `<save_id>/production.json` | every `flma-tick-interval` (full overwrite) | per-force, per-surface item/fluid `input_counts`/`output_counts` (lifetime cumulative totals) **and** `input_rates_per_min`/`output_rates_per_min` (real per-minute flow, via `get_flow_count`) |
| `<save_id>/logistics.json` | every `flma-tick-interval` (full overwrite) | per-force logistic networks: contents, robot counts |
| `<save_id>/inventories.json` | every `flma-tick-interval`, if enabled (full overwrite) | connected players' main inventory contents |
| `<save_id>/recipes.json` | on init / mod-config change / recipe-affecting research (coalesced) / translation completion / `remote.call("flma","export_recipes")` — never periodic (full overwrite, ~11 MB) | RecipeExporter-compatible dump of recipes, items, fluids, machines/drills/resources/generators, technologies, qualities, groups (player force); `translated_name` filled in best-effort once a connected player's translation pass completes |
| `<save_id>/buildings.ndjson` | on build/mine events (append), periodically compacted | `{"op":"add"/"remove", "entity":{...}}` event log — `entity` includes `recipe`/`modules`/`circuit` config, all absent-not-null |
| `<save_id>/building-contents.json` | every `flma-tick-interval`, only for machines matching `flma-contents-tracked-names` (full overwrite) | per-machine ingredient/output contents + crafting progress — dynamic state, deliberately not part of the event log |

## Debugging

Mod-local `storage` is not readable from `/c` console commands (those execute in
the scenario's own separate storage scope, not the mod's) — use the remote
interface instead: `/c remote.call("flma", "status")` prints export state and
tracked-building count; `/c remote.call("flma", "reset_buildings")` clears the
index and forces a fresh baseline scan under current rules, without needing a
new save; `/c remote.call("flma", "export_now")` forces one export cycle
immediately (useful when no players are connected and ticks aren't advancing);
`/c remote.call("flma", "export_recipes")` forces a `recipes.json` rewrite
(and kicks a translation pass if a player is connected). `status` also reports
`recipes_dirty` and translation-pass state.

For iterating against a live game, use the local dev environment in `../dev/`
(see `.claude/skills/factorio-dev/SKILL.md`) — note a **version bump in
`info.json` requires fully restarting both server and client**, not just a save
reload.

## `changelog.txt` format

Factorio parses this file with a strict grammar
([lua-api.factorio.com/latest/auxiliary/changelog-format.html](https://lua-api.factorio.com/latest/auxiliary/changelog-format.html))
and silently mis-renders (or drops) a section that doesn't match — there's no
error, just a wrong-looking changelog in the in-game mod manager or on the
portal. Hard rules, easy to violate by hand-editing:

- Each version section starts with a separator line of **exactly 99 dashes**,
  nothing else on the line.
- `Version: X.Y.Z` next (each of X/Y/Z in 0–65535, `0.0.0` invalid), then an
  optional `Date: ...` line.
- The line immediately after `Version:`/`Date:` must **not** be blank.
- A category header (`  Changes:`, `  Bugfixes:`, etc.) is exactly two spaces
  of indent, then the name, then a colon — nothing after it.
- An entry is exactly four spaces, a dash, a space, then text; a wrapped
  continuation line is exactly six spaces of indent, no dash.
- **No tabs anywhere, no trailing whitespace on any line.**

When adding an entry (required for any change to what the mod exports, see
above), match the existing entries' exact indentation instead of re-deriving
it, and don't introduce tabs or trailing spaces. If unsure, verify
mechanically before committing:

```bash
python3 -c "
lines = open('mod/changelog.txt', encoding='utf-8').read().split(chr(10))
bad = [i+1 for i, l in enumerate(lines) if l != l.rstrip() or '\t' in l]
print('trailing-whitespace/tab lines:', bad or 'none')
print('bad separators:', [i+1 for i, l in enumerate(lines)
      if set(l) == {'-'} and l and l != '-'*99])
"
```

## Packaging

From the repo root: `make mod-zip` → `flma_<version>.zip`; drop into
`~/.factorio/mods/` or upload to the mod portal.

## Verifying in-game

Confirmed working against a real running client (mod checksum loads clean, settings
toggle live via `on_runtime_mod_setting_changed`, all live-observe CLI commands return
real data from a live save):

1. Copy `mod/` into `~/.factorio/mods/flma_<version>/` (or `make mod-zip` and use the
   in-game mod manager), enable it, start/load a save.
2. Enable the map setting `flma-export-enabled` (Mod settings → Map). Confirm
   `script-output/flma/current-save.json` appears, and
   `script-output/flma/<save_id>/tech.json` (that pointer's `save_id`) has a
   non-empty `forces.player` entry.
3. Research a technology; confirm `tech.json` updates without waiting a full
   `flma-tick-interval`.
4. With `SCRIPT_OUTPUT_DIR` pointed at `script-output/flma` (the parent
   directory — it resolves `current-save.json` itself), exercise each command
   (`uv run python -m planner research`, `production`, `logistics`,
   `inventory`, `buildings`); confirm `status --json`'s `age_seconds` tracks
   the live game.
5. Enable `flma-export-buildings`; confirm `buildings.ndjson` gets a burst of `add`
   events (the baseline scan) spread across a few ticks, not one frame spike.
   ✅ Verified 2026-07: a 26,195-building base wrote its baseline across ~59 ticks.

Still open (not yet exercised against a real base with construction happening):

6. Build/mine something with `flma-export-buildings` on; confirm exactly one
   `add`/`remove` line appears per event (the live log so far contains only the
   baseline burst — zero live events, zero removes).
7. Check cost: F4 → `show-time-usage`, toggle `flma-export-enabled` off/on and
   confirm the mod's line drops to ~0 when disabled.
