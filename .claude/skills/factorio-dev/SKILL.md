---
name: factorio-dev
description: Start/stop an isolated local Factorio headless server + client for developing the flma mod, and query the running game over RCON
---

# flma local dev environment

## Purpose

Lets Claude iterate on the flma mod against a real, running Factorio game:
start a headless dev server and a separate game client on the same machine,
join them together, and read/poke live game state over RCON instead of
relying on the user relaying console output via screenshots.

## When to use this skill

- The user wants to test a mod change against a live game.
- The user asks to start/stop/restart the dev server or client.
- You need to inspect live game state (production stats, storage, entity
  counts, force data) to debug the mod -- use RCON instead of asking for
  screenshots.
- The user reports the client can't launch, or "is it already running?" --
  check `dev/status.sh` first.

## Layout

All scripts live in `dev/` at the repo root and share config from `dev/env.sh`
(source it, don't execute it, if you need the variables directly in a Bash
call: `source dev/env.sh && rcon "..."`).

- `dev/start-server.sh` -- (re)starts the headless server on the configured
  save, with RCON bound to `127.0.0.1:$RCON_PORT` and a generated password
  (`dev/.rcon-password`, gitignored). Runs `sync_mods.py` first.
- `dev/start-client.sh` -- (re)starts a game client using an **isolated
  profile** (`$CLIENT_PROFILE`, default `~/.factorio-client`), so it doesn't
  contend with the server for the default `~/.factorio/.lock`. Also runs
  `sync_mods.py`.
- `dev/stop.sh [server|client|both]` -- stops by PID file.
- `dev/status.sh` -- reports whether server/client are running and does a
  live RCON health check (`game.tick`).
- `dev/sync_mods.py` -- see "Why two profiles" below. Idempotent; both start
  scripts call it, so you rarely need to run it directly.
- `dev/rcon.py` -- minimal Source RCON client (stdlib only).
- `dev/summary.py` -- one-shot summary of everything the mod is exporting
  (feed staleness, research, production rates, logistics, inventories, live
  building counts replayed from the ndjson). Stdlib-only, reads the files
  directly, runs in ~0.2s: `python3 dev/summary.py` (arg or
  `FLMA_OUTPUT_DIR` to point elsewhere). Use as a smoke test after mod
  changes instead of hand-inspecting each JSON file.
- `dev/server-settings.json` -- checked in, not secret. Notably sets
  `require_user_verification: false` and `auto_pause: false`.

## Why two profiles (server vs. client)

Factorio only allows one instance per write-data directory (enforced via
`<profile>/.lock`). A headless server and a game client on the same machine
need **separate** write-data directories, set via each instance's own
`config.ini` `[path] write-data=`.

The mods themselves must still match byte-for-byte between server and
client (Factorio checks this on connect) -- but do **not** just symlink the
whole `mods/` directory between the two profiles to achieve that. That also
shares `mod-list.json`/`mod-settings.dat`, and Factorio's automatic "sync
mods with server" flow (which runs when a client connects) rewrites
`mod-list.json` in place. `flma` isn't on the mods portal, so that flow can't
resolve/verify it and will silently disable it -- corrupting the **shared**
file, which breaks the server too if it's ever restarted.

