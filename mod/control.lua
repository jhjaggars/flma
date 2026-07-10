-- flma (Factorio Live MCP Agent) — control.lua
--
-- Exports live game state to script-output/flma/<save_id>/ for a local
-- process (the factorio-live-mcp bridge, see apps/factorio-live-mcp/src/) to
-- read. Every export file is namespaced under a save_id (generated once,
-- persisted in `storage`) so switching between saves/servers on the same
-- machine never mixes or clobbers a different save's files; see
-- BASE_OUTPUT_DIR.."/current-save.json" below for how a consumer finds the
-- active one.
--
-- EFFICIENCY IS THE DESIGN CONSTRAINT, NOT AN AFTERTHOUGHT:
-- This mod is synced (it has a control stage, so its checksum must match every
-- peer's). Any per-tick work it does runs on the server AND every client, every
-- tick, for everyone in the game. Rules followed throughout this file:
--   1. Never hook on_tick. Only script.on_nth_tick(N) with a large configurable N.
--      (The one exception is the baseline building scan below, which registers
--      a temporary on_tick handler only while a scan is actually in flight and
--      unregisters itself the instant it's done.)
--   2. Prefer engine-aggregated reads (LuaFlowStatistics, LuaLogisticNetwork
--      contents) over scans — those are O(#item types), not O(#entities).
--   3. Never run surface.find_entities_filtered{} on a schedule. It is the one
--      genuinely expensive call available to a mod (O(#entities)). Buildings are
--      tracked with an incrementally-maintained index instead: one time-sliced
--      baseline scan when first enabled, then O(1) per build/mine event after.
--   4. All work is gated behind a single synced runtime-global flag. Disabled =
--      no registered on_nth_tick handlers and no registered build/destroy
--      handlers at all (not just an early-return) — verify with F4
--      "show-time-usage" that a disabled mod costs ~nothing.
--   5. Sanctioned exception to the single-tick budget: export_recipes() builds
--      and serializes an ~11 MB table in one tick. Its triggers are strictly
--      event-shaped — init, mod-configuration change, recipe-affecting research
--      (coalesced via a dirty flag), translation completion, an explicit remote
--      call — never the periodic schedule.
--
-- ON_LOAD SAFETY: reschedule() is called from on_load (via script.on_load),
-- where `game` is not accessible and `storage` must not be written (both are
-- desync/crash risks). Every code path reachable from reschedule(true) is
-- restricted to registering/unregistering event handlers and reading (never
-- writing) `storage`. Anything that touches `game.*` or writes `storage` — the
-- baseline scan's queue construction, the buildings-tracking on/off
-- transition — is only reachable from on_init, on_configuration_changed, or
-- on_runtime_mod_setting_changed.

local BASE_OUTPUT_DIR = "flma"

-- A short, mod-generated id (not the save's filename — Lua has no API to read
-- that, and filenames aren't stable across renames/copies/autosave rotation
-- anyway) that namespaces every export file under a directory unique to this
-- save. Generated once and persisted in `storage`, so it survives forever
-- with this save — this is what stops switching between saves/servers on one
-- machine from silently mixing or clobbering each other's script-output
-- files. Self-contained (doesn't call ensure_storage()) so it has no
-- dependency-ordering requirement on where it sits relative to that function.
local function save_id()
  storage.flma = storage.flma or {}
  storage.flma.save_id = storage.flma.save_id
    or string.format("%04x%04x", math.random(0, 0xffff), math.random(0, 0xffff))
  return storage.flma.save_id
end

local function output_dir()
  return BASE_OUTPUT_DIR .. "/" .. save_id()
end

-- Small fixed-location pointer a consumer reads first to find the
-- currently-active save's subdirectory, without being told the save_id out
-- of band or reconfigured every time the operator switches saves/servers.
-- Refreshed on every periodic export cycle and immediately when
-- flma-export-enabled turns on. Only ever called from contexts where `game`
-- is available (never on_load — see the file-level ON_LOAD SAFETY comment).
local function write_current_pointer()
  helpers.write_file(
    BASE_OUTPUT_DIR .. "/current-save.json",
    helpers.table_to_json({ save_id = save_id(), tick = game.tick }),
    false
  )
end

-- Entity types we don't consider a "placed building" for query_buildings.
-- This is a blocklist by Factorio's built-in prototype `type`, not by name —
-- every mod's custom entities (including all of pyanodons') still have to
-- declare one of these fixed engine type categories, so this generalizes to
-- any mod without per-mod maintenance. Two groups:
local BUILDING_TYPE_BLOCKLIST = {
  -- not player-placed at all
  ["resource"] = true,
  ["tree"] = true,
  ["fish"] = true,
  ["character"] = true,
  ["item-entity"] = true,
  ["particle-source"] = true,
  ["projectile"] = true,
  ["fire"] = true,
  ["smoke"] = true,
  ["smoke-with-trigger"] = true,
  ["explosion"] = true,
  ["corpse"] = true,
  ["rocket-silo-rocket"] = true,
  ["rocket-silo-rocket-shadow"] = true,
  ["highlight-box"] = true,
  ["flying-text"] = true,
  ["speech-bubble"] = true,
  -- mobile: a recorded position is stale the moment it moves. Also the type
  -- of every biter/spitter, so blocklisting it engine-side filters the
  -- constant stream of enemy on_entity_died events before they reach Lua.
  ["unit"] = true,
  -- player-placed, but high-cardinality "connective tissue" rather than
  -- production/logistics structures — on any real base (let alone a
  -- pyanodons-scale one) these vastly outnumber actual buildings and aren't
  -- useful to track positionally for query_buildings.
  ["transport-belt"] = true,
  ["underground-belt"] = true,
  ["splitter"] = true,
  ["linked-belt"] = true,
  ["loader"] = true,
  ["loader-1x1"] = true,
  ["pipe"] = true,
  ["pipe-to-ground"] = true,
  ["infinity-pipe"] = true,
  ["heat-pipe"] = true,
  ["electric-pole"] = true,
  ["inserter"] = true,
  ["straight-rail"] = true,
  ["curved-rail-a"] = true,
  ["curved-rail-b"] = true,
  ["half-diagonal-rail"] = true,
  ["legacy-straight-rail"] = true,
  ["legacy-curved-rail"] = true,
  ["elevated-straight-rail"] = true,
  ["elevated-curved-rail-a"] = true,
  ["elevated-curved-rail-b"] = true,
  ["elevated-half-diagonal-rail"] = true,
  ["rail-ramp"] = true,
  ["rail-support"] = true,
  ["rail-signal"] = true,
  ["rail-chain-signal"] = true,
}

-- Same set as a flat array, for passing to find_entities_filtered{type=..,
-- invert=true} so the engine excludes these natively instead of every entity
-- (including every resource tile and tree on the map) being materialized as a
-- Lua object just to get thrown away by is_building() below.
local NON_BUILDING_TYPES = (function()
  local t = {}
  for name, _ in pairs(BUILDING_TYPE_BLOCKLIST) do
    t[#t + 1] = name
  end
  return t
end)()

-- Same blocklist again, this time as a LuaEntityEventFilters-shaped array so
-- the *engine* discards non-buildings before a build/destroy event ever
-- reaches Lua — e.g. every biter death (on_entity_died) or belt placement
-- previously entered Lua just to be thrown away by is_building(). Every
-- build/destroy event registered below (LuaPlayerBuiltEntityEventFilter,
-- LuaEntityDiedEventFilter, LuaPlatformBuiltEntityEventFilter, etc.) accepts
-- this identical {filter, type, invert, mode} shape even though each is a
-- distinct concept type. ANDing every "not type T" clause together yields
-- "type is not any blocklisted type", i.e. the same set find_entities_filtered
-- excludes above.
local BUILDING_EVENT_FILTERS = (function()
  local f = {}
  for _, t in ipairs(NON_BUILDING_TYPES) do
    f[#f + 1] = { filter = "type", type = t, invert = true, mode = "and" }
  end
  return f
end)()

-- Number of entities to apply to the in-memory index per tick while draining
-- the baseline scan queue — keeps that from spiking a single frame even on a
-- huge base. Tuned conservatively; adjust after profiling with show-time-usage.
local BASELINE_CHUNK_SIZE = 500

-- Number of Factorio map chunks (32x32 tiles each) to query per tick while
-- collecting the baseline scan's candidate entities. Bounds each tick's
-- find_entities_filtered cost by chunk density, not total base size — a
-- megabase just takes more ticks overall, never a bigger single-tick spike.
local BASELINE_CHUNKS_PER_TICK = 5

--------------------------------------------------------------------------------
-- settings helpers
--------------------------------------------------------------------------------

local function export_enabled()
  return settings.global["flma-export-enabled"].value
end

local function tick_interval()
  return settings.global["flma-tick-interval"].value
end

local function inventories_enabled()
  return settings.global["flma-export-inventories"].value
end

local function buildings_enabled()
  return settings.global["flma-export-buildings"].value
end

local function compact_threshold()
  return settings.global["flma-buildings-compact-threshold"].value
end

--------------------------------------------------------------------------------
-- output helpers
--------------------------------------------------------------------------------

-- Overwrite a full-state file. No for_player filter: every peer (server and
-- every connected client) writes its own local copy to its own script-output —
-- that's the whole point, each machine's local bridge reads its own machine's
-- files. See apps/factorio-live-mcp/CLAUDE.md for why for_player is unnecessary
-- here (execution is already local per-peer; for_player only restricts *which*
-- peer performs the write, and we want all of them to).
local function write_snapshot(name, data)
  helpers.write_file(output_dir() .. "/" .. name .. ".json", helpers.table_to_json(data), false)
end

local function append_line(name, data)
  helpers.write_file(output_dir() .. "/" .. name .. ".ndjson", helpers.table_to_json(data) .. "\n", true)
end

-- Same as append_line but for many records in one call — one write_file
-- syscall instead of one per record. Used anywhere a batch of building
-- records needs writing at once (baseline scan draining, compaction), since
-- doing that one line at a time is its own O(#buildings)-syscalls cost.
local function append_lines_batch(name, records, append)
  if #records == 0 then
    return
  end
  local lines = {}
  for i, data in ipairs(records) do
    lines[i] = helpers.table_to_json(data)
  end
  helpers.write_file(output_dir() .. "/" .. name .. ".ndjson", table.concat(lines, "\n") .. "\n", append)
end

--------------------------------------------------------------------------------
-- tech tree export — event-driven, not scheduled. Full state is small
-- (bounded by technology count, not base size) so a full overwrite per
-- research event is simpler than diffing and still cheap.
--------------------------------------------------------------------------------

-- Shared by build_tech_table (tech.json) and export_research_snapshot
-- (research.json) — both need "the list of technology names currently
-- queued", just at different levels of detail.
local function research_queue_names(force)
  if not force.research_queue then
    return nil
  end
  local q = {}
  for _, t in pairs(force.research_queue) do
    q[#q + 1] = t.name
  end
  return q
end

-- Builds one force's tech-tree table. Does NOT write to disk itself: tech.json
-- covers every force in one snapshot (like production.json/logistics.json),
-- so writing per-force here would just have the last-iterated force clobber
-- the file.
local function build_tech_table(force)
  local techs = {}
  for name, tech in pairs(force.technologies) do
    local prereqs = {}
    for prereq_name, _ in pairs(tech.prerequisites) do
      prereqs[#prereqs + 1] = prereq_name
    end
    techs[name] = {
      researched = tech.researched,
      level = tech.level,
      enabled = tech.enabled,
      prerequisites = prereqs,
    }
  end

  local current = force.current_research
  return {
    current_research = current and current.name or nil,
    -- research_progress is a fraction 0-1 of the currently-researched technology
    research_progress = current and force.research_progress or nil,
    research_queue = research_queue_names(force),
    technologies = techs,
    -- The engine's own running total of this force's mining-drill yield bonus
    -- (e.g. 0.2 = +20% ore per mining operation, no extra energy/time cost) --
    -- whatever mix of techs contributed to it (vanilla's single infinite
    -- research, Pyanodons' many discrete mining-productivity-N techs, or any
    -- other mod's effects), reading the force property directly is exact
    -- where re-deriving it from individual tech effects would require picking
    -- one hardcoded per-modpack formula and keeping it in sync by hand.
    mining_drill_productivity_bonus = force.mining_drill_productivity_bonus,
  }
end

local function export_all_forces_tech()
  local forces_out = {}
  for force_name, force in pairs(game.forces) do
    forces_out[force_name] = build_tech_table(force)
  end
  write_snapshot("tech", { tick = game.tick, forces = forces_out })
end

-- Small per-force snapshot of just the "what's happening right now" research
-- fields (current_research/research_progress/research_queue), refreshed every
-- tick-interval cycle instead of only on research finished/reversed — those
-- two fields are otherwise stale for almost the entire duration of a research
-- (tech.json only updates when a research starts/finishes/is queued/is
-- cancelled/is reversed, all handled below, but research_progress itself
-- ticks up continuously between those events). Deliberately does NOT include
-- the full technologies table — that stays O(#technologies) and belongs to
-- tech.json; this file is O(#forces) only.
local function export_research_snapshot()
  local forces_out = {}
  for force_name, force in pairs(game.forces) do
    local current = force.current_research
    forces_out[force_name] = {
      current_research = current and current.name or nil,
      research_progress = current and force.research_progress or nil,
      research_queue = research_queue_names(force),
    }
  end
  write_snapshot("research", { tick = game.tick, forces = forces_out })
end

--------------------------------------------------------------------------------
-- production statistics — engine-aggregated (O(#item/fluid types)), scheduled.
--------------------------------------------------------------------------------

-- input_counts/output_counts are LIFETIME CUMULATIVE totals since the
-- force/game began (matching the "Total" figures in the in-game production
-- statistics GUI) — NOT rates. To also get a real per-minute rate, this calls
-- LuaFlowStatistics::get_flow_count per item/fluid name with
-- precision_index=one_minute and count=false: per the API docs, that returns
-- "the average across the provided precision time period" and "all return
-- values are normalized to be per-minute for all [non-electric] types" — so
-- the number really is "flow over roughly the last 60s", already in the units
-- we want. Still O(#item types): one extra call per name already present in
-- input_counts/output_counts, not a new scan.
local function flow_to_table(flow_stats)
  if not flow_stats then
    return nil
  end
  local input_rates = {}
  for name, _ in pairs(flow_stats.input_counts or {}) do
    input_rates[name] = flow_stats.get_flow_count({
      name = name,
      category = "input",
      precision_index = defines.flow_precision_index.one_minute,
      count = false,
    })
  end
  local output_rates = {}
  for name, _ in pairs(flow_stats.output_counts or {}) do
    output_rates[name] = flow_stats.get_flow_count({
      name = name,
      category = "output",
      precision_index = defines.flow_precision_index.one_minute,
      count = false,
    })
  end
  return {
    -- lifetime cumulative totals (unchanged, still useful for "how much have
    -- I ever made")
    input_counts = flow_stats.input_counts,
    output_counts = flow_stats.output_counts,
    -- real per-minute flow rates (the last ~60s), for "how much am I making
    -- right now"
    input_rates_per_min = input_rates,
    output_rates_per_min = output_rates,
  }
end

local function export_production_stats()
  local forces_out = {}
  for force_name, force in pairs(game.forces) do
    local surfaces_out = {}
    for _, surface in pairs(game.surfaces) do
      -- force.method (dot-access on a live instance) is already bound to that
      -- instance, so re-passing `force` as an explicit self argument shifts
      -- `surface` out of position — the engine then sees `force` where it
      -- expects a SurfaceIdentification and rejects it. Pass only the real
      -- argument.
      local ok_items, item_stats = pcall(force.get_item_production_statistics, surface)
      local ok_fluids, fluid_stats = pcall(force.get_fluid_production_statistics, surface)
      surfaces_out[surface.name] = {
        items = ok_items and flow_to_table(item_stats) or nil,
        fluids = ok_fluids and flow_to_table(fluid_stats) or nil,
      }
    end
    forces_out[force_name] = { surfaces = surfaces_out }
  end
  write_snapshot("production", { tick = game.tick, forces = forces_out })
end

--------------------------------------------------------------------------------
-- logistics networks — engine-aggregated, scheduled.
--------------------------------------------------------------------------------

local function export_logistics()
  local forces_out = {}
  for force_name, force in pairs(game.forces) do
    local networks_out = {}
    local by_surface = force.logistic_networks
    if by_surface then
      for surface_name, networks in pairs(by_surface) do
        for _, network in pairs(networks) do
          networks_out[#networks_out + 1] = {
            network_id = network.network_id,
            surface = surface_name,
            contents = network.get_contents(),
            available_logistic_robots = network.available_logistic_robots,
            available_construction_robots = network.available_construction_robots,
            all_logistic_robots = network.all_logistic_robots,
            all_construction_robots = network.all_construction_robots,
          }
        end
      end
    end
    forces_out[force_name] = networks_out
  end
  write_snapshot("logistics", { tick = game.tick, forces = forces_out })
end

--------------------------------------------------------------------------------
-- player inventories — O(#connected players), scheduled, opt-in (more
-- sensitive data than aggregate stats).
--------------------------------------------------------------------------------

local function export_inventories()
  local players_out = {}
  for _, player in pairs(game.connected_players) do
    local inv = player.get_main_inventory()
    players_out[player.name] = {
      contents = inv and inv.get_contents() or {},
      force = player.force.name,
      surface = player.surface.name,
    }
  end
  write_snapshot("inventories", { tick = game.tick, players = players_out })
end

--------------------------------------------------------------------------------
-- placed buildings — incremental index. The only O(#entities) operation is the
-- one-time baseline scan (chunked across ticks); every build/mine afterward is
-- O(1). storage.flma.buildings mirrors what's on disk so compaction can rewrite
-- buildings.ndjson from memory without re-scanning the world.
--------------------------------------------------------------------------------

-- Forces whose entities are never "placed buildings": biter nests/worms and
-- neutral map furniture (remnants, crash-site debris) aren't part of anyone's
-- factory. Checked Lua-side in is_building() rather than via engine event
-- filters — a "force" filter clause isn't uniformly supported across all the
-- built/died event filter types, and after blocklisting type "unit" above the
-- only enemy events left (spawner/worm deaths) are rare enough not to matter.
local FORCE_BLOCKLIST = {
  ["enemy"] = true,
  ["neutral"] = true,
}

local function is_building(entity)
  return entity and entity.valid
    and not BUILDING_TYPE_BLOCKLIST[entity.type]
    and not FORCE_BLOCKLIST[entity.force.name]
end

local function building_record(entity)
  local pos = entity.position
  return {
    id = entity.unit_number,
    name = entity.name,
    type = entity.type,
    surface = entity.surface.name,
    position = { x = pos.x, y = pos.y },
    force = entity.force and entity.force.name or nil,
  }
end

-- Updates the in-memory index only — no disk write. Returns the record on
-- success so callers can decide how to persist it (single append_line for a
-- live build event, or batched for the baseline scan).
local function apply_build(entity)
  if not is_building(entity) or not entity.unit_number then
    return nil
  end
  local rec = building_record(entity)
  if storage.flma.buildings[rec.id] == nil then
    storage.flma.building_count = storage.flma.building_count + 1
  end
  storage.flma.buildings[rec.id] = rec
  storage.flma.building_lines_since_compact = storage.flma.building_lines_since_compact + 1
  return rec
end

local function record_build(entity)
  local rec = apply_build(entity)
  if rec then
    append_line("buildings", { t = game.tick, op = "add", entity = rec })
    storage.flma.building_total_lines = storage.flma.building_total_lines + 1
  end
end

local function record_remove(entity)
  if not entity or not entity.unit_number then
    return
  end
  local id = entity.unit_number
  if storage.flma.buildings[id] then
    storage.flma.buildings[id] = nil
    storage.flma.building_count = storage.flma.building_count - 1
    storage.flma.building_lines_since_compact = storage.flma.building_lines_since_compact + 1
    append_line("buildings", { t = game.tick, op = "remove", id = id })
    storage.flma.building_total_lines = storage.flma.building_total_lines + 1
  end
end

-- Rewrite buildings.ndjson from the in-memory index (which is already
-- authoritative and cheap to hold — one small table entry per building).
-- This is O(#buildings) and runs in a single tick — an accepted tradeoff,
-- but only because maybe_compact_buildings() below now gates it so it only
-- runs when doing so actually halves the file, not on every threshold-full
-- batch of appends. Written as a single batched write (not one append_line
-- call per building) — on a large base the difference is one write_file
-- syscall vs. tens of thousands of them in the same tick.
local function compact_buildings()
  local records = {}
  for _, rec in pairs(storage.flma.buildings) do
    records[#records + 1] = { t = game.tick, op = "add", entity = rec }
  end
  helpers.write_file(output_dir() .. "/buildings.ndjson", "", false) -- truncate
  append_lines_batch("buildings", records, true)
  storage.flma.building_lines_since_compact = 0
  storage.flma.building_total_lines = #records
end

-- Compacting rewrites the *entire* index every time, so it's only worth the
-- O(#buildings) cost when it meaningfully shrinks the file. If churn since
-- the last compact was mostly/all adds, the file is already close to the
-- index size and a compact would rewrite essentially identical content —
-- which also defeats the bridge's shrink-based compaction detection (it
-- looks for the file getting *smaller*). So: only compact once the file has
-- grown to at least 2x the current index size, on top of the existing
-- append-count threshold. Compares against storage.flma.building_count (an
-- incrementally-maintained counter, see apply_build/record_remove) rather
-- than counting storage.flma.buildings here, so this stays O(1) — deciding
-- *not* to compact should never itself cost O(#buildings).
local function maybe_compact_buildings()
  if storage.flma.building_lines_since_compact < compact_threshold() then
    return
  end
  if storage.flma.building_total_lines >= 2 * storage.flma.building_count then
    compact_buildings()
  end
end

-- Time-sliced baseline scan, in two phases so nothing does O(#entities) or
-- O(#buildings) work in a single tick:
--   1. Collecting: BASELINE_CHUNKS_PER_TICK map chunks' worth of candidate
--      entities per tick, using find_entities_filtered{area=chunk.area,
--      type=NON_BUILDING_TYPES, invert=true} so the engine excludes
--      resources/trees/belts/pipes/etc. natively and each call is bounded by
--      one chunk's entity density — not by surface size or total base size.
--      Earlier versions called find_entities_filtered once per *surface*
--      (or, worse, with no filter at all), which is exactly the O(#entities)
--      full-map operation this design is supposed to avoid.
--   2. Draining: BASELINE_CHUNK_SIZE entities applied to the in-memory index
--      per tick, written with one batched append_lines_batch call per tick
--      instead of one write_file syscall per entity.
--
-- Split into two pieces for on_load safety:
--   - baseline_tick_handler/register_baseline_drainer: pure event-handler
--     registration, driven entirely off storage.flma.baseline_* state. Safe
--     to call from on_load (touches neither `game` nor `storage` itself at
--     registration time — only when the handler later actually runs on a
--     live tick, which on_load never does directly).
--   - start_baseline_scan: builds the chunk queue (game.surfaces) and writes
--     the initial storage.flma.baseline_* state. Only callable from
--     on_init/on_configuration_changed/on_runtime_mod_setting_changed.
local function baseline_tick_handler()
  if storage.flma.baseline_collecting then
    local chunks = storage.flma.baseline_chunks
    local ci = storage.flma.baseline_chunk_index
    if ci > #chunks then
      storage.flma.baseline_collecting = false
      storage.flma.baseline_chunks = nil
      return
    end
    local last = math.min(ci + BASELINE_CHUNKS_PER_TICK - 1, #chunks)
    local entities = storage.flma.baseline_entities
    for k = ci, last do
      local c = chunks[k]
      local found = c.surface.find_entities_filtered({
        area = c.area,
        type = NON_BUILDING_TYPES,
        invert = true,
      })
      for _, entity in pairs(found) do
        if entity.unit_number then
          entities[#entities + 1] = entity
        end
      end
    end
    storage.flma.baseline_chunk_index = last + 1
    return
  end

  local entities = storage.flma.baseline_entities
  local i = storage.flma.baseline_entity_index
  local last = math.min(i + BASELINE_CHUNK_SIZE - 1, #entities)
  local batch = {}
  for j = i, last do
    local e = entities[j]
    -- Skip entities already indexed: find_entities_filtered{area=chunk} returns
    -- every entity *intersecting* the chunk, so a building straddling a chunk
    -- border is collected once per adjacent chunk — the index dedupes by id,
    -- but without this check each extra copy still wrote a redundant add line
    -- (~7% of the baseline on a real save). Also covers entities built mid-scan,
    -- whose add line was already appended by the live build handler.
    local already = e.valid and e.unit_number and storage.flma.buildings[e.unit_number]
    if not already then
      local rec = apply_build(e)
      if rec then
        batch[#batch + 1] = { t = game.tick, op = "add", entity = rec }
      end
    end
  end
  append_lines_batch("buildings", batch, true)
  storage.flma.building_total_lines = storage.flma.building_total_lines + #batch
  storage.flma.baseline_entity_index = last + 1
  if storage.flma.baseline_entity_index > #entities then
    script.on_event(defines.events.on_tick, nil) -- unregister: baseline done
    storage.flma.baseline_entities = nil
    storage.flma.buildings_initialized = true
    storage.flma.baseline_in_progress = false
    maybe_compact_buildings() -- fold the baseline dump into one compacted write
  end
end

local function register_baseline_drainer()
  script.on_event(defines.events.on_tick, baseline_tick_handler)
end

local function start_baseline_scan()
  local chunks_queue = {}
  for _, surface in pairs(game.surfaces) do
    for chunk in surface.get_chunks() do
      chunks_queue[#chunks_queue + 1] = { surface = surface, area = chunk.area }
    end
  end
  storage.flma.baseline_chunks = chunks_queue
  storage.flma.baseline_chunk_index = 1
  storage.flma.baseline_entities = {}
  storage.flma.baseline_entity_index = 1
  storage.flma.baseline_collecting = true
  storage.flma.baseline_in_progress = true
  register_baseline_drainer()
end

--------------------------------------------------------------------------------
-- recipe/prototype dump — recipes.json. Byte-compatible with the RecipeExporter
-- mod's script-output/recipes.json (github.com/FactorioCalc/RecipeExporter) so
-- recipe-mcp's build_db.py consumes it unchanged. ~11 MB on a Space Age game:
-- built and serialized in a single tick, which is the sanctioned exception to
-- the per-tick cost rules above — it happens only on init, on mod-configuration
-- change, when a finished/reversed research actually unlocks recipes (coalesced
-- via a dirty flag to at most one write per tick-interval), on translation-pass
-- completion, and on remote.call("flma", "export_recipes"). Never periodically.
--
-- Unlike RecipeExporter (which holds the whole dump in a Lua upvalue across
-- ticks while translations trickle in — not save-safe), only the small
-- name → translated-string maps live in storage; the dump itself is rebuilt
-- from prototypes (deterministic) on every write.
--------------------------------------------------------------------------------

-- Array of a table's keys, or nil when the table is nil/empty — empty sets are
-- *absent* from the JSON rather than serialized as the ambiguous `{}`,
-- matching RecipeExporter's keys() helper exactly.
local function table_keys(t)
  if t == nil then
    return nil
  end
  local out = {}
  for k, _ in pairs(t) do
    out[#out + 1] = k
  end
  if #out == 0 then
    return nil
  end
  return out
end

-- The recipes.json format is single-force (per-force fields: recipe
-- enabled/productivity_bonus, tech enabled/researched), unlike tech.json's
-- all-forces shape. Use the "player" force, falling back to the first
-- non-blocklisted force for exotic scenarios that renamed it.
local function recipes_force()
  local f = game.forces["player"]
  if f then
    return f
  end
  for name, force in pairs(game.forces) do
    if not FORCE_BLOCKLIST[name] then
      return force
    end
  end
end

local TRANSLATION_SECTIONS = { "quality", "recipes", "items", "fluids", "entities", "technologies" }

local function empty_translation_tables()
  local t = {}
  for _, s in ipairs(TRANSLATION_SECTIONS) do
    t[s] = {}
  end
  return t
end

-- Best-effort translated_name lookup; nil (field absent) until a translation
-- pass has completed. Consumers must fall back to the internal name.
local function tr(section, name)
  local maps = storage.flma and storage.flma.recipe_translations
  local m = maps and maps[section]
  return m and m[name] or nil
end

local function build_recipes_export()
  local force = recipes_force()
  local data = {}
  data.game_version = script.active_mods["base"]

  data.groups = {}
  local function add_group(group)
    if not data.groups[group.name] then
      local g = {
        name = group.name,
        type = group.type,
        order = group.order,
      }
      if group.type == "item-group" then
        g.order_in_recipe = group.order_in_recipe
      end
      data.groups[group.name] = g
    end
    return group.name
  end

  data.quality = {}
  for _, v in pairs(prototypes.quality) do
    local q = {
      name = v.name,
      level = v.level,
      next_probability = v.next_probability,
      beacon_power_usage_multiplier = v.beacon_power_usage_multiplier,
      mining_drill_resource_drain_multiplier = v.mining_drill_resource_drain_multiplier,
      group = add_group(v.group),
      subgroup = add_group(v.subgroup),
      translated_name = tr("quality", v.name),
    }
    if v.next then
      q.next = v.next.name
    end
    data.quality[v.name] = q
  end

  data.quality_names = {}
  do
    local i = 1
    local name = "normal"
    repeat
      data.quality_names[i] = name
      name = data.quality[name] and data.quality[name].next or nil
      i = i + 1
    until name == nil
  end

  data.recipes = {}
  for _, v in pairs(force.recipes) do
    data.recipes[v.name] = {
      name = v.name,
      category = v.category,
      ingredients = v.ingredients,
      products = v.products,
      main_product = v.prototype.main_product,
      allowed_effects = v.prototype.allowed_effects,
      maximum_productivity = v.prototype.maximum_productivity,
      energy = v.energy,
      order = v.order,
      group = add_group(v.group),
      subgroup = add_group(v.subgroup),
      enabled = v.enabled,
      productivity_bonus = v.productivity_bonus,
      translated_name = tr("recipes", v.name),
    }
  end

  data.items = {}
  for _, v in pairs(prototypes.item) do
    data.items[v.name] = {
      name = v.name,
      type = v.type,
      order = v.order,
      group = add_group(v.group),
      subgroup = add_group(v.subgroup),
      stack_size = v.stack_size,
      weight = v.weight,
      fuel_category = v.fuel_category,
      fuel_value = v.fuel_value,
      module_effects = v.module_effects,
      rocket_launch_products = v.rocket_launch_products,
      flags = table_keys(v.flags),
      translated_name = tr("items", v.name),
    }
  end

  data.fluids = {}
  for _, v in pairs(prototypes.fluid) do
    data.fluids[v.name] = {
      name = v.name,
      order = v.order,
      group = add_group(v.group),
      subgroup = add_group(v.subgroup),
      fuel_value = v.fuel_value,
      translated_name = tr("fluids", v.name),
    }
  end

  data.entities = {}
  for _, v in pairs(prototypes.entity) do
    local etype = v.type
    if etype == "beacon"
        or etype == "furnace"
        or etype == "assembling-machine"
        or etype == "boiler"
        or etype == "rocket-silo" then
      local energy_consumption = nil
      local drain = nil
      local energy_source = nil
      local fuel_categories = nil
      local burner_effectivity = nil
      if v.electric_energy_source_prototype and v.energy_usage ~= nil then
        energy_consumption = v.energy_usage * 60
        drain = v.electric_energy_source_prototype.drain * 60
        energy_source = "electric"
      elseif v.burner_prototype and v.energy_usage ~= nil then
        energy_consumption = v.energy_usage * 60
        drain = 0
        energy_source = "burner"
        fuel_categories = table_keys(v.burner_prototype.fuel_categories)
        burner_effectivity = v.burner_prototype.effectivity
      end
      local entity_info = {
        name = v.name,
        type = etype,
        order = v.order,
        group = v.group.name,
        subgroup = v.subgroup.name,
        crafting_speed = {},
        crafting_categories = table_keys(v.crafting_categories),
        allowed_effects = table_keys(v.allowed_effects),
        module_inventory_size = v.module_inventory_size,
        fixed_recipe = v.fixed_recipe,
        effect_receiver = v.effect_receiver,
        rocket_parts_required = v.rocket_parts_required,
        distribution_effectivity = v.distribution_effectivity,
        distribution_effectivity_bonus_per_quality_level = v.distribution_effectivity_bonus_per_quality_level,
        supply_area_distance = {},
        energy_consumption = energy_consumption,
        drain = drain,
        energy_source = energy_source,
        fuel_categories = fuel_categories,
        burner_effectivity = burner_effectivity,
        width = v.tile_width,
        height = v.tile_height,
        flags = table_keys(v.flags),
        translated_name = tr("entities", v.name),
      }
      for _, qname in pairs(data.quality_names) do
        entity_info.crafting_speed[qname] = v.get_crafting_speed(qname)
        entity_info.supply_area_distance[qname] = v.get_supply_area_distance(qname)
      end
      if not next(entity_info.crafting_speed) then
        entity_info.crafting_speed = nil
      end
      if not next(entity_info.supply_area_distance) then
        entity_info.supply_area_distance = nil
      end
      data.entities[v.name] = entity_info
    elseif etype == "mining-drill" then
      local energy_consumption = nil
      local drain = nil
      local energy_source = nil
      local fuel_categories = nil
      local burner_effectivity = nil
      if v.electric_energy_source_prototype and v.energy_usage ~= nil then
        energy_consumption = v.energy_usage * 60
        drain = v.electric_energy_source_prototype.drain * 60
        energy_source = "electric"
      elseif v.burner_prototype and v.energy_usage ~= nil then
        energy_consumption = v.energy_usage * 60
        drain = 0
        energy_source = "burner"
        fuel_categories = table_keys(v.burner_prototype.fuel_categories)
        burner_effectivity = v.burner_prototype.effectivity
      end
      data.entities[v.name] = {
        name = v.name,
        type = etype,
        resource_categories = table_keys(v.resource_categories),
        mining_speed = v.mining_speed,
        module_inventory_size = v.module_inventory_size,
        energy_consumption = energy_consumption,
        drain = drain,
        energy_source = energy_source,
        fuel_categories = fuel_categories,
        burner_effectivity = burner_effectivity,
        width = v.tile_width,
        height = v.tile_height,
        translated_name = tr("entities", v.name),
      }
    elseif etype == "resource" then
      local mp = v.mineable_properties
      if mp and mp.minable then
        data.entities[v.name] = {
          name = v.name,
          type = etype,
          resource_category = v.resource_category,
          mining_time = mp.mining_time,
          required_fluid = mp.required_fluid,
          fluid_amount = mp.fluid_amount,
          product_name = mp.products and mp.products[1] and mp.products[1].name or nil,
          translated_name = tr("entities", v.name),
        }
      end
    elseif etype == "generator" then
      -- Fluid-driven electricity generators (e.g. vanilla steam-engine, or
      -- pyanodons' steam-turbine-mk01..04). Deliberately excludes
      -- "electric-energy-interface" entities (e.g. pyanodons' wind
      -- turbines) -- those have no static prototype power figure at all;
      -- their output is live per-instance state (LuaEntity.power_production,
      -- adjusted at runtime by their owning mod's own simulation), not
      -- prototype data this export can represent.
      local input_fluid = nil
      local fb = v.fluidbox_prototypes
      if fb and fb[1] and fb[1].filter then
        input_fluid = fb[1].filter.name
      end
      data.entities[v.name] = {
        name = v.name,
        type = etype,
        order = v.order,
        group = v.group.name,
        subgroup = v.subgroup.name,
        max_power_output = v.max_power_output,
        fluid_usage_per_sec = v.fluid_usage_per_tick and (v.fluid_usage_per_tick * 60) or nil,
        effectivity = v.effectivity,
        maximum_temperature = v.maximum_temperature,
        input_fluid = input_fluid,
        width = v.tile_width,
        height = v.tile_height,
        translated_name = tr("entities", v.name),
      }
    end
  end

  data.technologies = {}
  for name, v in pairs(force.technologies) do
    local recipes_unlocked = {}
    for _, effect in pairs(v.prototype.effects) do
      if effect.type == "unlock-recipe" then
        recipes_unlocked[#recipes_unlocked + 1] = effect.recipe
      end
    end
    local prerequisites = {}
    for prereq_name, _ in pairs(v.prerequisites) do
      prerequisites[#prerequisites + 1] = prereq_name
    end
    local unit_ingredients = {}
    for _, ing in pairs(v.research_unit_ingredients) do
      unit_ingredients[#unit_ingredients + 1] = { name = ing.name, amount = ing.amount }
    end
    data.technologies[name] = {
      name = name,
      enabled = v.enabled,
      researched = v.researched,
      prerequisites = prerequisites,
      recipes_unlocked = recipes_unlocked,
      unit_count = v.research_unit_count,
      unit_count_formula = v.research_unit_count_formula,
      unit_energy = v.research_unit_energy,
      unit_ingredients = unit_ingredients,
      translated_name = tr("technologies", name),
    }
  end

  return data
end

-- The single write entry point for every trigger. pcall so a pathological
-- modded prototype (e.g. an inf/NaN value helpers.table_to_json refuses to
-- serialize) can't take down the other export paths that call this.
local function export_recipes()
  local ok, err = pcall(function()
    write_snapshot("recipes", build_recipes_export())
  end)
  storage.flma.recipes_dirty = false
  if not ok then
    log("flma: recipes.json export failed: " .. tostring(err))
  end
end

-- Only recipe-unlocking (or recipe-productivity) research changes anything in
-- recipes.json — damage/speed upgrades, the bulk of late-game research, never
-- trigger the 11 MB rewrite.
local function research_affects_recipes(tech)
  if not (tech and tech.valid) then
    return false
  end
  for _, effect in pairs(tech.prototype.effects) do
    if effect.type == "unlock-recipe" or effect.type == "change-recipe-productivity" then
      return true
    end
  end
  return false
end

--------------------------------------------------------------------------------
-- localised-name translation pass — best-effort, two-phase. recipes.json is
-- always written immediately with internal names only (works headless, no
-- player needed); when a player is connected, request_translation is issued
-- for every exported prototype and the file is rewritten with translated_name
-- filled in once all results arrive.
--
-- DESYNC SAFETY: request_translation results come back as broadcast input
-- actions, so on_string_translated fires with identical data on *every* peer.
-- The pending map and translation results live in synced storage, and
-- reschedule() re-registers the handler on any peer whose storage shows a
-- pass in flight (including a client that joins mid-pass) — every peer must
-- run the same storage mutations or checksums diverge.
--------------------------------------------------------------------------------

-- Translation request ids don't survive a save/load or the requesting player
-- disconnecting — after this many ticks a pending pass is considered dead and
-- the next trigger starts a fresh one. Already-received translations are kept
-- in storage, so a restarted pass only requests what's still missing.
local TRANSLATION_STALE_TICKS = 3600

-- The pass is time-sliced like the baseline building scan. Issuing every
-- request in a single tick was field-tested to get the requesting client
-- dropped from the game on a pyanodons-scale prototype set (18,704 requests
-- in the tick it joined). This bounds it to TRANSLATION_REQUESTS_PER_DRAIN
-- requests per TRANSLATION_DRAIN_TICKS ticks (~1,200/s → a full pyanodons
-- pass in ~16s).
local TRANSLATION_REQUESTS_PER_DRAIN = 100
-- Must never equal flma-tick-interval (minimum 60): a second on_nth_tick
-- registration with the same N would silently replace the periodic handler.
local TRANSLATION_DRAIN_TICKS = 5

local function on_string_translated(event)
  local st = storage.flma
  local pending = st and st.recipe_translation_pending
  local entry = pending and pending[event.id]
  if not entry then
    return -- unrelated translation (another mod's request)
  end
  if event.translated then
    st.recipe_translations[entry.section][entry.key] = event.result
  end
  pending[event.id] = nil
  st.recipe_translation_pending_count = st.recipe_translation_pending_count - 1
  if st.recipe_translation_pending_count <= 0 and st.recipe_translation_queue == nil then
    -- Every queued request has been issued and answered.
    st.recipe_translation_pending = nil
    st.recipe_translation_pending_count = 0
    st.recipe_translations_done = true
    script.on_event(defines.events.on_string_translated, nil)
    export_recipes() -- rewrite with translated_name filled in
  end
end

-- The queue stores only {section, key}; the LocalisedString is re-resolved
-- from prototypes at request time so storage never holds 18k localised-string
-- tables across saves.
local function localised_name_for(force, section, key)
  if section == "quality" then
    local p = prototypes.quality[key]
    return p and p.localised_name
  elseif section == "items" then
    local p = prototypes.item[key]
    return p and p.localised_name
  elseif section == "fluids" then
    local p = prototypes.fluid[key]
    return p and p.localised_name
  elseif section == "entities" then
    local p = prototypes.entity[key]
    return p and p.localised_name
  elseif section == "recipes" then
    local r = force and force.recipes[key]
    return r and r.localised_name
  elseif section == "technologies" then
    local t = force and force.technologies[key]
    return t and t.localised_name
  end
end

local function abort_translation_pass()
  local st = storage.flma
  st.recipe_translation_queue = nil
  st.recipe_translation_queue_index = nil
  st.recipe_translation_player_index = nil
  st.recipe_translation_pending = nil
  st.recipe_translation_pending_count = 0
  script.on_nth_tick(TRANSLATION_DRAIN_TICKS, nil)
  script.on_event(defines.events.on_string_translated, nil)
end

local translation_drain_handler

local function register_translation_drainer()
  script.on_nth_tick(TRANSLATION_DRAIN_TICKS, translation_drain_handler)
end

translation_drain_handler = function()
  local st = storage.flma
  local queue = st.recipe_translation_queue
  if not queue then
    script.on_nth_tick(TRANSLATION_DRAIN_TICKS, nil)
    return
  end
  local player = st.recipe_translation_player_index
    and game.get_player(st.recipe_translation_player_index)
  if not (player and player.valid and player.connected) then
    -- Requester left mid-pass: keep what's translated so far, drop the rest.
    -- The next on_player_joined_game starts a fresh, incremental pass.
    abort_translation_pass()
    return
  end
  local force = recipes_force()
  local i = st.recipe_translation_queue_index
  local last = math.min(i + TRANSLATION_REQUESTS_PER_DRAIN - 1, #queue)
  for j = i, last do
    local entry = queue[j]
    local ls = localised_name_for(force, entry.section, entry.key)
    if ls then
      local id = player.request_translation(ls)
      if id then
        st.recipe_translation_pending[id] = entry
        st.recipe_translation_pending_count = st.recipe_translation_pending_count + 1
      end
    end
  end
  st.recipe_translation_queue_index = last + 1
  if st.recipe_translation_queue_index > #queue then
    st.recipe_translation_queue = nil
    st.recipe_translation_queue_index = nil
    script.on_nth_tick(TRANSLATION_DRAIN_TICKS, nil)
    if st.recipe_translation_pending_count <= 0 then
      -- Nothing was actually requested (all resolvable strings already
      -- translated) — no responses to wait for; finish now.
      st.recipe_translation_pending = nil
      st.recipe_translations_done = true
      script.on_event(defines.events.on_string_translated, nil)
      export_recipes()
    end
  end
end

-- Runtime contexts only (writes storage) — never reachable from on_load.
local function start_translation_pass(player)
  if not export_enabled() then
    return
  end
  local st = storage.flma
  if st.recipe_translations_done then
    return
  end
  if not (player and player.valid and player.connected) then
    return
  end
  local in_flight = st.recipe_translation_queue ~= nil or st.recipe_translation_pending ~= nil
  if in_flight and game.tick - (st.recipe_translation_started_tick or 0) <= TRANSLATION_STALE_TICKS then
    return -- a pass is already in flight and not stale
  end

  st.recipe_translations = st.recipe_translations or empty_translation_tables()
  local translations = st.recipe_translations
  local queue = {}
  local function enqueue(section, key)
    -- Incremental: skip anything a previous (aborted) pass already got.
    if not translations[section][key] then
      queue[#queue + 1] = { section = section, key = key }
    end
  end

  -- Exactly the prototype sets build_recipes_export() emits.
  for _, v in pairs(prototypes.quality) do
    enqueue("quality", v.name)
  end
  local force = recipes_force()
  for _, v in pairs(force.recipes) do
    enqueue("recipes", v.name)
  end
  for _, v in pairs(prototypes.item) do
    enqueue("items", v.name)
  end
  for _, v in pairs(prototypes.fluid) do
    enqueue("fluids", v.name)
  end
  for _, v in pairs(prototypes.entity) do
    local etype = v.type
    if etype == "beacon" or etype == "furnace" or etype == "assembling-machine"
        or etype == "boiler" or etype == "rocket-silo" or etype == "mining-drill"
        or etype == "generator" then
      enqueue("entities", v.name)
    elseif etype == "resource" then
      local mp = v.mineable_properties
      if mp and mp.minable then
        enqueue("entities", v.name)
      end
    end
  end
  for _, v in pairs(force.technologies) do
    enqueue("technologies", v.name)
  end

  if #queue == 0 then
    -- Everything already translated (e.g. a prior pass finished requesting
    -- but its done-flag reset was interrupted) — just embed what we have.
    st.recipe_translations_done = true
    export_recipes()
    return
  end
  st.recipe_translation_queue = queue
  st.recipe_translation_queue_index = 1
  st.recipe_translation_player_index = player.index
  st.recipe_translation_pending = {}
  st.recipe_translation_pending_count = 0
  st.recipe_translation_started_tick = game.tick
  script.on_event(defines.events.on_string_translated, on_string_translated)
  register_translation_drainer()
end

-- Kick a translation pass off the first connected player, if any — used from
-- the lifecycle hooks right after an untranslated export. On a headless server
-- with nobody connected this is a no-op; the on_player_joined_game handler
-- picks it up when someone eventually joins.
local function maybe_start_translation_pass()
  if #game.connected_players > 0 then
    start_translation_pass(game.connected_players[1])
  end
end

local function on_player_joined(event)
  start_translation_pass(game.get_player(event.player_index))
end

--------------------------------------------------------------------------------
-- scheduler — wires up (or tears down) every handler based on current
-- settings. Called from on_init, on_load, on_configuration_changed, and
-- on_runtime_mod_setting_changed so registration always matches the synced
-- settings deterministically on every peer.
--------------------------------------------------------------------------------

local function ensure_storage()
  storage.flma = storage.flma or {}
  storage.flma.buildings = storage.flma.buildings or {}
  storage.flma.buildings_initialized = storage.flma.buildings_initialized or false
  storage.flma.building_lines_since_compact = storage.flma.building_lines_since_compact or 0
  storage.flma.building_total_lines = storage.flma.building_total_lines or 0
  if storage.flma.building_count == nil then
    -- Migrating from a save written before this counter existed: count once
    -- here (on_init/on_configuration_changed only, never on a schedule) so
    -- maybe_compact_buildings can stay O(1) from here on.
    local n = 0
    for _ in pairs(storage.flma.buildings) do
      n = n + 1
    end
    storage.flma.building_count = n
  end
  if storage.flma.baseline_in_progress == nil then
    storage.flma.baseline_in_progress = false
  end
  if storage.flma.tracking_was_active == nil then
    storage.flma.tracking_was_active = false
  end
  storage.flma.recipe_translations = storage.flma.recipe_translations or empty_translation_tables()
  if storage.flma.recipes_dirty == nil then
    storage.flma.recipes_dirty = false
  end
  if storage.flma.recipe_translations_done == nil then
    storage.flma.recipe_translations_done = false
  end
  if storage.flma.recipe_translation_pending_count == nil then
    storage.flma.recipe_translation_pending_count = 0
  end
end

local BUILD_EVENTS = {
  defines.events.on_built_entity,
  defines.events.on_robot_built_entity,
  defines.events.script_raised_built,
  defines.events.script_raised_revive,
}
-- Space Age platform building — guarded so the mod still loads fine on a
-- hypothetical build where these defines are absent.
if defines.events.on_space_platform_built_entity then
  BUILD_EVENTS[#BUILD_EVENTS + 1] = defines.events.on_space_platform_built_entity
end

local DESTROY_EVENTS = {
  defines.events.on_player_mined_entity,
  defines.events.on_robot_mined_entity,
  defines.events.on_entity_died,
  defines.events.script_raised_destroy,
}
if defines.events.on_space_platform_mined_entity then
  DESTROY_EVENTS[#DESTROY_EVENTS + 1] = defines.events.on_space_platform_mined_entity
end

local function on_build_event(event)
  record_build(event.entity)
  maybe_compact_buildings()
end

local function on_destroy_event(event)
  record_remove(event.entity)
  maybe_compact_buildings()
end

-- Detects an export_enabled+buildings_enabled active -> inactive transition
-- (either switch going off stops build/destroy events from being recorded)
-- and, on the inactive -> active transition back, forces a fresh baseline
-- scan: events during the "off" window are unrecoverably lost, so the index
-- would otherwise silently drift from the real world forever. Only touches
-- `storage` — safe from on_configuration_changed/on_runtime_mod_setting_changed,
-- never from on_load.
local function handle_buildings_tracking_transition()
  ensure_storage()
  local now_active = export_enabled() and buildings_enabled()
  local was_active = storage.flma.tracking_was_active
  if was_active and not now_active then
    storage.flma.buildings = {}
    storage.flma.building_count = 0
    storage.flma.buildings_initialized = false
    storage.flma.building_lines_since_compact = 0
    storage.flma.building_total_lines = 0
    storage.flma.baseline_in_progress = false
    storage.flma.baseline_chunks = nil
    storage.flma.baseline_entities = nil
    helpers.write_file(output_dir() .. "/buildings.ndjson", "", false) -- truncate
  end
  storage.flma.tracking_was_active = now_active
end

-- reschedule() is called from on_init, on_load, on_configuration_changed, and
-- on_runtime_mod_setting_changed — it has to be safe under the *most*
-- restrictive of those (on_load: no `game`, no `storage` writes), so
-- `from_on_load` gates every branch that would violate that. See the
-- file-level "ON_LOAD SAFETY" comment at the top.
local function reschedule(from_on_load)
  -- Always start clean: unregister everything, then re-register only what's
  -- enabled. Keeps this idempotent and safe to call from on_load. Includes
  -- the baseline on_tick drainer, so toggling flma-export-enabled off mid-scan
  -- actually stops all work rather than leaving an orphaned on_tick handler.
  script.on_nth_tick(nil)
  script.on_event(defines.events.on_research_finished, nil)
  script.on_event(defines.events.on_research_reversed, nil)
  script.on_event(defines.events.on_research_started, nil)
  script.on_event(defines.events.on_research_cancelled, nil)
  script.on_event(BUILD_EVENTS, nil)
  script.on_event(DESTROY_EVENTS, nil)
  script.on_event(defines.events.on_tick, nil)
  script.on_event(defines.events.on_string_translated, nil)
  script.on_event(defines.events.on_player_joined_game, nil)

  if not export_enabled() then
    return -- fully idle: zero registered handlers, zero per-tick cost
  end

  -- Reattach the translation-result handler and request drainer on any peer
  -- whose (synced) storage shows a pass in flight — mandatory for desync
  -- safety, see the translation section above. Read-only on storage, so
  -- on_load-safe. (Reattached request ids are dead after a reload — the
  -- drainer keeps issuing the rest of the queue with fresh ids, and the
  -- stale-pass timeout covers a pass that was already fully issued.)
  if storage.flma and (storage.flma.recipe_translation_pending or storage.flma.recipe_translation_queue) then
    script.on_event(defines.events.on_string_translated, on_string_translated)
  end
  if storage.flma and storage.flma.recipe_translation_queue then
    register_translation_drainer()
  end
  script.on_event(defines.events.on_player_joined_game, on_player_joined)

  local n = tick_interval()
  script.on_nth_tick(n, function()
    write_current_pointer()
    -- O(1) when clean; coalesces research bursts into at most one recipes.json
    -- rewrite per interval. The write itself is never periodic.
    if storage.flma.recipes_dirty then
      export_recipes()
    end
    export_production_stats()
    export_logistics()
    export_research_snapshot()
    if inventories_enabled() then
      export_inventories()
    end
  end)

  script.on_event(defines.events.on_research_finished, function(event)
    export_all_forces_tech()
    if research_affects_recipes(event.research) then
      storage.flma.recipes_dirty = true
    end
  end)
  script.on_event(defines.events.on_research_reversed, function(event)
    export_all_forces_tech()
    if research_affects_recipes(event.research) then
      storage.flma.recipes_dirty = true
    end
  end)
  script.on_event(defines.events.on_research_started, function()
    export_all_forces_tech()
  end)
  script.on_event(defines.events.on_research_cancelled, function()
    export_all_forces_tech()
  end)

  if buildings_enabled() then
    -- One registration per event: the engine rejects filters on an
    -- array-of-events registration ("Filters can only be used when
    -- registering single non custom-input events").
    for _, ev in ipairs(BUILD_EVENTS) do
      script.on_event(ev, on_build_event, BUILDING_EVENT_FILTERS)
    end
    for _, ev in ipairs(DESTROY_EVENTS) do
      script.on_event(ev, on_destroy_event, BUILDING_EVENT_FILTERS)
    end
    if from_on_load then
      -- Never start a new scan from on_load (game/storage writes are
      -- off-limits) — only reattach the drainer if one was already in
      -- flight when the game was saved. Read-only; storage.flma should
      -- always exist by the time on_load can run (on_init/
      -- on_configuration_changed always run first for any save), but guard
      -- the read anyway rather than assume it.
      if storage.flma and storage.flma.baseline_in_progress then
        register_baseline_drainer()
      end
    elseif not storage.flma.buildings_initialized then
      if storage.flma.baseline_in_progress then
        -- A scan was already queued (e.g. a setting changed again mid-scan,
        -- re-entering reschedule) — keep draining the existing queue rather
        -- than restarting it, which would duplicate every record already
        -- written for entities already scanned.
        register_baseline_drainer()
      else
        start_baseline_scan()
      end
    end
  end
end

--------------------------------------------------------------------------------
-- lifecycle
--------------------------------------------------------------------------------

script.on_init(function()
  ensure_storage()
  handle_buildings_tracking_transition()
  reschedule(false)
  if export_enabled() then
    export_all_forces_tech()
    export_research_snapshot()
    export_recipes()
    maybe_start_translation_pass()
  end
end)

script.on_load(function()
  -- storage already restored by the engine here; `game` is not available and
  -- `storage` must not be written. reschedule(true) only registers handlers
  -- and reads storage — see its own comment and the file-level one at top.
  reschedule(true)
end)

script.on_configuration_changed(function()
  ensure_storage()
  -- Mods changed: the recipe/prototype set and its localised strings may have
  -- changed with them — throw away all translation state and re-export.
  storage.flma.recipe_translations = empty_translation_tables()
  storage.flma.recipe_translations_done = false
  storage.flma.recipe_translation_pending = nil
  storage.flma.recipe_translation_pending_count = 0
  handle_buildings_tracking_transition()
  reschedule(false)
  if export_enabled() then
    export_recipes()
    maybe_start_translation_pass()
  end
end)

script.on_event(defines.events.on_runtime_mod_setting_changed, function(event)
  if event.setting == "flma-export-enabled"
      or event.setting == "flma-tick-interval"
      or event.setting == "flma-export-buildings"
      or event.setting == "flma-export-inventories" then
    handle_buildings_tracking_transition()
    reschedule(false)
    if export_enabled() then
      write_current_pointer() -- discoverable immediately, not after the next tick-interval
      export_all_forces_tech() -- prime tech.json immediately rather than waiting for the next research event
      export_research_snapshot()
      export_recipes()
      maybe_start_translation_pass()
    end
  end
end)

--------------------------------------------------------------------------------
-- remote interface — for debugging from the console. Mod-local `storage` is
-- not readable from a /c command (those run in the scenario's own separate
-- storage scope), so this is the only way to inspect or reset flma's state
-- without editing settings or starting a fresh save.
--   /c remote.call("flma", "status")
--   /c remote.call("flma", "reset_buildings")
--------------------------------------------------------------------------------

remote.add_interface("flma", {
  status = function()
    local initialized = storage.flma and storage.flma.buildings_initialized or false
    local count = 0
    if storage.flma and storage.flma.buildings then
      for _ in pairs(storage.flma.buildings) do
        count = count + 1
      end
    end
    local msg = string.format(
      "flma: save_id=%s output_dir=%s export_enabled=%s buildings_enabled=%s buildings_initialized=%s tracked_buildings=%d lines_since_compact=%d recipes_dirty=%s translations_done=%s translations_pending=%d",
      tostring(storage.flma and storage.flma.save_id or "(not yet assigned)"),
      output_dir(),
      tostring(export_enabled()),
      tostring(buildings_enabled()),
      tostring(initialized),
      count,
      storage.flma and storage.flma.building_lines_since_compact or 0,
      tostring(storage.flma and storage.flma.recipes_dirty or false),
      tostring(storage.flma and storage.flma.recipe_translations_done or false),
      (storage.flma and storage.flma.recipe_translation_pending_count or 0)
        + (storage.flma and storage.flma.recipe_translation_queue
          and (#storage.flma.recipe_translation_queue - storage.flma.recipe_translation_queue_index + 1)
          or 0)
    )
    game.print(msg)
    -- game.print is invisible to an RCON caller (it goes to chat/stdout, not
    -- the RCON response); echo there too so `rcon "remote.call('flma','status')"`
    -- actually returns the answer.
    rcon.print(msg)
  end,
  reset_buildings = function()
    ensure_storage()
    storage.flma.buildings = {}
    storage.flma.building_count = 0
    storage.flma.buildings_initialized = false
    storage.flma.building_lines_since_compact = 0
    storage.flma.building_total_lines = 0
    storage.flma.baseline_in_progress = false
    storage.flma.baseline_chunks = nil
    storage.flma.baseline_entities = nil
    helpers.write_file(output_dir() .. "/buildings.ndjson", "", false) -- truncate
    game.print("flma: building index reset; re-scanning under current rules")
    reschedule(false)
  end,
  -- Forces one export cycle immediately, without waiting for the next
  -- on_nth_tick — useful when the server has no connected players (ticks
  -- don't advance, so the schedule never fires) or when debugging.
  export_now = function()
    export_production_stats()
    export_logistics()
    export_research_snapshot()
    if inventories_enabled() then
      export_inventories()
    end
    export_all_forces_tech()
    game.print("flma: forced an export cycle")
  end,
  -- Forces a recipes.json rewrite immediately (and kicks off a translation
  -- pass if a player is connected and one hasn't completed yet). Works even
  -- with flma-export-enabled off — an explicit call is its own authorization.
  export_recipes = function()
    ensure_storage()
    export_recipes()
    maybe_start_translation_pass()
    local msg = "flma: recipes.json exported"
    game.print(msg)
    rcon.print(msg)
  end,
  -- Writes runtime-global settings directly. Factorio rejects
  -- `settings.global[...] = ...` from an RCON /silent-command (no player,
  -- and RCON isn't "the mod that made the setting") — this remote call runs
  -- the write from inside flma's own script instead, which is exempt, so
  -- dev/RCON workflows can toggle the mod on without a connected client
  -- driving the Mod Settings GUI. Any argument left nil keeps that setting
  -- unchanged.
  configure = function(export_enabled_value, buildings_enabled_value, inventories_enabled_value)
    if export_enabled_value ~= nil then
      settings.global["flma-export-enabled"] = {value = export_enabled_value}
    end
    if buildings_enabled_value ~= nil then
      settings.global["flma-export-buildings"] = {value = buildings_enabled_value}
    end
    if inventories_enabled_value ~= nil then
      settings.global["flma-export-inventories"] = {value = inventories_enabled_value}
    end
    local msg = "flma: configured via remote call"
    game.print(msg)
    rcon.print(msg)
  end,
})
