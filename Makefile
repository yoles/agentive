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

.PHONY: dev-host
dev-host: _check-env ## Démarre le stack dev en exposant le frontend sur le LAN (0.0.0.0:5173)
	@docker compose -f docker-compose.yml -f docker-compose.dev-host.yml up -d
	@echo ""
	@echo "✅ Stack dev-host démarré (frontend exposé sur le réseau local)"
	@echo ""
	@echo "  Accès local      : http://localhost:5173"
	@PRIMARY_IP=$$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($$i=="src") {print $$(i+1); exit}}'); \
	 if [ -n "$$PRIMARY_IP" ]; then \
		echo "  Accès LAN        : http://$$PRIMARY_IP:5173"; \
	 else \
		echo "  Accès LAN        : (IP non détectée — exécuter 'ip addr' ou 'hostname -I')"; \
	 fi
	@echo ""
	@echo "  Notes :"
	@echo "    - /api/* est proxifié par Vite vers backend:8000 (réseau Docker interne)."
	@echo "    - Caddy n'est PAS utilisé dans ce mode (binding localhost only)."
	@echo "    - À utiliser sur un réseau de confiance uniquement (HTTP en clair)."
	@echo "    - Stop : make down"

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

# ━━━ Bench M4 pgvector HNSW (Story 1.3 — gating critique #2) ━━━
# Pipeline : (1) make bench → baseline + ground truth (~1 min)
#            (2) make bench-hnsw-fast → sweep réduit 8 combos (~10 min CI)
#            (3) make bench-hnsw → sweep complet 27 combos (~25 min)
#            (4) make bench-ivfflat → comparaison ivfflat (~10 min)
#            (5) make bench-report → agrégation CSV + génération hnsw-tuning.md
# Output : _bench_artifacts/*.csv + docs/decisions/hnsw-tuning.md

.PHONY: bench
bench: up ## Bench baseline (10k chunks + ground truth + mesure baseline) — Story 1.3
	$(DC_DEV) run --rm backend uv run python -m scripts.benchmark_m4

.PHONY: bench-hnsw
bench-hnsw: up ## Sweep HNSW complet (27 combos m × ef_construction × ef_search) — ~25 min
	$(DC_DEV) run --rm backend uv run python -m scripts.benchmark_hnsw

.PHONY: bench-hnsw-fast
bench-hnsw-fast: up ## Sweep HNSW réduit (8 combos) — ~10 min, pour CI
	$(DC_DEV) run --rm -e BENCH_FAST=1 backend uv run python -m scripts.benchmark_hnsw

.PHONY: bench-ivfflat
bench-ivfflat: up ## Comparaison ivfflat (9 combos lists × probes) — ~10 min
	$(DC_DEV) run --rm backend uv run python -m scripts.benchmark_ivfflat

.PHONY: bench-aggregate
bench-aggregate: ## Agrège les CSV bench HNSW+ivfflat → comparison_hnsw_vs_ivfflat.csv (P8 review)
	$(DC_DEV) run --rm backend uv run python -m scripts._bench_aggregate

.PHONY: bench-report
bench-report: bench-aggregate ## Agrège + génère docs/decisions/hnsw-tuning.md
	$(DC_DEV) run --rm backend uv run python -m scripts._bench_report
	@mkdir -p docs/decisions
	@cp backend/_bench_artifacts/hnsw-tuning.md docs/decisions/hnsw-tuning.md
	@echo "✅ Report copied to docs/decisions/hnsw-tuning.md"

# ━━━ Spike M3 LangGraph (Story 1.2 — gating critique #1) ━━━
# Le spike valide 3 piliers sur LangGraph 1.1.8 : checkpointing Postgres natif,
# scatter-gather, human-in-the-loop. Code isolé dans backend/spike/ — sera réécrit
# proprement dans features/m3_workflow_engine/ à l'Epic 4 selon le verdict de
# docs/decisions/m3-spike-result.md.

.PHONY: spike-m3
spike-m3: up ## Spike M3 — workflow basique (Producer → QualityGate → Reviewer), MockLLM auto si pas de clé Anthropic
	$(DC_DEV) run --rm backend uv run python -m spike.m3_langgraph

.PHONY: spike-m3-mock
spike-m3-mock: up ## Spike M3 — force MockLLM (ignore ANTHROPIC_API_KEY)
	$(DC_DEV) run --rm -e ANTHROPIC_API_KEY="" backend uv run python -m spike.m3_langgraph

.PHONY: spike-m3-real
spike-m3-real: up ## Spike M3 — force Anthropic réel (échoue si ANTHROPIC_API_KEY absent)
	@if [ -z "$$ANTHROPIC_API_KEY" ]; then \
		echo "❌ ANTHROPIC_API_KEY absent — exporter la clé dans le shell avant cette cible"; \
		echo "   (sinon utiliser make spike-m3-mock pour forcer MockLLM)"; \
		exit 1; \
	fi
	$(DC_DEV) run --rm -e ANTHROPIC_API_KEY="$$ANTHROPIC_API_KEY" backend uv run python -m spike.m3_langgraph

.PHONY: spike-m3-crash
spike-m3-crash: up ## Spike M3 — crash post-producer (kill -9), persiste thread_id pour resume
	$(DC_DEV) run --rm -e CRASH_AFTER=producer backend uv run python -m spike.m3_langgraph || true
	@if [ -f "$(WORKDIR)/backend/.spike-thread-id" ]; then \
		echo "✅ thread_id persisté : $$(cat $(WORKDIR)/backend/.spike-thread-id)"; \
		echo "   → relance via : make spike-m3-resume"; \
	else \
		echo "❌ pas de .spike-thread-id — le crash n'a peut-être pas eu lieu"; exit 1; \
	fi

.PHONY: spike-m3-resume
spike-m3-resume: up ## Spike M3 — reprise sur le thread_id écrit par spike-m3-crash
	@test -f "$(WORKDIR)/backend/.spike-thread-id" || { echo "❌ backend/.spike-thread-id absent — exécuter make spike-m3-crash d'abord"; exit 1; }
	@TID="$$(cat $(WORKDIR)/backend/.spike-thread-id)"; \
	 echo "▶️  Resume thread_id=$$TID"; \
	 $(DC_DEV) run --rm -e SPIKE_RESUME_THREAD_ID=$$TID backend uv run python -m spike.m3_langgraph

.PHONY: spike-m3-scatter
spike-m3-scatter: up ## Spike M3 — scatter-gather (3 summarizers en parallèle via Send)
	$(DC_DEV) run --rm backend uv run python -m spike.m3_scatter_gather

.PHONY: spike-m3-inspect
spike-m3-inspect: up ## Spike M3 — inspecte le checkpoint Postgres pour un thread_id (THREAD_ID=<uuid>)
	@test -n "$(THREAD_ID)" || { echo "❌ THREAD_ID requis — usage: make spike-m3-inspect THREAD_ID=<uuid>"; exit 1; }
	$(DC_DEV) run --rm backend uv run python -m spike.inspect_checkpoint "$(THREAD_ID)"

.PHONY: spike-m3-test
spike-m3-test: up ## Spike M3 — exécute uniquement les tests pytest tests/spike/
	$(DC_DEV) run --rm backend uv run pytest tests/spike/ -v

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
