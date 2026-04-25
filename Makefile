SHELL := /bin/bash
.DEFAULT_GOAL := help
# `init` interne lance 2 scripts Docker concurrents sur le même workspace →
# les sérialiser évite les courses de création/écriture sur package.json / pyproject.toml.
.NOTPARALLEL:

# Use $(CURDIR) (make built-in, always absolute, no shell expansion) rather
# than $(PWD) which relies on the shell and is affected by `cd` / spaces.
WORKDIR := $(CURDIR)

# Map the host UID/GID into containers so generated files (uv.lock, .venv,
# node_modules, dist) are owned by the developer, not root.
# Wrapped with `?=` + fallback to `1000:1000` to keep CI parity.
HOST_UID ?= $(shell id -u 2>/dev/null || echo 1000)
HOST_GID ?= $(shell id -g 2>/dev/null || echo 1000)
USER_FLAG := -u $(HOST_UID):$(HOST_GID)

PROJECT := agentive
DC := docker compose
DC_DEV := docker compose
DC_PROD := docker compose -f docker-compose.yml -f docker-compose.prod.yml

# Images — pinned to explicit versions. Bump deliberately + verify reproducibility.
# For full supply-chain hardening, append `@sha256:...` digests (see Story 9.6).
IMG_NODE := node:24-alpine3.21
IMG_PYTHON := python:3.14.4-slim
IMG_GITLEAKS := zricethezav/gitleaks:v8.30.1
IMG_PGVECTOR := pgvector/pgvector:pg17
IMG_ALPINE := alpine:3.21
# `age` est fourni par le package `age` dans Alpine 3.21 (v1.2.x).
# On l'installe à la volée dans un container éphémère au lieu de dépendre
# d'une image pre-built (aucune image publique officielle de `FiloSottile/age`).

# Helper : execute a command in an ephemeral container with the workdir mounted.
# Paths are quoted so spaces in $(CURDIR) don't break the invocation.
RUN_NODE = docker run --rm $(USER_FLAG) -v "$(WORKDIR):/workspace" -w /workspace $(IMG_NODE)
RUN_PYTHON = docker run --rm $(USER_FLAG) -v "$(WORKDIR):/workspace" -w /workspace $(IMG_PYTHON)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GUARDS (internal targets, used as prerequisites)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

.PHONY: _check-env
_check-env:  ## (interne) vérifie qu'un fichier .env existe à la racine
	@if [ ! -f "$(WORKDIR)/.env" ]; then \
		echo "❌ .env absent à la racine — copier .env.example :"; \
		echo "   cp .env.example .env && ${EDITOR:-vi} .env"; \
		exit 1; \
	fi

.PHONY: _validate-msg
_validate-msg:  ## (interne) valide MSG pour éviter shell injection dans `migrate-new`
	@if [ -z "$(MSG)" ]; then \
		echo "❌ MSG= obligatoire (ex: make migrate-new MSG=\"add user table\")"; \
		exit 1; \
	fi
	@if echo '$(MSG)' | grep -qE '[^A-Za-z0-9 _.,:\/\-]'; then \
		echo "❌ MSG contient des caractères interdits (autorisés : A-Za-z0-9 _.,:/-)"; \
		exit 1; \
	fi

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
# SCAFFOLDING (init one-time) — séquentiel, pas parallélisable
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

.PHONY: init
init: init-backend init-frontend ## Scaffolde frontend + backend via containers éphémères (one-time)
	@echo "✅ Init terminée. Prochain pas : cp .env.example .env && make up"

.PHONY: init-frontend
init-frontend: ## Scaffolde frontend/ via node:24 (Vite + shadcn primitives + deps)
	@echo "🎨 Scaffolding frontend via $(IMG_NODE)..."
	@mkdir -p frontend
	@docker run --rm $(USER_FLAG) -v "$(WORKDIR):/workspace" -w /workspace $(IMG_NODE) sh -c '\
		if [ ! -f frontend/package.json ]; then \
			npm create vite@latest frontend -- --template react-ts --yes; \
		else \
			echo "  frontend/package.json existe déjà, skip création Vite"; \
		fi && \
		cd frontend && \
		npm install @tanstack/react-router @tanstack/react-query zustand react-hook-form zod react-markdown rehype-sanitize lucide-react cmdk next-themes tailwindcss@latest @tailwindcss/vite@latest class-variance-authority clsx tailwind-merge && \
		npm install -D openapi-typescript vitest @testing-library/react @testing-library/jest-dom jsdom eslint-plugin-jsx-a11y eslint-plugin-boundaries @vitejs/plugin-react \
	'
	@echo "✅ Frontend scaffold terminé"

