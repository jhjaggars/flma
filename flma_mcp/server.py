"""flma MCP server: Streamable HTTP at /mcp, plus an open (no-auth) /healthz.

Run via `make run-mcp`, `uv run --extra mcp python -m flma_mcp`, or the
`flma-mcp` console script (after `uv sync --extra mcp`). See
flma_mcp/CLAUDE.md for the desktop systemd unit and the homelab-side wiring.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover -- exercised by hand, not in CI
    print(
        "flma_mcp requires the 'mcp' extra: uv sync --extra mcp\n"
        "(the flma repo itself stays dependency-free; this is opt-in.)",
        file=sys.stderr,
    )
    raise SystemExit(1) from None

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from flma_mcp import config, live_tools, metrics, planning_tools, state, static_tools

logger = logging.getLogger("flma_mcp")

_START_TIME = time.monotonic()


# `host`/`port` here matter for ONE thing: FastMCP auto-enables DNS-rebinding
# Host-header protection, allowing only 127.0.0.1/localhost/::1, whenever
# `host` is one of those three (its own default is "127.0.0.1", so leaving
# this unset would silently reject every real request once bound to a LAN
# address -- exactly what happened in testing: 127.0.0.1 worked, the real
# bind address got HTTP 421 "Invalid Host header"). The actual bind/listen
# address is controlled entirely by uvicorn.run() in main() below, since we
# build the ASGI app ourselves (build_app()) rather than calling mcp.run().
mcp = FastMCP("flma", host=config.HOST, port=config.PORT, streamable_http_path="/mcp")
live_tools.register(mcp)
planning_tools.register(mcp)
static_tools.register(mcp)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> JSONResponse:
    """Liveness + freshness for humans and the in-cluster reachability
    check (see flma_mcp/CLAUDE.md's verification section) -- deliberately
    NOT behind the bearer token (BearerTokenMiddleware below exempts this
    path), so it stays a pure "is this reachable at all" signal that leaks
    no game data. Returns 503 only when SCRIPT_OUTPUT_DIR itself is
    missing; "Factorio isn't running right now" is reported in the body
    (`game_running: false`) rather than as an unhealthy status -- the
    server is still fully useful for planning off recipes.db in that case,
    and flapping Restart=on-failure every time the user quits the game
    would be actively wrong."""
    gs = state.gs()
    if not gs.health_check():
        return JSONResponse(
            {"status": "error", "reason": f"{gs.base_dir} does not exist"}, status_code=503
        )
    await state.warm()
    status_payload = await asyncio.to_thread(live_tools._status_payload)
    payload = {
        "status": "ok",
        "uptime_seconds": time.monotonic() - _START_TIME,
        **status_payload,
    }
    return JSONResponse(payload)


# Paths BearerTokenMiddleware exempts from auth. /healthz leaks no game data
# (see its docstring above); /metrics does -- production rates, research
# progress, building counts -- so its exemption is a deliberate tradeoff, not
# an oversight: Prometheus' off-cluster ScrapeConfig has no clean way to hold
# a bearer token, and the firewalld `flma-mcp-allowed` ipset is the actual
# gate for both routes (see flma_mcp/CLAUDE.md's security section). That
# makes the ipset load-bearing for confidentiality here, not just
# availability.
_OPEN_PATHS = frozenset({"/healthz", "/metrics"})


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics_route(request: Request) -> PlainTextResponse:
    """Prometheus scrape endpoint. Auth-exempt for the reason documented at
    `_OPEN_PATHS` above. `metrics.build_body()` never raises -- each
    collector traps its own exceptions so a partial scrape still carries
    `flma_up`, the one series every alert should hang off (see
    `flma_mcp/metrics.py` and `flma_mcp/CLAUDE.md`)."""
    body = await metrics.build_body()
    return PlainTextResponse(body, media_type=metrics.CONTENT_TYPE)


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Requires `Authorization: Bearer <FLMA_MCP_TOKEN>` on every route
    except /healthz and /metrics (see _OPEN_PATHS above). If FLMA_MCP_TOKEN
    is unset, runs open -- convenient for local dev against 127.0.0.1, never
    silent (see the startup warning in main())."""

    async def dispatch(self, request: Request, call_next):
        # .rstrip("/") so a request to "/metrics/" doesn't 401 before
        # Starlette's own redirect_slashes gets a chance to 307 it -- that
        # redirect is produced *inside* the app, downstream of this
        # middleware, so a bare `==` check would reject it first.
        if config.TOKEN is None or request.url.path.rstrip("/") in _OPEN_PATHS:
            return await call_next(request)
        expected = f"Bearer {config.TOKEN}"
        if request.headers.get("authorization") != expected:
            return PlainTextResponse("unauthorized", status_code=401)
        return await call_next(request)


def build_app():
    app = mcp.streamable_http_app()
    app.add_middleware(BearerTokenMiddleware)
    return app


def main() -> int:
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    if config.TOKEN is None:
        logger.warning(
            "FLMA_MCP_TOKEN is not set -- running WITHOUT authentication. "
            "Every tool call and /healthz will be answered to anyone who can "
            "reach %s:%s. Set FLMA_MCP_TOKEN before exposing this beyond "
            "127.0.0.1.",
            config.HOST,
            config.PORT,
        )

    logger.info("loading initial game state from %s", config.SCRIPT_OUTPUT_DIR)
    state.init()

    import uvicorn

    tool_count = len(asyncio.run(mcp.list_tools()))
    logger.info("starting flma-mcp on %s:%s (tools: %d)", config.HOST, config.PORT, tool_count)

    uvicorn.run(build_app(), host=config.HOST, port=config.PORT, log_level=config.LOG_LEVEL.lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
