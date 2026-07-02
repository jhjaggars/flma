"""One-shot summary of everything the flma mod is currently exporting.

Reads the server's script-output/flma/ files directly -- no RCON, no MCP
bridge, no third-party deps -- so it's cheap enough to run after every mod
change as a smoke test ("is data flowing, and does it look sane?").

Usage: python3 dev/summary.py [script-output-flma-dir]
       (default: $FLMA_OUTPUT_DIR, else ~/.factorio/script-output/flma)
"""

import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

TOP_N = 10


def age_str(path: Path) -> str:
    if not path.exists():
        return "MISSING"
    age = time.time() - path.stat().st_mtime
    if age < 120:
        return f"{age:.0f}s ago"
    if age < 7200:
        return f"{age / 60:.0f}m ago"
    return f"{age / 3600:.1f}h ago"


def load(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"  !! {path.name}: unreadable ({e})")
        return None


def tick_str(tick: int) -> str:
    secs = tick // 60
    return f"{tick:,} ({secs // 3600}h {secs % 3600 // 60:02d}m in-game)"


def fmt_top(counts: dict[str, float], n: int = TOP_N, unit: str = "") -> list[str]:
    rows = sorted(counts.items(), key=lambda kv: -kv[1])[:n]
    width = max((len(name) for name, _ in rows), default=0)
    return [f"    {name:<{width}}  {count:>12,.0f}{unit}" for name, count in rows]


def section(title: str) -> None:
    print(f"\n== {title}")


def resolve_active_dir(base_dir: Path) -> Path:
    """Since mod 0.3.1, data files live under base_dir/<save_id>/, not
    directly in base_dir -- follow the current-save.json pointer the mod
    maintains there. Falls back to base_dir itself if there's no pointer yet
    (mod not enabled, or an older mod version)."""
    pointer = base_dir / "current-save.json"
    try:
        save_id = json.loads(pointer.read_text()).get("save_id")
    except (OSError, json.JSONDecodeError):
        return base_dir
    return base_dir / save_id if isinstance(save_id, str) and save_id else base_dir