.PHONY: init-backend
init-backend: ## Scaffolde backend/ via python:3.14 (FastAPI + uv + deps)
	@echo "🐍 Scaffolding backend via $(IMG_PYTHON)..."
	@mkdir -p backend
	@docker run --rm $(USER_FLAG) -v "$(WORKDIR):/workspace" -w /workspace $(IMG_PYTHON) sh -c '\
		pip install --quiet --no-cache-dir --root-user-action=ignore uv && \
		cd backend && \
		if [ ! -f pyproject.toml ]; then \
			uv init --package --name agentive-backend --no-readme --no-pin-python; \
		else \
			echo "  backend/pyproject.toml existe déjà, skip uv init"; \
		fi && \
		uv add --no-sync fastapi "uvicorn[standard]" "sqlalchemy[asyncio]" alembic pgvector pydantic pydantic-settings langgraph langchain-anthropic langchain-openai mcp python-multipart sse-starlette structlog cryptography slowapi apscheduler "psycopg[binary]>=3.2" && \
		uv add --no-sync --dev pytest pytest-asyncio pytest-cov ruff mypy "langgraph-cli[inmem]" import-linter polyfactory testcontainers "cyclonedx-bom>=5" && \
		uv sync \
	'
	@echo "✅ Backend scaffold terminé"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DEV STACK (docker compose)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

.PHONY: up
up: _check-env ## Démarre le stack dev (db + backend + frontend + caddy) en arrière-plan
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
	@docker run --rm $(USER_FLAG) -v "$(WORKDIR)/backend:/app" -w /app $(IMG_PYTHON) sh -c "\
		pip install --quiet --no-cache-dir --root-user-action=ignore uv && uv sync \
	"

.PHONY: migrate
migrate: ## Applique les migrations Alembic (alembic upgrade head)
	$(DC_DEV) run --rm backend uv run alembic upgrade head

.PHONY: migrate-new
migrate-new: _validate-msg ## Crée une nouvelle migration (usage: make migrate-new MSG="description")
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
	@docker run --rm $(USER_FLAG) -v "$(WORKDIR)/frontend:/app" -w /app $(IMG_NODE) npm install

.PHONY: test-frontend
test-frontend: ## Exécute les tests Vitest
	$(DC_DEV) run --rm frontend npm run test -- --run

.PHONY: lint-frontend
lint-frontend: ## Lint frontend (eslint + tsc noEmit)
	$(DC_DEV) run --rm frontend sh -c "npm run lint && npx tsc --noEmit"

.PHONY: gen-api-types
gen-api-types: up ## Génère les types TS depuis le schéma OpenAPI du backend
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
db-reset: ## DROP DATABASE + recreate via recreate du volume (DANGER : perte données)
	@echo "⚠️  DANGER : db-reset supprime TOUTES les données."
	@echo "  Cette commande détruit le volume Postgres, supprimant DB + rôles, puis"
	@echo "  recrée tout via init.sql + migrations."
	@read -rp "  Confirmer avec 'yes' : " confirm; test "$$confirm" = "yes" || (echo "abandon" && exit 1)
	$(DC_DEV) stop db
	$(DC_DEV) rm -f db
	docker volume rm -f agentive_agentive_db_data
	$(DC_DEV) up -d db
	@echo "⏳ Attente DB healthy (30s)..."
	@sleep 30
	$(MAKE) migrate
	@echo "✅ DB réinitialisée (rôles + schema)"

