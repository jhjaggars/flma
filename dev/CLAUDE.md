# dev/ — local Factorio dev environment (mod development)

Scripts to run an isolated headless server + game client on this machine and
poke the live game over RCON, for iterating on `../mod/`. The authoritative
guide is `.claude/skills/factorio-dev/SKILL.md` — read it before touching
anything here; it documents hard-won gotchas (why the client uses a separate
profile, why `mods/` must never be wholesale-symlinked between profiles, why
the dev save is a dev-owned copy that gets mutated, why RCON commands need the
`/silent-command` prefix).

Quick reference: `start-server.sh` / `start-client.sh` / `stop.sh` /
`status.sh`; `summary.py` is a stdlib-only smoke test that prints everything
the mod is currently exporting.
