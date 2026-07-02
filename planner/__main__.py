"""Allows `python -m planner` as a shorthand for `python -m planner.cli`."""

from planner.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
