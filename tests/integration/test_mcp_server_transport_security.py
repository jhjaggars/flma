"""Regression test for a real bug hit while deploying flma_mcp: FastMCP
auto-enables Host-header ("DNS rebinding") protection, allowing ONLY
127.0.0.1/localhost/::1, whenever it's constructed with `host` left at its
own default of "127.0.0.1" -- which is exactly what `FastMCP("flma",
streamable_http_path="/mcp")` (no explicit `host=`) does. Since flma_mcp
builds its ASGI app via `mcp.streamable_http_app()` and serves it with our
own `uvicorn.run(host=config.HOST, ...)` in server.py's main(), the actual
listen address and the `host` FastMCP was constructed with can silently
disagree -- and did: bound to 192.168.1.140, every real request got HTTP 421
"Invalid Host header" while 127.0.0.1 worked fine in earlier ad hoc testing,
because FastMCP had no idea it wasn't actually loopback-only.

This needs the real `mcp` package (unlike every other flma_mcp test file),
hence the whole-module skip below and its home in tests/integration/ rather
than tests/unit/."""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

pytestmark = pytest.mark.integration

from mcp.server.fastmcp import FastMCP  # noqa: E402


class TestFastMCPHostDefaultTrap:
    """Characterizes the underlying library behavior this bug depends on --
    if a future `mcp` release changes this default, these fail loudly rather
    than the fix in server.py silently rotting."""

    def test_default_host_silently_enables_loopback_only_protection(self) -> None:
        mcp = FastMCP("x", streamable_http_path="/mcp")  # no explicit host=
        assert mcp.settings.transport_security is not None
        assert mcp.settings.transport_security.enable_dns_rebinding_protection is True
        assert all(
            "127.0.0.1" in h or "localhost" in h or "::1" in h
            for h in mcp.settings.transport_security.allowed_hosts
        )

    def test_explicit_lan_host_avoids_the_auto_protection_trap(self) -> None:
        mcp = FastMCP("x", host="192.168.1.140", port=9110, streamable_http_path="/mcp")
        assert mcp.settings.transport_security is None


class TestServerPassesConfiguredHostThrough:
    """The actual regression guard: flma_mcp/server.py's module-level `mcp`
    must be constructed with `host=config.HOST` explicitly, not left to
    FastMCP's own default -- otherwise every deployment bound to anything
    other than loopback silently 421s every request, exactly as this repo's
    real systemd-deployed instance did until this was found.

    Asserting `server.mcp.settings.host == config.HOST` alone would pass
    trivially in a test process where FLMA_MCP_HOST is unset (both default
    to "127.0.0.1"), which wouldn't have caught the actual bug. This forces
    a real LAN-like host through env + reload first, so the assertion only
    passes if server.py genuinely threads it through to FastMCP.
    """

    def test_module_level_mcp_instance_was_constructed_with_configured_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        monkeypatch.setenv("FLMA_MCP_HOST", "192.168.1.140")
        monkeypatch.setenv("FLMA_MCP_PORT", "9110")
        from flma_mcp import config, server

        try:
            importlib.reload(config)
            importlib.reload(server)
            assert server.mcp.settings.host == "192.168.1.140"
            assert server.mcp.settings.port == 9110
            # The behavior that actually matters: a non-loopback host must
            # NOT trip FastMCP's loopback-only auto-protection.
            assert server.mcp.settings.transport_security is None
        finally:
            # Restore both modules to their env-default state so later
            # tests in the same process don't inherit this reload.
            monkeypatch.undo()
            importlib.reload(config)
            importlib.reload(server)
