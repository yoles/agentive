SHELL := /bin/bash
.DEFAULT_GOAL := help

PROJECT := agentive
DC := docker compose
DC_DEV := docker compose
DC_PROD := docker compose -f docker-compose.yml -f docker-compose.prod.yml

# Images officielles latest stable (Sprint 0)
IMG_NODE := node:24-alpine
IMG_PYTHON := python:3.14-slim
IMG_GITLEAKS := zricethezav/gitleaks:latest
IMG_PRECOMMIT := ghcr.io/pre-commit/action:latest
IMG_PGVECTOR := pgvector/pgvector:pg17

# Helper : exécuter une commande dans un container éphémère avec le workdir monté
RUN_NODE = docker run --rm -v $(PWD):/workspace -w /workspace $(IMG_NODE)
RUN_PYTHON = docker run --rm -v $(PWD):/workspace -w /workspace $(IMG_PYTHON)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HELP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

.PHONY: help
help: ## Affiche l'aide (cette liste)
	@echo "Agentive — Makefile orchestrateur (Docker-first)"
	@echo ""
	@echo "Cibles disponibles :"
	@awk 'BEGIN {FS = ":.*##"; OFS = "  "} /^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SCAFFOLDING (init one-time)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

.PHONY: init
init: init-frontend init-backend ## Scaffolde frontend + backend via containers éphémères (one-time)
	@echo "✅ Init terminée. Prochain pas : make up"

.PHONY: init-frontend
init-frontend: ## Scaffolde frontend/ via node:24-alpine (Vite + shadcn v4 + deps)
	@echo "🎨 Scaffolding frontend via $(IMG_NODE)..."
	@mkdir -p frontend
	@docker run --rm -v $(PWD):/workspace -w /workspace $(IMG_NODE) sh -c "\
		if [ ! -f frontend/package.json ]; then \
			npm create vite@latest frontend -- --template react-ts --yes; \
		else \
			echo '  frontend/package.json existe déjà, skip création Vite'; \
		fi && \
		cd frontend && \
		npm install @tanstack/react-router @tanstack/react-query zustand react-hook-form zod react-markdown rehype-sanitize lucide-react cmdk next-themes tailwindcss@latest @tailwindcss/vite@latest class-variance-authority clsx tailwind-merge && \
		npm install -D openapi-typescript vitest @testing-library/react @testing-library/jest-dom jsdom eslint-plugin-jsx-a11y eslint-plugin-boundaries @vitejs/plugin-react \
	"
	@echo "✅ Frontend scaffold terminé"

.PHONY: init-backend
init-backend: ## Scaffolde backend/ via python:3.14-slim (FastAPI + uv + deps)
	@echo "🐍 Scaffolding backend via $(IMG_PYTHON)..."
	@mkdir -p backend
	@docker run --rm -v $(PWD):/workspace -w /workspace $(IMG_PYTHON) sh -c "\
		pip install --quiet --no-cache-dir --root-user-action=ignore uv && \
		cd backend && \
		if [ ! -f pyproject.toml ]; then \
			uv init --package agentive-backend . --no-readme; \
		else \
			echo '  backend/pyproject.toml existe déjà, skip uv init'; \
		fi && \
		uv add --no-sync fastapi 'uvicorn[standard]' 'sqlalchemy[asyncio]' alembic pgvector pydantic pydantic-settings langgraph langchain-anthropic langchain-openai mcp python-multipart sse-starlette structlog cryptography slowapi fastembed apscheduler psycopg[binary] && \
		uv add --no-sync --dev pytest pytest-asyncio ruff mypy 'langgraph-cli[inmem]' import-linter polyfactory testcontainers cyclonedx-py && \
		uv sync \
	"
	@echo "✅ Backend scaffold terminé"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DEV STACK (docker compose)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

.PHONY: up
up: ## Démarre le stack dev (db + backend + frontend + caddy) en arrière-plan
	$(DC_DEV) up -d
	@echo "✅ Stack démarré. Accès : https://localhost:8443 (self-signed)"
	@$(DC_DEV) ps

.PHONY: dev
dev: up ## Alias de make up

.PHONY: down
down: ## Arrête le stack dev (préserve volumes)
	$(DC_DEV) down

.PHONY: nuke
nuke: ## Arrête le stack + supprime volumes (DANGER : perte données DB)
	$(DC_DEV) down -v

.PHONY: build
build: ## Rebuild les images Docker du stack
	$(DC_DEV) build

.PHONY: ps
ps: ## Statut des services
	$(DC_DEV) ps

.PHONY: logs
logs: ## Stream des logs (Ctrl+C pour sortir)
	$(DC_DEV) logs -f --tail 100

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BACKEND COMMANDS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

.PHONY: backend-shell
backend-shell: ## Ouvre un shell dans le container backend
	$(DC_DEV) exec backend sh

.PHONY: backend-install
backend-install: ## (Re)synchronise les dépendances Python (uv sync)
	@docker run --rm -v $(PWD)/backend:/app -w /app $(IMG_PYTHON) sh -c "\
		pip install --quiet --no-cache-dir --root-user-action=ignore uv && uv sync \
	"

