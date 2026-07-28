"""flma_mcp — an MCP server exposing flma's live game state and factory
planner over Streamable HTTP, for a remote consumer (the homelab Hermes
agent) that has no shell on the machine running Factorio.

This module deliberately imports nothing from the `mcp` package (or any
other submodule that does) — every other file under `src/` and `planner/`
must stay importable in the default, stdlib-only environment (see
pyproject.toml's `dependencies = []`), and `flma_mcp` itself must be
importable too so that `python -c "import flma_mcp"` doesn't require the
optional `mcp` extra just to exist on disk. Only `flma_mcp.server` (and
whatever it imports) requires `uv sync --extra mcp`.
"""

from __future__ import annotations