.PHONY: db-backup
db-backup: ## pg_dump robuste vers ./backups/agentive-YYYYMMDD-HHMMSS.sql
	@mkdir -p backups
	@timestamp=$$(date +%Y%m%d-%H%M%S); \
	 tmpfile="backups/agentive-$${timestamp}.sql.tmp"; \
	 finalfile="backups/agentive-$${timestamp}.sql"; \
	 set -e; \
	 echo "🗄️  pg_dump → $$finalfile"; \
	 $(DC_DEV) exec -T db pg_dump -U agentive_owner -d agentive --no-owner --no-acl --clean --if-exists > "$$tmpfile" \
	   || { rm -f "$$tmpfile"; echo "❌ pg_dump échoué"; exit 1; }; \
	 if [ ! -s "$$tmpfile" ]; then \
	   rm -f "$$tmpfile"; \
	   echo "❌ backup vide — DB accessible ?"; \
	   exit 1; \
	 fi; \
	 mv "$$tmpfile" "$$finalfile"; \
	 echo "✅ Backup OK : $$finalfile ($$(wc -c < "$$finalfile") bytes)"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PRE-COMMIT + SECURITY
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

.PHONY: precommit-install
precommit-install: ## Installe les git hooks pre-commit (via image Docker pre-commit)
	@set -e; \
	 if [ ! -f "$(WORKDIR)/infra/scripts/pre-commit.sh" ]; then \
	   echo "❌ infra/scripts/pre-commit.sh absent — impossible d'installer le hook"; \
	   exit 1; \
	 fi; \
	 mkdir -p .git/hooks; \
	 cp infra/scripts/pre-commit.sh .git/hooks/pre-commit; \
	 chmod +x .git/hooks/pre-commit; \
	 echo "✅ pre-commit hook installé ($(WORKDIR)/.git/hooks/pre-commit)"

.PHONY: precommit-run
precommit-run: ## Exécute manuellement tous les hooks pre-commit sur tous les fichiers
	@docker run --rm $(USER_FLAG) -v "$(WORKDIR):/src" $(IMG_GITLEAKS) detect --source="/src" --no-banner -v

.PHONY: gitleaks
gitleaks: ## Scan gitleaks sur le repo
	docker run --rm $(USER_FLAG) -v "$(WORKDIR):/src" $(IMG_GITLEAKS) detect --source="/src" --no-banner -v

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
# STAGING (observe-only wrappers — le deploy réel passe par GitHub Actions)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Le workflow .github/workflows/deploy-staging.yml est la seule source de vérité
# pour déployer. Ces cibles servent uniquement à consulter l'état staging depuis
# une machine locale. Nécessite DEPLOY_USER + DEPLOY_HOST dans l'env shell.

STAGING_COMPOSE := docker compose -f docker-compose.yml -f docker-compose.staging.yml -p agentive-staging
STAGING_REMOTE := /opt/app/agentive-staging

.PHONY: staging-logs
staging-logs: ## Stream des logs staging via SSH (tail 100)
	@test -n "$(DEPLOY_USER)" -a -n "$(DEPLOY_HOST)" || { echo "❌ DEPLOY_USER et DEPLOY_HOST requis"; exit 1; }
	ssh $(DEPLOY_USER)@$(DEPLOY_HOST) 'cd $(STAGING_REMOTE) && $(STAGING_COMPOSE) logs -f --tail=100'

.PHONY: staging-ps
staging-ps: ## Statut des services staging via SSH
	@test -n "$(DEPLOY_USER)" -a -n "$(DEPLOY_HOST)" || { echo "❌ DEPLOY_USER et DEPLOY_HOST requis"; exit 1; }
	ssh $(DEPLOY_USER)@$(DEPLOY_HOST) 'cd $(STAGING_REMOTE) && $(STAGING_COMPOSE) ps'

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MISC
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

.PHONY: clean
clean: ## Supprime caches dans backend/ et frontend/ (blast radius restreint)
	@rm -rf \
		"$(WORKDIR)/backend/.venv" \
		"$(WORKDIR)/backend/.pytest_cache" \
		"$(WORKDIR)/backend/.mypy_cache" \
		"$(WORKDIR)/backend/.ruff_cache" \
		"$(WORKDIR)/backend/build" \
		"$(WORKDIR)/backend/dist" \
		"$(WORKDIR)/frontend/node_modules" \
		"$(WORKDIR)/frontend/dist" \
		"$(WORKDIR)/frontend/.vite"
	@find "$(WORKDIR)/backend" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
	@echo "✅ Caches nettoyés (backend/.venv, frontend/node_modules, etc.)"

.PHONY: pull
pull: ## Pull toutes les images Docker utilisées par le stack
	$(DC_DEV) pull
