"""Build an independent client mods/ profile that mirrors the master (server)
mod set, without sharing mod-list.json / mod-settings.dat as the same file.

Why this exists: the client and server must load byte-identical mods (Factorio
checks this on connect), but they must NOT share mod-list.json itself -- when
a client joins and Factorio's automatic "sync mods with server" flow runs, it
rewrites mod-list.json in place. A mod that isn't on the mods portal (like
flma) can't be resolved by that flow and gets silently disabled -- if that
file is shared (e.g. via a directory symlink), it corrupts the server's copy
too. Keeping independent mod-list.json/mod-settings.dat per profile, with only
the actual mod content (zips/dirs) shared via symlink, avoids this entirely.

Usage: python3 sync_mods.py <master_profile> <client_profile> <repo_mod_dir> [source_list]

`source_list` (optional) is a mod-list.json to derive the client's set from
INSTEAD of the master profile's live mod-list.json. start-server.sh snapshots
the list at server-launch time to dev/.server-mod-list.json and
start-client.sh passes that — the live master file is a moving target (a
gracefully-exiting server rewrites it with whatever *it* had loaded, and
GUI mod-manager edits land there too), so deriving the client from it can
mismatch a server that launched earlier. The snapshot is, by construction,
exactly what the running server read.
"""

import json
import re
import sys
from pathlib import Path

VERSION_RE = re.compile(r"^(?P<name>.+)_(?P<version>\d+\.\d+\.\d+)(?P<ext>\.zip)?$")


def version_key(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def find_best_mod_file(master_mods: Path, name: str, version: str | None = None) -> Path | None:
    """Newest matching file for `name` -- or the exact `version` when given
    (the server honors a mod-list.json version pin, so when the source list
    carries one, "newest" could hand the client a different version than the
    server actually loaded). Falls back to newest if the pinned file is gone."""
    candidates = []
    for entry in master_mods.iterdir():
        if entry.name in ("mod-list.json", "mod-settings.dat"):
            continue
        m = VERSION_RE.match(entry.name)
        if m and m.group("name") == name:
            if version is not None and m.group("version") == version:
                return entry
            candidates.append((version_key(m.group("version")), entry))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[-1][1]


def main() -> None:
    master_profile, client_profile, repo_mod_dir = (Path(p) for p in sys.argv[1:4])
    source_list_path = Path(sys.argv[4]) if len(sys.argv) > 4 else None
    master_mods = master_profile / "mods"
    client_mods = client_profile / "mods"

    info = json.loads((repo_mod_dir / "info.json").read_text())
    flma_name = info["name"]
    flma_version = info["version"]
    flma_link_name = f"{flma_name}_{flma_version}"

    # Make sure the master (server) profile points flma at the live repo
    # checkout and has it enabled -- self-heals the "disabled by a failed
    # mod-sync" corruption described above.
    for stale in master_mods.glob(f"{flma_name}_*"):
        if stale.name != flma_link_name:
            stale.unlink()
    master_link = master_mods / flma_link_name
    if not master_link.is_symlink() or master_link.resolve() != repo_mod_dir.resolve():
        master_link.unlink(missing_ok=True)
        master_link.symlink_to(repo_mod_dir)

    master_list_path = master_mods / "mod-list.json"
    master_list = json.loads(master_list_path.read_text())
    patched = False
    found = False
    for entry in master_list["mods"]:
        if entry["name"] == flma_name:
            found = True
            if not entry.get("enabled") or entry.get("version") != flma_version:
                entry["enabled"] = True
                entry["version"] = flma_version
                patched = True
    if not found:
        master_list["mods"].append({"name": flma_name, "enabled": True, "version": flma_version})
        patched = True
    if patched:
        master_list_path.write_text(json.dumps(master_list, indent=2))
        print(f"master: re-enabled {flma_name} {flma_version}")

    # The client's set derives from the server-launch snapshot when given
    # (see module docstring); the live master list is only the fallback.
    if source_list_path is not None and source_list_path.exists():
        source_list = json.loads(source_list_path.read_text())
        print(f"client: deriving mod set from snapshot {source_list_path}")
    else:
        source_list = master_list

    for entry in source_list["mods"]:
        if entry["name"] == flma_name and entry.get("version") not in (None, flma_version):
            print(
                f"WARNING: server launched with {flma_name} {entry.get('version')} but the "
                f"repo is now {flma_version} -- restart the server (dev/start-server.sh) "
                f"or the client will be refused with ModsMismatch."
            )

    # Rebuild the client's mods/ from scratch -- cheap, and guarantees no
    # drift from a previous run's stale symlinks/mod-list.json.
    if client_mods.exists():
        for entry in client_mods.iterdir():
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
    client_mods.mkdir(parents=True, exist_ok=True)

    # Mirror the FULL source list -- disabled entries included. A mod that is
    # merely omitted from mod-list.json gets auto-enabled by Factorio when it
    # discovers it installed, and the built-in DLCs (quality, space-age, ...)
    # are always "installed" (they ship with the game, no zip in mods/). So a
    # client list that only carries the enabled set resurrects every disabled
    # DLC on launch and earns a ModsMismatch refusal + manual in-game sync on
    # every connect. Disabled means written down as disabled.
    client_list_mods = []
    for entry in source_list["mods"]:
        name = entry["name"]
        if name == flma_name:
            (client_mods / flma_link_name).symlink_to(repo_mod_dir)
            client_list_mods.append({"name": name, "enabled": True, "version": flma_version})
            continue
        if not entry.get("enabled"):
            client_list_mods.append({"name": name, "enabled": False})
            continue
        src = find_best_mod_file(master_mods, name, entry.get("version"))
        if src is None:
            # Built-in / DLC (base, elevated-rails, quality, space-age) --
            # no file in mods/, just needs the mod-list.json entry.
            client_list_mods.append({"name": name, "enabled": True})
            continue
        (client_mods / src.name).symlink_to(src)
        m = VERSION_RE.match(src.name)
        client_list_mods.append({"name": name, "enabled": True, "version": m.group("version")})

    # Belt and suspenders for the DLCs: if the source list somehow has no
    # entry for one at all, pin it explicitly disabled rather than letting
    # the client's auto-discovery decide.
    listed = {e["name"] for e in client_list_mods}
    for dlc in ("elevated-rails", "quality", "space-age"):
        if dlc not in listed:
            client_list_mods.append({"name": dlc, "enabled": False})

    (client_mods / "mod-list.json").write_text(json.dumps({"mods": client_list_mods}, indent=2))
    # Startup settings must match the server as well; prefer the launch-time
    # snapshot (written by start-server.sh next to the mod-list snapshot) over
    # the live file for the same reason as the list itself.
    settings_src = master_mods / "mod-settings.dat"
    if source_list_path is not None:
        snapshot_settings = source_list_path.parent / ".server-mod-settings.dat"
        if snapshot_settings.exists():
            settings_src = snapshot_settings
    (client_mods / "mod-settings.dat").write_bytes(settings_src.read_bytes())
    print(f"client: synced {len(client_list_mods)} mods to {client_mods}")


if __name__ == "__main__":
    main()
