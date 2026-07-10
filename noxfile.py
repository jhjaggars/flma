"""Nox sessions for testing and validation."""

import nox

PYTHON_VERSION = None  # Use current Python interpreter
SRC_DIRS = ["src", "planner", "tests"]

nox.options.sessions = ["lint", "typecheck", "tests"]

MAIN_DEPS: list[str] = []
TEST_DEPS = ["pytest", "pytest-asyncio", "pytest-cov"]


@nox.session(python=PYTHON_VERSION)
def lint(session):
    """Run code linters (ruff)."""
    session.install("ruff")
    session.run("ruff", "check", "src/", "planner/", *session.posargs)
    session.run("ruff", "format", "--check", "src/", "planner/")
    session.log("✓ Linting passed!")


@nox.session(python=PYTHON_VERSION)
def format(session):
    """Auto-format code with ruff."""
    session.install("ruff")
    session.run("ruff", "format", "src/", "planner/", "tests/")
    session.run("ruff", "check", "--fix", "src/", "planner/", "tests/")
    session.log("✓ Code formatted!")


@nox.session(python=PYTHON_VERSION)
def typecheck(session):
    """Run type checking with mypy."""
    session.install("mypy", *MAIN_DEPS)
    session.run(
        "mypy",
        "src/",
        "planner/",
        "--ignore-missing-imports",
        "--no-strict-optional",
        "--warn-unused-ignores",
        *session.posargs,
    )
    session.log("✓ Type checking passed!")


@nox.session(python=PYTHON_VERSION)
def tests(session):
    """Run unit tests with pytest."""
    session.install(*MAIN_DEPS, *TEST_DEPS)
    session.run("pytest", "tests/unit/", "-v", "--tb=short", *session.posargs)
    session.log("✓ Unit tests passed!")


@nox.session(python=PYTHON_VERSION)
def tests_with_coverage(session):
    """Run tests with coverage report."""
    session.install(*MAIN_DEPS, *TEST_DEPS)
    session.run(
        "pytest",
        "tests/unit/",
        "-v",
        "--cov=src",
        "--cov-report=term-missing",
        "--cov-branch",
        "--cov-fail-under=60",
        *session.posargs,
    )
    session.log("✓ Tests passed with coverage!")


@nox.session(python=PYTHON_VERSION, name="quick")
def quick_validation(session):
    """Quick validation: lint + typecheck + unit tests."""
    session.notify("lint")
    session.notify("typecheck")
    session.notify("tests")


@nox.session(python=PYTHON_VERSION, name="ci")
def ci_validation(session):
    """Full CI validation: all code quality + tests."""
    session.notify("lint")
    session.notify("typecheck")
    session.notify("tests_with_coverage")


@nox.session(python=PYTHON_VERSION)
def clean(session):
    """Clean up generated files."""
    import shutil
    from pathlib import Path

    paths_to_clean = [
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".coverage",
        "htmlcov",
        "dist",
        "build",
        "*.egg-info",
        ".nox",
        "__pycache__",
    ]

    for pattern in paths_to_clean:
        for path in Path(".").rglob(pattern):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)

    session.log("✓ Cleanup complete!")
