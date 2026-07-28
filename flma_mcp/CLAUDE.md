# flma_mcp/ — MCP server (a consumer, over the network)

Exposes flma's live game state and factory planner to a **remote** consumer that
has no shell on this machine — the homelab Hermes agent — over Streamable HTTP.
Every other consumer in this repo (`planner/`'s CLI, the skills) reaches a local
process directly; this is the one place that crosses a network boundary, so it's
also the one place auth/staleness/concurrency actually matter.

This is additive, not a replacement: `planner/`'s CLI and `src/game_state.py`'s
file-reading model are unchanged and still work exactly as documented in their own
`CLAUDE.md`s. `flma_mcp/` is a thin server shell on top — it reuses
`planner/observe.py`'s functions and `planner/cli.py`'s command handlers verbatim
rather than reimplementing anything (see each module's docstring for specifics).

**History:** an MCP server (`src/server.py`) existed once before and was removed in
commit `da138a7` — at the time, the only consumer was Claude Code, which reaches a
CLI through `Bash` just as directly as it would MCP tools, so the extra layer had
no payoff. `flma_mcp/` reintroduces that layer because Hermes has no `Bash` into
this machine at all; the removal's own reasoning ("the only consumer... which
reaches a CLI through Bash") stops holding once the consumer is remote.

## Module map

| File | Role |
|---|---|
| `config.py` | env vars (`FLMA_MCP_HOST`/`_PORT`/`_TOKEN`/`_STALE_AFTER_SECONDS`); re-exports `SCRIPT_OUTPUT_DIR`/`RECIPES_DB` from `src.config`/`planner.config` rather than duplicating them |
| `state.py` | the one process-wide `GameState`, opted into `planner/live_state.py`'s `use_shared_game_state` so every CLI handler `cli_bridge.py` drives reuses it too; `warm()` refreshes it off the event loop before a handler's own synchronous `open_game_state()` call runs |
| `live_tools.py` | 7 tools wrapping `planner/observe.py`'s functions directly (same logic `src/server.py`'s deleted tools used), plus `flma_status` (health/freshness as structured data) |
| `cli_bridge.py` | `run_cli(argv)` — routes a real argv list through `planner.cli.build_parser()` + `_HANDLERS`, under stdout/stderr capture and a lock. **Never** builds an `argparse.Namespace` by hand (see its own docstring for why) |
| `planning_tools.py` | 12 tools wrapping the CLI's text-only planning commands (`plan`/`recommend`/`options`/`expand`/`recipe`/`producers`/`consumers`/`power`/`have`/`belts`/`tech`/`status`) via `cli_bridge`. Deliberately excludes `build-db` |
| `static_tools.py` | 9 tools ported from the homelab repo's `apps/recipe-mcp` (search/browse/machine/drill lookups + research-path expansion) — see below |
| `exposition.py` | hand-rolled Prometheus text exposition (0.0.4) writer — HELP/TYPE/label escaping, float spelling, family grouping. Zero flma knowledge, zero dependencies, fully unit-tested under `make quick` |
| `metrics.py` | `/metrics` collectors over the shared `GameState`; the top-N cardinality cap for per-item production; never raises |
| `server.py` | `FastMCP` assembly, `/healthz`, `/metrics`, the bearer-token middleware, `main()` |

Every tool result carries a `freshness` envelope (`age_seconds`/`stale`/
`game_running`/`save_id`/`note`) — see `state.freshness()`. Nothing errors when
Factorio isn't running or the export is stale; it's reported in-band instead, since
a remote caller has no other way to know.

## Why flma supersedes recipe-mcp (and the tradeoff that implies)

The homelab repo's `apps/recipe-mcp` already gave Hermes 16 Factorio recipe tools,
but its DB is baked into its container image at build time from a hand-copied,
git-committed `recipes.json` — it goes stale the moment the running save's modpack
or research state changes, and there's no way to tell from its answers alone.
flma's `recipes.db` is built from the **currently running save's own export**
(`make build-db`), and its schema is a verified strict superset of recipe-mcp's
(same 13 core tables, plus `fuels`/`generators`/`machine_fuel_categories`) — every
recipe-mcp query ports over unchanged. `static_tools.py` ports the 9 tools
`planner/cli.py` never grew subcommands for; the other 7 recipe-mcp tools
(`find_recipes_producing`/`find_recipes_using`/`get_recipe`/`plan_factory`/
`find_recipes_unlocked_by_technology`/`list_researchable_technologies`) are **not**
re-ported, because flma's own `producers`/`consumers`/`recipe`/`plan`/`recommend`/
`tech`/`tech-tree` (via `planning_tools.py`) already cover the same ground with LIVE
annotations (`[N built]` tags, live tech-scoping) a static DB can't provide.

**The accepted tradeoff:** recipe-mcp is always deployed in-cluster; flma_mcp only
runs when this desktop is up. Superseding it means Hermes loses ALL Factorio recipe
Q&A whenever this machine sleeps or is off. That's why retiring recipe-mcp in the
homelab repo is its own separate, later step — done only after flma_mcp is verified
working end to end, not bundled with standing this server up. If the outage proves
too disruptive in practice, the fallback is re-pointing recipe-mcp's own DB build at
flma's live export (killing its staleness without losing always-on), not resurrecting
the stale one as-is.

## Prometheus metrics (`/metrics`)

A Prometheus scrape endpoint alongside `/healthz`, on the same shared `GameState`
and `state.warm()`/`asyncio.to_thread` pattern every MCP tool uses (see
`metrics.build_body()`). It turns the mod's ~5s export cadence into scrapeable
series for the homelab's kube-prometheus-stack (see "Homelab-side wiring" below);
it does not replace any existing tool or command.

**Metric families** (full list in `metrics.py`'s docstrings): always-on meta
(`flma_up`, `flma_snapshot_age_seconds{snapshot}`, `flma_save_info{save_id}`,
`flma_scrape_duration_seconds`, `flma_scrape_errors_total`); game data emitted only
while `flma_up == 1` (research progress/queue, technology counts by status,
logistic/construction robot counts, building counts by name/type, and capped
per-item/per-fluid production).

**The produced/consumed naming caveat — the one thing to get right when touching
this code.** Factorio's `production.json` calls what a force *produced*
`input_counts` and what it *consumed* `output_counts` (`LuaFlowStatistics`'
naming, matching the in-game GUI's left/right split — see `SCHEMA.md` and
`planner/live_state.py`'s `net_production` docstring). The metrics here are named
`flma_items_produced_total`/`flma_items_consumed_total` — **never** carry
`input`/`output` into a metric name, that would be faithful to the JSON and a lie
to anyone reading a dashboard. `tests/unit/test_mcp_metrics.py`'s
`test_input_counts_map_to_produced_not_consumed` is the regression test that
catches a future refactor silently flipping this.

**`flma_up` vs Prometheus' own `up{job="flma"}`** — two different signals, both
needed:

| `up{job="flma"}` | `flma_up` | Meaning | Alert? |
|---|---|---|---|
| 0 | (absent) | Desktop off, unit down, or network/firewall broken | Yes — but this is *normal* when the desktop sleeps |
| 1 | 0 | Server reachable, Factorio not running or export disabled | **Never** |
| 1 | 1 | Everything live | — |

**Staleness: game-data series are dropped entirely, not frozen at their last
value**, once the export goes stale (`flma_up` flips to 0). A frozen
`flma_items_produced_per_minute` while Factorio is closed would be a false
statement, and worse, indistinguishable on a graph from a factory running
perfectly steady. **Never write an `absent()`-based alert on a production
series** — it would fire every time you stop playing. All alerting goes through
`flma_up` and `flma_snapshot_age_seconds`.

**Cardinality cap — the hard rule:** per-item/fluid production series are capped
to the top `FLMA_MCP_METRICS_TOP_ITEMS` by throughput (default 40, independently
per force and per kind), plus `FLMA_MCP_METRICS_ITEM_ALLOWLIST`, plus sticky
retention (`FLMA_MCP_METRICS_TOP_ITEMS_SLACK`) so ids near the cutoff don't flap
in and out every scrape. **Anything an alert or recording rule references must be
in the allowlist — top-N membership is not stable enough to alert on.** Measured
on a real save: ranks 34–52 by throughput sat within ~15 units/min of each other,
close enough to reorder on ordinary scrape-to-scrape noise.
`flma_metrics_items_{seen,selected}` tell you whether the cap is actually binding.

**Save-switch caveat:** production counters carry the same labels across a save
switch but describe a different factory — `_active_save_id` changes live
(`src/game_state.py`'s `_resolve_active_dir`), and the counter jumps to an
unrelated value. Usually downward (`rate()` handles it as a reset correctly);
occasionally upward (one bogus large rate sample). Accepted rather than fixed —
adding `save_id` to every production series would double their cardinality for
the overwhelmingly common single-save case. `flma_save_info{save_id}` is how a
dashboard shows the switch happened.

**Why hand-rolled text exposition instead of `prometheus_client`:** that library
is an *instrumentation* API (`Gauge`/`Counter` objects that own and mutate their
own value); flma has nothing to instrument, every number here is read fresh out
of `GameState` at scrape time, so the correct `prometheus_client` shape would be a
custom `Collector` — about the same amount of code as `exposition.py` minus its
~60 lines of rendering. Putting that rendering behind an optional dependency would
also move it outside `make quick`'s coverage, and escaping/float-spelling is
exactly the code most prone to silent breakage in a hand-rolled exporter. Don't
"simplify" this into a `prometheus_client` dependency without re-reading this
paragraph.

## Development

```bash
uv sync --extra mcp   # only this server needs the `mcp` package; planner/tests don't
make run-mcp          # foreground, binds 127.0.0.1:9110 by default

curl -s localhost:9110/healthz | python3 -m json.tool
curl -s localhost:9110/metrics | head -40

# Validate the exposition format (promtool isn't installed on this desktop):
curl -s localhost:9110/metrics > /tmp/flma.prom
podman run --rm -i -v /tmp/flma.prom:/m.prom:z --entrypoint promtool \
  quay.io/prometheus/prometheus:latest check metrics < /m.prom   # expect no output
```

`make quick` must stay green with the `mcp` extra **not** installed — that's the
check that the optional-dependency boundary (`pyproject.toml`'s
`[project.optional-dependencies] mcp`) is real. Only `server.py`/`__main__.py`
import `mcp`; every other module here (including all the tool-registration
modules) is importable without it, so unit tests for them don't need the extra —
see `tests/unit/test_mcp_cli_bridge.py`/`test_mcp_live_tools.py`.

## Desktop deployment (systemd --user)

Runs as a systemd **user** unit on the machine actually running Factorio (not a
container — the data is `$HOME/.factorio/script-output/flma`, owned by this same
user; a container would need a bind mount + uid alignment for zero isolation
benefit). Requires `loginctl enable-linger <user>` first, or the unit dies at
logout.

`~/.config/systemd/user/flma-mcp.service` (see the homelab repo's wiring below for
the values that must match): binds a **specific LAN interface** (never `0.0.0.0` —
this host may also be on a Tailscale tailnet with other people's nodes on it, and
`0.0.0.0` would expose the port there too; never loopback-only, since a remote
Hermes needs to reach it), a fixed port outside the range of every other service
already running on this desktop, `Restart=on-failure`, and `ExecStart` pointing at
the **prebuilt venv's** python directly (`uv run` wants to write into `.venv`/its
cache, which a hardened unit's `ProtectHome=read-only` blocks, and re-resolves the
lock on every start).

## Security

Two independent layers, since either one alone can fail silently:

1. **Bearer token** (`FLMA_MCP_TOKEN`) — required on every route except `/healthz`
   and `/metrics`. `/healthz` stays open so it remains a pure liveness signal, not
   gated behind the same secret it's meant to help debug — it leaks no game data.
   `/metrics` is exempt for a different reason: Prometheus' off-cluster
   `ScrapeConfig` has no clean way to hold a bearer token, so the firewalld
   `flma-mcp-allowed` ipset (below) is its actual gate — and unlike `/healthz`,
   `/metrics` **does** leak real game data (production rates, research progress,
   building counts), which makes that ipset load-bearing for confidentiality here,
   not just availability. If the token is unset, the server logs a loud warning
   and runs open rather than refusing to start — convenient for local dev against
   `127.0.0.1`, never silent.
2. **Network-level restriction** (host firewall + the consuming cluster's
   NetworkPolicy) — this desktop's firewall already has a broad port range
   (`1025-65535/tcp`) open for other services (node_exporter, Syncthing, ...), so
   don't assume the token is redundant with it, and **verify the firewall rule
   empirically, don't assume it works** — see the gotcha below, which is exactly
   the kind of mistake that fails open, not closed.

### The firewalld gotcha (verified the hard way)

The naive approach — a separate rich rule per allowed IP (`accept`) plus one bare
`rule ... port=9110 protocol=tcp drop` for everyone else — **does not work**, and
fails in the dangerous direction: it blocks *everyone*, including the allowed IPs,
not just non-allowed ones. Confirmed via `nft list ruleset`: firewalld's nftables
backend puts all `drop`/`reject` rich rules into a `filter_IN_<zone>_deny` chain
and all `accept` rules into a separate `filter_IN_<zone>_allow` chain, and **the
deny chain is evaluated before the allow chain** — so an unconditional drop rule
(no source match) terminates every packet to that port before any of the specific
accept rules, or the zone's own blanket `1025-65535/tcp` allow, ever get a chance
to run.

The fix: express the restriction as a **single deny-chain rule with a negated
source match**, using an ipset rather than one drop-rule per excluded host (there's
no way to negate "one of several specific IPs" in a single rich rule otherwise):

```bash
sudo firewall-cmd --permanent --new-ipset=flma-mcp-allowed --type=hash:ip
sudo firewall-cmd --permanent --ipset=flma-mcp-allowed --add-entry=<node-ip>   # once per cluster node
sudo firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source NOT ipset=flma-mcp-allowed port port=9110 protocol=tcp drop'
sudo firewall-cmd --reload
```

Every k3s node's IP needs an entry here, not just whichever node a particular
consumer happens to be running on right now: Hermes' pod can be scheduled to any
node, and the in-cluster Prometheus scraping `/metrics` (see "Homelab-side wiring"
below) is in the same boat — its egress to this host is SNAT'd to whichever node
it's currently on, and that pod reschedules independently of Hermes'.

Traffic from an allowed IP doesn't match the negated condition, falls through the
(now source-aware) deny chain unmatched, and proceeds to the allow chain where the
zone's existing blanket rule accepts it as normal. Traffic from anywhere else
matches the negated deny rule and is dropped before reaching the allow chain at all.

**Verify both directions after any change here** — a passing "blocked from an
unauthorized host" test alone doesn't prove the rule is *scoped* correctly; the
first version of this rule blocked EVERYONE and would have "passed" a
block-only check:

```bash
# From an ALLOWED source (must succeed) -- a hostNetwork pod on a cluster node
# reproduces the actual source IP Hermes' traffic will have.
kubectl run flma-fw-probe --restart=Never --image=curlimages/curl \
  --overrides='{"spec":{"nodeName":"<node>","hostNetwork":true}}' --command -- \
  sh -c 'curl -sS -m 5 -o /dev/null -w "HTTP %{http_code}\n" http://<this-host-ip>:9110/healthz'
kubectl logs flma-fw-probe; kubectl delete pod flma-fw-probe

# From a DISALLOWED source on the LAN (must time out / fail)
ssh <some-other-lan-host> "timeout 5 curl -sS -o /dev/null -w 'HTTP %{http_code}\n' \
  http://<this-host-ip>:9110/healthz || echo unreachable"
```

### A second gotcha, in the MCP library itself

`FastMCP(...)` auto-enables Host-header ("DNS rebinding") protection allowing ONLY
`127.0.0.1`/`localhost`/`::1` whenever it's constructed with `host` left at its own
default (`"127.0.0.1"`) — completely independent of whatever address `uvicorn.run()`
is later told to actually bind. `server.py` passes `host=config.HOST` into the
`FastMCP(...)` constructor specifically to avoid this; removing that (e.g. "cleaning
up" what looks like a redundant kwarg) silently makes every real request 421
"Invalid Host header" once bound to anything but loopback — caught by testing
against the real bind address, not just `127.0.0.1`, exactly why that's called out
in the verification steps below. See
`tests/integration/test_mcp_server_transport_security.py` for the regression test.

## Homelab-side wiring (separate repo)

Hermes (the consumer) lives in `/home/jhjaggars/code/homelab`, not here. Its
`mcp_servers` config entry, the `NetworkPolicy` egress rule letting its pod reach
this desktop, and the SOPS-encrypted token live under that repo's
`clusters/homelab/hermes/`. Verify reachability from inside the cluster with two
separate checks — from any pod on the node Hermes runs on, and from Hermes' own pod
specifically — so a routing failure and a NetworkPolicy failure can't be confused
for each other.

The same off-cluster endpoint is also scraped for Prometheus metrics: a
`ScrapeConfig` at `clusters/homelab/monitoring/scrape.yml` (job `flma`, a literal
IP target — see that file's comment on why, the same multi-A-record trap as
`hermes/networkpolicy.yaml`) feeds the homelab's kube-prometheus-stack, and a
starter Perses dashboard lives at
`clusters/homelab/perses/dashboards/flma-factory.json`. See this file's
"Prometheus metrics" section above for `flma_up` semantics, the staleness/
cardinality rules, and why "Factorio isn't running" must never page.
