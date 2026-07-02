# flma data schema

This document is the contract between the two halves of this project: the
**producer** (the `flma` Factorio mod, `mod/`) and any **consumer** (the MCP
bridge in `src/`, the planner CLI in `planner/`, `dev/summary.py`, or anything
you write yourself). The mod writes the files described here; consumers read
them and nothing else — there is no other channel between the two.

Describes the format as written by mod version **0.2.1** (`mod/info.json`).
Shape changes are noted in `mod/changelog.txt`; additions of new fields or new
files are backwards-compatible and consumers must ignore keys they don't
recognize.

## Where the files live

The mod writes into `flma/` under Factorio's `script-output` directory of the
machine it's running on — e.g. `~/.factorio/script-output/flma/` on Linux.
Every peer in a multiplayer game (server and each client) writes its own local
copy; a consumer reads its own machine's files.

| File | Kind | Written |
|---|---|---|
| `tech.json` | full-overwrite JSON | on research started/finished/queued/cancelled/reversed, and when exporting is (re)enabled |
| `research.json` | full-overwrite JSON | every `flma-tick-interval` ticks |
| `production.json` | full-overwrite JSON | every `flma-tick-interval` ticks |
| `logistics.json` | full-overwrite JSON | every `flma-tick-interval` ticks |
| `inventories.json` | full-overwrite JSON | every `flma-tick-interval` ticks, only if `flma-export-inventories` is on |
| `buildings.ndjson` | append-only NDJSON event log | on each build/mine event; periodically compacted (see below), only if `flma-export-buildings` is on |

Nothing is written at all unless the `flma-export-enabled` map setting is on.
`remote.call("flma", "export_now")` from the Factorio console forces one
export cycle immediately (useful on a paused or player-less server).

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
      }
    }
  }
}
```

- `research_progress` — fraction 0–1 of the *current* research only.
- `technologies` — one entry per technology the force knows about.
  `enabled=false` means locked out (e.g. hidden by a mod). A technology is
  *available to research* when it's not researched, is enabled, and all its
  `prerequisites` are researched — the file doesn't precompute that;
  consumers derive it (see `get_tech_tree` in `src/server.py`).
- `level` — current level, only meaningful for repeatable/leveled techs.

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

## `buildings.ndjson`

Append-only event log of placed buildings — the one dataset proportional to
base size, so it's incremental rather than a periodic snapshot. Only written
when `flma-export-buildings` is on. One JSON record per line:

```json
{"t": 20754937, "op": "add", "entity": {"id": 177991, "name": "stone-wall", "type": "wall", "surface": "nauvis", "position": {"x": -741.5, "y": 119.5}, "force": "player"}}
{"t": 20755100, "op": "remove", "id": 177991}
```

- `entity.id` is the entity's `unit_number` — unique per entity for the life
  of the save. Fold the log into a `id → entity` map: `add` upserts,
  `remove` deletes. Re-`add` of a known id just overwrites (this happens:
  the baseline scan can emit an id the live build handler already wrote).
- **What counts as a "building"**: any entity *not* in the mod's type
  blocklist (`mod/control.lua` `BUILDING_TYPE_BLOCKLIST`) and not on the
  `enemy`/`neutral` forces. Excluded: non-placed entities (resources, trees,
  corpses…), mobile units, and high-cardinality connective tissue — belts,
  pipes, poles, inserters, rails and signals. Filtering is by prototype
  *type*, not name, so modded entities are covered automatically.
- **Baseline scan**: the first time tracking is enabled, the mod emits an
  `add` for every existing building, time-sliced over many ticks (verified:
  a 26k-building base wrote its baseline across ~59 ticks).

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
```