.PHONY: migrate
migrate: ## Applique les migrations Alembic (alembic upgrade head)
	$(DC_DEV) run --rm backend uv run alembic upgrade head

.PHONY: migrate-new
migrate-new: ## Crée une nouvelle migration (usage: make migrate-new MSG="description")
	$(DC_DEV) run --rm backend uv run alembic revision --autogenerate -m "$(MSG)"

.PHONY: migrate-init
migrate-init: ## Crée la migration initiale (manuel, pas autogenerate)
	$(DC_DEV) run --rm backend uv run alembic revision -m "initial schema"

.PHONY: test-backend
test-backend: ## Exécute les tests Python
	$(DC_DEV) run --rm backend uv run pytest

.PHONY: lint-backend
lint-backend: ## Lint + format check Python (ruff + mypy)
	$(DC_DEV) run --rm backend sh -c "uv run ruff check . && uv run ruff format --check . && uv run mypy src/"

.PHONY: format-backend
format-backend: ## Format Python (ruff format)
	$(DC_DEV) run --rm backend uv run ruff format .

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FRONTEND COMMANDS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

.PHONY: frontend-shell
frontend-shell: ## Ouvre un shell dans le container frontend
	$(DC_DEV) exec frontend sh

.PHONY: frontend-install
frontend-install: ## (Re)synchronise les dépendances Node (npm install)
	@docker run --rm -v $(PWD)/frontend:/app -w /app $(IMG_NODE) npm install

.PHONY: test-frontend
test-frontend: ## Exécute les tests Vitest
	$(DC_DEV) run --rm frontend npm run test -- --run

.PHONY: lint-frontend
lint-frontend: ## Lint frontend (eslint + tsc noEmit)
	$(DC_DEV) run --rm frontend sh -c "npm run lint && npx tsc --noEmit"

.PHONY: gen-api-types
gen-api-types: ## Génère les types TS depuis le schéma OpenAPI du backend
	$(DC_DEV) run --rm frontend sh -c "npx openapi-typescript http://backend:8000/openapi.json -o src/shared/api/types.ts"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AGGREGATES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

.PHONY: test
test: test-backend test-frontend ## Tests backend + frontend

.PHONY: lint
lint: lint-backend lint-frontend ## Lint backend + frontend

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DATABASE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

.PHONY: db-shell
db-shell: ## Ouvre psql sur la DB (role agentive_owner)
	$(DC_DEV) exec db psql -U agentive_owner -d agentive

.PHONY: db-reset
db-reset: ## DROP DATABASE + recreate + migrations (DANGER : perte données)
	$(DC_DEV) exec db psql -U postgres -c "DROP DATABASE IF EXISTS agentive;"
	$(DC_DEV) exec db psql -U postgres -c "CREATE DATABASE agentive OWNER agentive_owner;"
	$(MAKE) migrate

.PHONY: db-backup
db-backup: ## pg_dump de la DB vers ./backups/agentive-YYYYMMDD.sql
	@mkdir -p backups
	$(DC_DEV) exec -T db pg_dump -U agentive_owner agentive > "backups/agentive-$$(date +%Y%m%d).sql"
	@echo "✅ Backup: backups/agentive-$$(date +%Y%m%d).sql"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PRE-COMMIT + SECURITY
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

.PHONY: precommit-install
precommit-install: ## Installe les git hooks pre-commit (via image Docker pre-commit)
	@echo "🪝 Installation des git hooks pre-commit via Docker..."
	@mkdir -p .git/hooks
	@cp infra/scripts/pre-commit.sh .git/hooks/pre-commit 2>/dev/null || echo "  (pre-commit.sh à créer en T5)"
	@chmod +x .git/hooks/pre-commit 2>/dev/null || true
	@echo "✅ pre-commit hook installé (exécution via Docker)"

.PHONY: precommit-run
precommit-run: ## Exécute manuellement tous les hooks pre-commit sur tous les fichiers
	@docker run --rm -v $(PWD):/src $(IMG_GITLEAKS) detect --source="/src" --no-banner -v

.PHONY: gitleaks
gitleaks: ## Scan gitleaks sur le repo
	docker run --rm -v $(PWD):/src $(IMG_GITLEAKS) detect --source="/src" --no-banner -v

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BENCHMARKS (Sprint 0 Stories 1.2 + 1.3)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

.PHONY: bench
bench: ## Exécute les benchmarks M4 pgvector (Story 1.3)
	$(DC_DEV) run --rm backend uv run python -m scripts.benchmark_m4

.PHONY: spike-m3
spike-m3: ## Exécute le spike M3 LangGraph (Story 1.2)
	$(DC_DEV) run --rm backend uv run python -m spike.m3_langgraph

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MISC
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

.PHONY: clean
clean: ## Supprime node_modules, __pycache__, caches (préserve volumes Docker)
	@find . -type d \( -name '__pycache__' -o -name '.pytest_cache' -o -name '.mypy_cache' -o -name '.ruff_cache' -o -name 'node_modules' -o -name '.venv' -o -name 'dist' -o -name 'build' \) -prune -exec rm -rf {} \; 2>/dev/null || true
	@echo "✅ Caches nettoyés"

.PHONY: pull
pull: ## Pull toutes les images Docker utilisées par le stack
	$(DC_DEV) pull
