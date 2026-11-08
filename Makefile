# AgentEscrow developer commands.  Run `make help` for the list.
VENV   ?= .venv
PY     ?= $(VENV)/bin/python
PORT   ?= 4000
UPPORT ?= 4101

.PHONY: help install dev demo test e2e lint fmt check ui clean

help:  ## Show this help
	@grep -E '^[a-z0-9]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-8s %s\n", $$1, $$2}'

$(PY):
	python3 -m venv $(VENV)

install: $(PY)  ## Create .venv, install Python dev deps, web deps and git hooks
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r requirements-dev.txt
	cd web && npm ci
	$(VENV)/bin/pre-commit install

dev:  ## Run proxy (dev mode) + fake MCP server; Ctrl-C stops both
	@echo "fake MCP server : http://127.0.0.1:$(UPPORT)/mcp"
	@echo "proxy + UI      : http://127.0.0.1:$(PORT)/ui/   (run 'make demo' in another terminal)"
	@trap 'kill 0' INT TERM EXIT; \
	FAKE_UPSTREAM_PORT=$(UPPORT) $(PY) scripts/fake_upstream.py & \
	ESCROW_MODE=dev ESCROW_PORT=$(PORT) MCP_UPSTREAM_URL=http://127.0.0.1:$(UPPORT)/mcp \
	  DB_PATH=.dev-escrow.db $(PY) -m proxy

demo:  ## Demo agent: sends safe + risky calls to the running proxy
	ESCROW_URL=http://127.0.0.1:$(PORT) $(PY) scripts/demo_agent.py

test:  ## Unit tests
	$(PY) -m pytest -m "not e2e"

e2e:  ## End-to-end tests (real proxy + fake upstream processes)
	$(PY) -m pytest -m e2e

lint:  ## ruff + pyright
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .
	$(VENV)/bin/pyright

fmt:  ## Auto-fix lint and format
	$(VENV)/bin/ruff check --fix .
	$(VENV)/bin/ruff format .

check: lint  ## Everything CI runs (lint, types, all tests)
	$(PY) -m pytest

ui:  ## Build the dashboard into web/out
	cd web && npm run build

clean:  ## Remove the local dev database and tool caches
	rm -f .dev-escrow.db .dev-escrow.db-wal .dev-escrow.db-shm
	rm -r -f .pytest_cache .ruff_cache
