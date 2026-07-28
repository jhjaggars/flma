"""Integration test for the `/metrics` HTTP route added to flma_mcp/server.py.

Route registration and the bearer-token auth exemption both live in
server.py, which imports the real `mcp` package -- so they can't be unit-
tested (see tests/unit/test_mcp_exposition.py / test_mcp_metrics.py for
everything about rendering/collection that *can* be, without the extra).
Mirrors tests/integration/test_mcp_server_transport_security.py's shape:
whole-module skip if `mcp` isn't installed, and the same
monkeypatch.setenv + importlib.reload(config); importlib.reload(server)
dance (with its own `finally:` restore) to force a real bearer token through
without leaking that reload into later tests in the same process.

`flma_mcp`/`mcp`/`starlette` imports are deliberately deferred into the
fixture body rather than done at module top: tests/integration/ (unlike
tests/unit/) has no `__init__.py`, so a bare `pytest` invocation (as opposed
to `python -m pytest`) doesn't add the repo root to sys.path at collection
time, and a module-level `from flma_mcp import ...` fails to import before
`pytest.importorskip` even gets a chance to run. The sibling
test_mcp_server_transport_security.py already avoids this the same way (its
`from flma_mcp import config, server` lives inside a test method, not at
module level).
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

pytestmark = pytest.mark.integration


@pytest.fixture
def client_with_token(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """A TestClient against a server reloaded with a real bearer token and
    an isolated SCRIPT_OUTPUT_DIR, restored afterwards so this doesn't leak
    into other integration tests running in the same process.

    SCRIPT_OUTPUT_DIR is set via monkeypatch.setattr rather than
    monkeypatch.setenv + reload: flma_mcp/config.py re-exports it from
    src.config, which reads the env var once at src.config's own first
    import and is never reloaded here, so an env var change alone wouldn't
    be visible -- setting the attribute directly on the (already-reloaded)
    config module object is what state.init() below actually reads.
    """
    import importlib

    from flma_mcp import config, server

    monkeypatch.setenv("FLMA_MCP_TOKEN", "test-token")
    try:
        importlib.reload(config)
        importlib.reload(server)
        monkeypatch.setattr(config, "SCRIPT_OUTPUT_DIR", tmp_path)
        server.state.reset_for_tests()
        server.state.init()
        from starlette.testclient import TestClient

        with TestClient(server.build_app()) as client:
            yield client
    finally:
        monkeypatch.undo()
        importlib.reload(config)
        importlib.reload(server)
        server.state.reset_for_tests()


class TestMetricsRouteAuthExemption:
    def test_metrics_is_reachable_with_no_auth_header(self, client_with_token) -> None:
        response = client_with_token.get("/metrics")
        assert response.status_code == 200

    def test_metrics_content_type_is_prometheus_text_0_0_4(self, client_with_token) -> None:
        response = client_with_token.get("/metrics")
        assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"

    def test_metrics_body_contains_flma_up_type_line(self, client_with_token) -> None:
        response = client_with_token.get("/metrics")
        assert "# TYPE flma_up gauge" in response.text

    def test_mcp_route_still_requires_auth(self, client_with_token) -> None:
        # Proves the exemption widened by exactly one path, not to everything.
        response = client_with_token.post("/mcp", json={})
        assert response.status_code == 401

    def test_trailing_slash_does_not_401(self, client_with_token) -> None:
        response = client_with_token.get("/metrics/", follow_redirects=False)
        assert response.status_code != 401
