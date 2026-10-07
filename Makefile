# Knowledge OS — local developer tasks.
# Backend-owned targets (M0-003 code1; web-rebuild added by code2, boss micro-task).
# Run `make help` for the list.

SHELL := /bin/bash
COMPOSE ?= docker compose
PYTHON ?= python3
PKG_PATH := packages
API_PATH := apps/api
WORKER_PATH := apps/worker

.DEFAULT_GOAL := help

.PHONY: help up down logs ps build rebuild web-rebuild restart migrate revision shell-api shell-db lint fmt typecheck test test-unit test-integration api-dev worker-dev clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

up: ## Build and start the full local stack
	$(COMPOSE) up -d --build

down: ## Stop and remove the stack (keeps volumes)
	$(COMPOSE) down

logs: ## Follow logs for all services
	$(COMPOSE) logs -f --tail=200

ps: ## Show service status
	$(COMPOSE) ps

build: ## Build api/worker images
	$(COMPOSE) build api worker

rebuild: ## Rebuild api/worker and restart
	$(COMPOSE) up -d --build api worker

web-rebuild: ## Rebuild the web image and restart it (WEB_PORT=3001 make web-rebuild when :3000 is taken)
	$(COMPOSE) build web
	$(COMPOSE) up -d web

restart: ## Restart api and worker
	$(COMPOSE) restart api worker

migrate: ## Apply Alembic migrations (host, requires running postgres)
	cd $(API_PATH) && PYTHONPATH=../../$(PKG_PATH) alembic upgrade head

revision: ## Create a new Alembic revision: make revision m="add table"
	cd $(API_PATH) && PYTHONPATH=../../$(PKG_PATH) alembic revision -m "$(m)"

shell-api: ## Open a shell in the running api container
	$(COMPOSE) exec api /bin/sh

shell-db: ## Open psql in the running postgres container
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-knowledge} -d $${POSTGRES_DB:-knowledge}

api-dev: ## Run the API locally with reload
	cd $(API_PATH) && PYTHONPATH=../../$(PKG_PATH) $(PYTHON) -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

worker-dev: ## Run the worker locally
	cd $(WORKER_PATH) && PYTHONPATH=../../$(PKG_PATH) $(PYTHON) -m worker.main

lint: ## Ruff lint
	ruff check .

fmt: ## Ruff format
	ruff format .

typecheck: ## mypy typecheck
	mypy

test: ## Run the full test suite
	pytest -q

test-unit: ## Run unit tests only
	pytest -q -m unit

test-integration: ## Run integration/boot tests
	pytest -q -m integration

clean: ## Remove caches
	find . -type d -name __pycache__ -prune -exec rm -rf {} + ; rm -rf .pytest_cache .mypy_cache .ruff_cache