`sync_mods.py` instead: symlinks only the actual mod content (zips / the
`flma` dir, which it points straight at this repo's `mod/` folder) into the
client's own `mods/`, and writes each profile its own independent
`mod-list.json` derived from the server's enabled set. Content is shared;
mutable state is not.

**The mod set is derived from the SAVE being served, end to end:**

1. `start-server.sh` runs `factorio --sync-mods "$SAVE"` first -- the save
   records exactly which mods+versions it was created with, and this
   rewrites the master `mod-list.json` to match. Loading a save under any
   other set silently migrates/mutates the world (a Space Age save got
   pyanodons-contaminated here once).
2. `sync_mods.py` then re-enables `flma` on top (the mod under development
   usually isn't in the save yet) and repairs the repo symlink.
3. The resulting list + startup settings are snapshotted
   (`dev/.server-mod-list.json` + `.server-mod-settings.dat`) just before
   launch, and `start-client.sh` derives the client's mod set from that
   snapshot -- NOT from the live master list, which is a moving target
   (a gracefully-exiting server rewrites it with whatever it had loaded,
   and in-game mod-manager edits land there too). The client list mirrors
   disabled entries explicitly: a mod merely *omitted* from mod-list.json
   gets auto-enabled by Factorio on launch (the built-in DLCs especially,
   which are always installed), which caused ModsMismatch on every connect
   until written down as disabled.

Net effect: pick a save, start server, start client -- everything matches
with no manual in-game "sync mods with server" round-trip. To serve a
different game, just point at a different save; the mod set follows.
`start-client.sh` warns if the repo's flma version differs from the
snapshot's (you bumped `info.json` after the server started) -- that always
needs a server restart, see the version-bump section.

**The dev save is a dev-owned copy, not a real save.** The server exit-saves
back into whatever file it loaded, so serving a real save from
`~/.factorio/saves/` mutates it (a real `_autosave` was overwritten this way
once). `env.sh` defaults `SAVE` to `dev/saves/flma-dev.zip`; seed it with
`cp ~/.factorio/saves/<save>.zip dev/saves/flma-dev.zip` (or set
`FLMA_DEV_SAVE`). Note the running server's periodic autosaves still go to
`~/.factorio/saves/_autosaveN.zip` regardless of which save was loaded.

## Common tasks

**Fresh start (server, then client):**
```
bash dev/start-server.sh
bash dev/start-client.sh
```
The client auto-reconnects to its last "connect to address" attempt on
launch; if it's a brand new profile, tell the user to use Multiplayer ->
Connect to address -> `localhost:$GAME_PORT` (default 34197) once, after
which it remembers.

**Check what's running before assuming a lock conflict:**
```
bash dev/status.sh
```
If the user says "can't launch Factorio" / "is it already running", this is
almost always because the headless server holds `~/.factorio/.lock` and they
tried to launch via Steam's normal button (which always uses the default
profile). The fix is to launch via `dev/start-client.sh`, not Steam directly.

**Querying live state over RCON:**
```
source dev/env.sh
rcon "rcon.print(game.tick)"
rcon "remote.call('flma', 'status')"
rcon "remote.call('flma', 'export_now')"
```
`rcon()` is a shell function defined in `env.sh` -- it auto-prefixes with
`/silent-command`. **This prefix is required**: a bare expression like
`rcon.print(x)` sent raw over RCON is logged as a chat message and returns
nothing, silently. If an RCON call returns empty, check you went through the
`rcon` helper (or added `/silent-command` yourself), not a raw command.

`storage`/mod-local state is not readable from `/c` or `/silent-command` at
all (console runs in the scenario's own separate storage scope) -- that's
why flma exposes `remote.call('flma', 'status'|'reset_buildings'|'export_now')`
as the introspection/control surface. Add to that interface in `control.lua`
rather than trying to reach into `storage` from RCON.

## Version bumps require a full restart of both processes

Factorio only rescans `mods/` for renamed folders / new versions at full
process startup -- reloading a save in an already-running process does
**not** pick up a mod folder rename (e.g. `flma_0.2.1` -> `flma_0.2.2`).
After bumping `mod/info.json`'s version:
1. Run `dev/stop.sh both`.
2. Run `dev/start-server.sh` then `dev/start-client.sh` again (this
   re-runs `sync_mods.py`, which recreates the `flma_<version>` symlink
   under the new name in both profiles).

A same-process save reload is only sufficient for in-place `control.lua`
edits when the mod's folder name hasn't changed.

## Gotchas already found and fixed here (don't reintroduce)

- Never symlink the whole `mods/` directory between profiles (see above).
- `require_user_verification: true` (Factorio's default) causes
  `UserVerificationMissing` connection refusals on self-hosted/local servers
  -- keep it `false` in `dev/server-settings.json` for local dev.
- `auto_pause: true` (Factorio's default) freezes ticks -- including
  `on_nth_tick` exports -- while zero players are connected. Keep it `false`
  here so the mod's scheduled exports actually run during headless-only
  testing; `remote.call('flma', 'export_now')` remains available as a manual
  trigger regardless.
- The client needs `steam_appid.txt` (containing `427520`) next to the
  Factorio binary to authenticate against an already-running Steam client
  when launched directly (outside Steam's own launcher). `start-client.sh`
  writes this each time; it's harmless to leave in place.
