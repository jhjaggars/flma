-- Runtime-global settings (synced across all peers so enable/disable and cadence
-- stay deterministic — see apps/factorio-live-mcp/CLAUDE.md for the efficiency
-- rationale). Change these from the in-game "Mod settings" -> "Map" tab, or via
-- /c settings.global['flma-export-enabled'] = {value=true}.

data:extend({
  {
    type = "bool-setting",
    name = "flma-export-enabled",
    setting_type = "runtime-global",
    default_value = false,
    order = "a",
  },
  {
    type = "int-setting",
    name = "flma-tick-interval",
    setting_type = "runtime-global",
    default_value = 300, -- ~5s at 60 UPS
    minimum_value = 60,
    maximum_value = 216000,
    order = "b",
  },
  {
    type = "bool-setting",
    name = "flma-export-inventories",
    setting_type = "runtime-global",
    default_value = false, -- off by default: player names/contents are more sensitive
    order = "c",
  },
  {
    type = "bool-setting",
    name = "flma-export-buildings",
    setting_type = "runtime-global",
    default_value = false, -- off by default: the one-time baseline scan is the
                            -- single genuinely expensive operation this mod performs
    order = "d",
  },
  {
    type = "int-setting",
    name = "flma-buildings-compact-threshold",
    setting_type = "runtime-global",
    default_value = 20000, -- lines appended to buildings.ndjson before compaction
    minimum_value = 1000,
    maximum_value = 1000000,
    order = "e",
  },
  {
    type = "string-setting",
    name = "flma-contents-tracked-names",
    setting_type = "runtime-global",
    default_value = "", -- empty = disabled; a comma-separated list of exact
                         -- entity prototype names (e.g. "prandium-lab-mk01,rc-mk01")
                         -- is itself the enable signal, scoping
                         -- building-contents.json's per-tick-interval cost to
                         -- O(#matched machines) instead of O(#all buildings)
    allow_blank = true, -- string-setting rejects a blank default_value unless
                         -- this is set (confirmed live: the mod fails to load
                         -- entirely otherwise) -- blank is the deliberate
                         -- "disabled" state here, not an oversight
    order = "f",
  },
})
