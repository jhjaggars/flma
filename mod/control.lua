-- flma (Factorio Live MCP Agent) — control.lua
--
-- Exports live game state to script-output/flma/ for a local process (the
-- factorio-live-mcp bridge, see apps/factorio-live-mcp/src/) to read.
--
-- EFFICIENCY IS THE DESIGN CONSTRAINT, NOT AN AFTERTHOUGHT:
-- This mod is synced (it has a control stage, so its checksum must match every
-- peer's). Any per-tick work it does runs on the server AND every client, every
-- tick, for everyone in the game. Rules followed throughout this file:
--   1. Never hook on_tick. Only script.on_nth_tick(N) with a large configurable N.
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

local OUTPUT_DIR = "flma"

-- Entity types we don't consider a "placed building" for query_buildings —
-- resources, trees, fish, etc. are not something a player placed.
local BUILDING_TYPE_BLOCKLIST = {
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
}

-- Number of entities to scan per tick while building the initial baseline —
-- keeps the one unavoidable full scan from spiking a single frame even on a
-- huge base. Tuned conservatively; adjust after profiling with show-time-usage.
local BASELINE_CHUNK_SIZE = 500

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
  helpers.write_file(OUTPUT_DIR .. "/" .. name .. ".json", helpers.table_to_json(data), false)
end

local function append_line(name, data)
  helpers.write_file(OUTPUT_DIR .. "/" .. name .. ".ndjson", helpers.table_to_json(data) .. "\n", true)
end

--------------------------------------------------------------------------------
-- tech tree export — event-driven, not scheduled. Full state is small
-- (bounded by technology count, not base size) so a full overwrite per
-- research event is simpler than diffing and still cheap.
--------------------------------------------------------------------------------

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
    research_queue = force.research_queue and (function()
      local q = {}
      for _, t in pairs(force.research_queue) do
        q[#q + 1] = t.name
      end
      return q
    end)() or nil,
    technologies = techs,
  }
end

local function export_all_forces_tech()
  local forces_out = {}
  for force_name, force in pairs(game.forces) do
    forces_out[force_name] = build_tech_table(force)
  end
  write_snapshot("tech", { tick = game.tick, forces = forces_out })
end

--------------------------------------------------------------------------------
-- production statistics — engine-aggregated (O(#item/fluid types)), scheduled.
--------------------------------------------------------------------------------

local function flow_to_table(flow_stats)
  if not flow_stats then
    return nil
  end
  return {
    input_counts = flow_stats.input_counts,
    output_counts = flow_stats.output_counts,
  }
end

local function export_production_stats()
  local forces_out = {}
  for force_name, force in pairs(game.forces) do
    local surfaces_out = {}
    for _, surface in pairs(game.surfaces) do
      local ok_items, item_stats = pcall(force.get_item_production_statistics, force, surface)
      local ok_fluids, fluid_stats = pcall(force.get_fluid_production_statistics, force, surface)
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

local function is_building(entity)
  return entity and entity.valid and not BUILDING_TYPE_BLOCKLIST[entity.type]
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

local function record_build(entity)
  if not is_building(entity) or not entity.unit_number then
    return
  end
  local rec = building_record(entity)
  storage.flma.buildings[rec.id] = rec
  storage.flma.building_lines_since_compact = storage.flma.building_lines_since_compact + 1
  append_line("buildings", { t = game.tick, op = "add", entity = rec })
end

local function record_remove(entity)
  if not entity or not entity.unit_number then
    return
  end
  local id = entity.unit_number
  if storage.flma.buildings[id] then
    storage.flma.buildings[id] = nil
    storage.flma.building_lines_since_compact = storage.flma.building_lines_since_compact + 1
    append_line("buildings", { t = game.tick, op = "remove", id = id })
  end
end

-- Rewrite buildings.ndjson from the in-memory index (which is already
-- authoritative and cheap to hold — one small table entry per building).
-- This is O(#buildings) but runs rarely (only once every
-- flma-buildings-compact-threshold appended lines), not on a fixed schedule,
-- so cost scales with churn, not with wall-clock time.
local function compact_buildings()
  helpers.write_file(OUTPUT_DIR .. "/buildings.ndjson", "", false) -- truncate
  for _, rec in pairs(storage.flma.buildings) do
    append_line("buildings", { t = game.tick, op = "add", entity = rec })
  end
  storage.flma.building_lines_since_compact = 0
end

local function maybe_compact_buildings()
  if storage.flma.building_lines_since_compact >= compact_threshold() then
    compact_buildings()
  end
end

-- Time-sliced baseline scan: collect all matching entities up front (cheap,
-- just references) and drain BASELINE_CHUNK_SIZE of them per tick via a
-- temporary on_tick handler, so a huge base doesn't spike one frame. Uses
-- on_tick only for the duration of this one-time scan, then unregisters
-- itself — not a standing per-tick cost.
local function start_baseline_scan()
  local queue = {}
  for _, surface in pairs(game.surfaces) do
    for _, entity in pairs(surface.find_entities_filtered({})) do
      if is_building(entity) and entity.unit_number then
        queue[#queue + 1] = entity
      end
    end
  end
  storage.flma.baseline_queue = queue
  storage.flma.baseline_index = 1

  script.on_event(defines.events.on_tick, function()
    local q = storage.flma.baseline_queue
    local i = storage.flma.baseline_index
    local last = math.min(i + BASELINE_CHUNK_SIZE - 1, #q)
    for j = i, last do
      record_build(q[j])
    end
    storage.flma.baseline_index = last + 1
    if storage.flma.baseline_index > #q then
      script.on_event(defines.events.on_tick, nil) -- unregister: baseline done
      storage.flma.baseline_queue = nil
      storage.flma.buildings_initialized = true
    end
  end)
end

--------------------------------------------------------------------------------
-- scheduler — wires up (or tears down) every handler based on current
-- settings. Called from on_init, on_load, and on_runtime_mod_setting_changed
-- so registration always matches the synced settings deterministically on
-- every peer.
--------------------------------------------------------------------------------

local function ensure_storage()
  storage.flma = storage.flma or {}
  storage.flma.buildings = storage.flma.buildings or {}
  storage.flma.buildings_initialized = storage.flma.buildings_initialized or false
  storage.flma.building_lines_since_compact = storage.flma.building_lines_since_compact or 0
end

local BUILD_EVENTS = {
  defines.events.on_built_entity,
  defines.events.on_robot_built_entity,
  defines.events.script_raised_built,
  defines.events.script_raised_revive,
}

local DESTROY_EVENTS = {
  defines.events.on_player_mined_entity,
  defines.events.on_robot_mined_entity,
  defines.events.on_entity_died,
  defines.events.script_raised_destroy,
}

local function on_build_event(event)
  record_build(event.entity)
end

local function on_destroy_event(event)
  record_remove(event.entity)
  maybe_compact_buildings()
end

local function reschedule()
  -- Always start clean: unregister everything, then re-register only what's
  -- enabled. Keeps this idempotent and safe to call from on_load.
  script.on_nth_tick(nil)
  script.on_event(defines.events.on_research_finished, nil)
  script.on_event(defines.events.on_research_reversed, nil)
  script.on_event(BUILD_EVENTS, nil)
  script.on_event(DESTROY_EVENTS, nil)

  if not export_enabled() then
    return -- fully idle: zero registered handlers, zero per-tick cost
  end

  local n = tick_interval()
  script.on_nth_tick(n, function()
    export_production_stats()
    export_logistics()
    if inventories_enabled() then
      export_inventories()
    end
  end)

  script.on_event(defines.events.on_research_finished, function()
    export_all_forces_tech()
  end)
  script.on_event(defines.events.on_research_reversed, function()
    export_all_forces_tech()
  end)

  if buildings_enabled() then
    script.on_event(BUILD_EVENTS, on_build_event)
    script.on_event(DESTROY_EVENTS, on_destroy_event)
    if not storage.flma.buildings_initialized then
      start_baseline_scan()
    end
  end
end

--------------------------------------------------------------------------------
-- lifecycle
--------------------------------------------------------------------------------

script.on_init(function()
  ensure_storage()
  reschedule()
  if export_enabled() then
    export_all_forces_tech()
  end
end)

script.on_load(function()
  -- storage already restored by the engine here; just re-attach handlers.
  reschedule()
end)

script.on_configuration_changed(function()
  ensure_storage()
  reschedule()
end)

script.on_event(defines.events.on_runtime_mod_setting_changed, function(event)
  if event.setting == "flma-export-enabled"
      or event.setting == "flma-tick-interval"
      or event.setting == "flma-export-buildings"
      or event.setting == "flma-export-inventories" then
    reschedule()
    if export_enabled() then
      export_all_forces_tech() -- prime tech.json immediately rather than waiting for the next research event
    end
  end
end)
