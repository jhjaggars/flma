"""Recipe-calculation engine and DB builder, vendored into flma.

Originally developed in `recipe-mcp` (github.com/jhjaggars/recipe-mcp, a
standalone sibling project) as the FastMCP-independent core its MCP server
wrapped. Vendored here so the planner CLI is self-contained: this repo's own
`recipes.json` export (see `../../SCHEMA.md`) builds straight into a
`recipes.db` via `build_db.py`, and `engine.py` computes recipe-chain
expansion and machine/drill counts against it — no external checkout, no MCP
server, no network access.

    engine.py    -- pure calculation logic: recipe selection, recursive
                    bill-of-materials expansion, machine/drill sizing.
    db.py        -- thin async wrapper around the read-only recipes.db.
    build_db.py  -- offline builder: recipes.json -> recipes.db (run via
                    `make build-db` or `python -m planner.recipedb.build_db`).
"""