def main() -> None:
    default = Path.home() / ".factorio/script-output/flma"
    base_dir = Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("FLMA_OUTPUT_DIR", default))
    if not base_dir.is_dir():
        sys.exit(f"error: no such directory: {base_dir}\n(is flma-export-enabled on?)")
    out_dir = resolve_active_dir(base_dir)
    if not out_dir.is_dir():
        sys.exit(f"error: current-save.json points at {out_dir}, which doesn't exist")

    files = ["research.json", "production.json", "logistics.json",
             "inventories.json", "tech.json", "recipes.json", "buildings.ndjson"]
    section(f"feeds ({out_dir})")
    for name in files:
        print(f"    {name:<18} {age_str(out_dir / name)}")

    research = load(out_dir / "research.json")
    production = load(out_dir / "production.json")
    logistics = load(out_dir / "logistics.json")
    inventories = load(out_dir / "inventories.json")
    tech = load(out_dir / "tech.json")

    tick = max((d.get("tick", 0) for d in (research, production, logistics) if d), default=0)
    if tick:
        print(f"    latest snapshot tick: {tick_str(tick)}")

    if research:
        section("research")
        for force, r in research.get("forces", {}).items():
            cur = r.get("current_research")
            if not cur and not r.get("research_queue"):
                continue
            researched = total = 0
            if tech:
                techs = tech.get("forces", {}).get(force, {}).get("technologies", {})
                total = len(techs)
                researched = sum(1 for t in techs.values() if t.get("researched"))
            progress = f" @ {r.get('research_progress', 0) * 100:.1f}%" if cur else ""
            queue = r.get("research_queue") or []
            queue = queue if isinstance(queue, list) else []
            print(f"    [{force}] {cur or 'idle'}{progress}"
                  + (f"  (researched {researched}/{total})" if total else ""))
            for q in queue[1:6]:
                print(f"      queued: {q}")

    # SCHEMA.md tells polling consumers to stat() recipes.json rather than
    # parse it every cycle -- fine to ignore here since this is a one-shot
    # smoke test, not a polling loop, and the full ~11MB parse is ~0.1s.
    recipes = load(out_dir / "recipes.json")
    if recipes:
        section(f"recipes (static catalog, game {recipes.get('game_version', '?')})")
        for cat in ("recipes", "items", "fluids", "entities", "technologies"):
            vals = recipes.get(cat, {})
            if not isinstance(vals, dict) or not vals:
                continue
            total = len(vals)
            translated = sum(1 for v in vals.values() if isinstance(v, dict) and v.get("translated_name"))
            print(f"    {cat:<13} {total:>6,} entries  ({translated:,} translated)")

    if production:
        section(f"production (last-minute rates, top {TOP_N})")
        for force, f in production.get("forces", {}).items():
            for surface, s in f.get("surfaces", {}).items():
                for kind in ("items", "fluids"):
                    k = s.get(kind, {})
                    produced = {n: r for n, r in k.get("input_rates_per_min", {}).items() if r > 0}
                    consumed = {n: r for n, r in k.get("output_rates_per_min", {}).items() if r > 0}
                    if not produced and not consumed:
                        continue
                    print(f"    [{force} / {surface} / {kind}]")
                    if produced:
                        print("     producing:")
                        print("\n".join(fmt_top(produced, unit="/min")))
                    if consumed:
                        print("     consuming:")
                        print("\n".join(fmt_top(consumed, unit="/min")))
        # Lifetime totals are always present even when the base is idle.
        idle = all(
            r <= 0
            for f in production.get("forces", {}).values()
            for s in f.get("surfaces", {}).values()
            for kind in ("items", "fluids")
            for r in s.get(kind, {}).get("input_rates_per_min", {}).values()
        )
        if idle:
            print("    (no production in the last minute -- lifetime totals:)")
            for force, f in production.get("forces", {}).items():
                for surface, s in f.get("surfaces", {}).items():
                    counts = s.get("items", {}).get("input_counts", {})
                    if counts:
                        print(f"    [{force} / {surface}]")
                        print("\n".join(fmt_top(counts)))

    if logistics:
        section("logistics networks")
        for force, nets in logistics.get("forces", {}).items():
            if not isinstance(nets, list) or not nets:
                continue
            for net in nets:
                contents = net.get("contents", [])
                total_items = sum(c.get("count", 0) for c in contents)
                print(f"    [{force}] network {net.get('network_id')} on {net.get('surface')}: "
                      f"{len(contents)} item types, {total_items:,} items, "
                      f"logibots {net.get('available_logistic_robots', '?')}/{net.get('all_logistic_robots', '?')}, "
                      f"conbots {net.get('available_construction_robots', '?')}/{net.get('all_construction_robots', '?')}")
                top = Counter({c["name"]: c["count"] for c in contents if "name" in c})
                print("\n".join(fmt_top(dict(top.most_common(5)), n=5)))

    if inventories:
        section("player inventories")
        players = inventories.get("players", {})
        if not players:
            print("    (none connected)")
        for name, p in players.items():
            contents = p.get("contents", [])
            total = sum(c.get("count", 0) for c in contents)
            print(f"    [{name or '<unnamed>'}] on {p.get('surface')}: "
                  f"{len(contents)} item types, {total:,} items")
            print("\n".join(fmt_top({c['name']: c['count'] for c in contents}, n=5)))

    ndjson = out_dir / "buildings.ndjson"
    if ndjson.exists():
        section("buildings (replayed from ndjson)")
        entities: dict[int, dict] = {}
        lines = adds = removes = bad = 0
        with ndjson.open() as fh:
            for line in fh:
                lines += 1
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    bad += 1
                    continue
                op = rec.get("op")
                if op == "add":
                    ent = rec.get("entity", {})
                    eid = ent.get("id")
                    if eid is not None:
                        entities[eid] = ent
                        adds += 1
                elif op == "remove":
                    # Remove records carry a flat "id", not "entity.id" --
                    # SCHEMA.md `buildings.ndjson`.
                    eid = rec.get("id")
                    if eid is not None:
                        entities.pop(eid, None)
                        removes += 1
        print(f"    {lines:,} log lines ({adds:,} add / {removes:,} remove"
              + (f" / {bad} unparseable" if bad else "") + f") -> {len(entities):,} live entities")
        by_force = Counter(e.get("force", "?") for e in entities.values())
        print("    by force: " + ", ".join(f"{f}={c:,}" for f, c in by_force.most_common()))
        by_type = Counter(e.get("type", "?") for e in entities.values())
        print("    top types:")
        print("\n".join(fmt_top(dict(by_type.most_common(TOP_N)))))
    print()


if __name__ == "__main__":
    main()
