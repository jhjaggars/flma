"""Imports recipe-mcp's `src` package (engine.py + db.py) under a distinct
module name.

recipe-mcp ([github.com/jhjaggars/recipe-mcp](https://github.com/jhjaggars/recipe-mcp),
a standalone sibling project, normally checked out at ~/code/recipe-mcp)
names its own package `src` — same as flma's own `src/`. A plain `import
src.engine` would silently resolve to whichever `src` package Python's
import system already has cached (almost certainly flma's, since it's
imported first), not recipe-mcp's. Instead, this module loads recipe-mcp's
`src/` directory under the distinct name `recipe_mcp_src` via `importlib`,
so its internal `from .db import ...` / `from .config import ...` relative
imports keep resolving correctly, unmodified.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_PKG_NAME = "recipe_mcp_src"


def _ensure_package_loaded(recipe_mcp_dir: Path) -> None:
    if _PKG_NAME in sys.modules:
        return

    pkg_dir = recipe_mcp_dir / "src"
    init_path = pkg_dir / "__init__.py"
    if not init_path.exists():
        raise FileNotFoundError(
            f"recipe-mcp package not found at {pkg_dir} "
            f"(set RECIPE_MCP_DIR if the recipe-mcp checkout is elsewhere)"
        )

    spec = importlib.util.spec_from_file_location(
        _PKG_NAME, init_path, submodule_search_locations=[str(pkg_dir)]
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build an import spec for {init_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PKG_NAME] = module
    spec.loader.exec_module(module)


def load_engine(recipe_mcp_dir: Path) -> ModuleType:
    """Import and return recipe-mcp's `engine` module (the recipe-expansion
    and machine-count calculation logic). Safe to call repeatedly — the
    underlying package is only imported once per process."""
    _ensure_package_loaded(recipe_mcp_dir)
    return importlib.import_module(f"{_PKG_NAME}.engine")


def load_async_database_class(recipe_mcp_dir: Path) -> type:
    """Return recipe-mcp's `AsyncDatabase` class, for constructing an
    instance to hand to `engine.set_db(...)`."""
    _ensure_package_loaded(recipe_mcp_dir)
    db_module = importlib.import_module(f"{_PKG_NAME}.db")
    return db_module.AsyncDatabase  # type: ignore[no-any-return]
