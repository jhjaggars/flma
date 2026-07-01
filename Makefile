# Makefile for factorio-live-mcp
# Uses 'uv run' to execute commands in the project environment

.PHONY: help install dev test lint format typecheck clean run mod-zip

.DEFAULT_GOAL := help

help: ## Show this help message
	@echo "factorio-live-mcp - Development Commands"
	@echo ""
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

install: ## Install dependencies
	uv sync

dev: ## Set up development environment
	uv sync --group dev
	uv run pre-commit install
	@echo ""
	@echo "✓ Development environment ready!"

test: ## Run unit tests
	uv run nox -s tests

test-cov: ## Run tests with coverage
	uv run nox -s tests_with_coverage

lint: ## Run linter (ruff)
	uv run nox -s lint

format: ## Auto-format code
	uv run nox -s format

typecheck: ## Run type checker (mypy)
	uv run nox -s typecheck

quick: ## Quick validation (lint + typecheck + tests)
	uv run nox -s quick

ci: ## Full CI validation
	uv run nox -s ci

clean: ## Clean up generated files
	uv run nox -s clean

run: ## Run MCP server locally (for dev/testing)
	uv run python -m src.server

mod-zip: ## Zip the flma mod for local install (~/.factorio/mods) or the mod portal
	@VERSION=$$(python3 -c "import json; print(json.load(open('mod/info.json'))['version'])"); \
	rm -rf /tmp/flma_$$VERSION flma_$$VERSION.zip; \
	cp -r mod /tmp/flma_$$VERSION; \
	(cd /tmp && zip -rq "$(CURDIR)/flma_$$VERSION.zip" "flma_$$VERSION"); \
	rm -rf /tmp/flma_$$VERSION; \
	echo "Built flma_$$VERSION.zip"

fix: format ## Fix formatting and linting issues

check: quick ## Quick check before commit

ci-local: ## Run full CI pipeline locally
	uv run nox -s ci
