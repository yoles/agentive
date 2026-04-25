# Agentive

**Company Builder pour agents IA** — application web self-hosted qui permet à un utilisateur unique de concevoir, déployer et piloter des départements IA composés d'agents spécialisés.

> **Sprint 0 — Scaffolding minimal viable.** Cette version pose la fondation technique. Les features métier (agents, workflows, mémoire, chat, etc.) arrivent dans les sprints suivants.

## 🚀 Quick Start

**Prérequis machine** : uniquement `docker` + `make` installés. **Aucun runtime local n'est nécessaire** (Python, Node, uv, etc. tournent dans des containers).

```bash
git clone <repo-url> agentive && cd agentive
make init          # Scaffolde frontend + backend via containers éphémères (one-time)
make up            # Démarre le stack dev (db + backend + frontend + caddy)
open https://localhost:8443   # UI (certificat self-signed en dev)
```

### Ports dev

| Service | Port hôte | Détail |
|---|---|---|
| Caddy HTTPS (dev) | **8443** | Reverse proxy self-signed |
| Caddy HTTP (dev) | **8080** | Redirect vers 8443 |
| Frontend (direct) | 5173 | Vite dev server |
| Backend (direct) | 8000 | FastAPI + Uvicorn |
| Postgres | 5432 | DB (connection locale) |

> 🔌 Les ports standards 80/443 sont **réservés à la prod** (où Caddy fait du Let's Encrypt auto-HTTPS). En dev, on utilise 8080/8443 pour éviter les conflits avec des services existants (traefik, etc.).

## 🏗️ Architecture

- **Backend** : Python 3.14 / FastAPI async / SQLAlchemy 2.0 / Alembic / LangGraph (wrapper M3) / pgvector
- **Frontend** : React 19 / TypeScript / Vite / shadcn/ui v4 / Tailwind v4 / TanStack Router + Query / Zustand
- **Database** : PostgreSQL 17 + pgvector (image officielle `pgvector/pgvector:pg17`)
- **Reverse proxy** : Caddy 2 (auto-HTTPS en prod, self-signed en dev)
- **Self-hosted local-first** — tout en Docker Compose, single-node

Détail complet : voir [`_bmad-output/planning-artifacts/architecture.md`](./_bmad-output/planning-artifacts/architecture.md).

## 🐳 Docker-first : principe fondamental

> **Aucun runtime ne doit être installé sur l'hôte.** Toute action (init, build, test, lint, migrations, génération de code) passe via Docker.

- Un conteneur par techno (Python 3.14, Node 24, Postgres 17, Caddy 2)
- Le `Makefile` orchestre toutes les commandes via `docker run` ou `docker compose run`
- Même l'initialisation (shadcn, Vite, uv init, npm install) s'exécute dans des containers éphémères — **zéro trace sur l'hôte**

### Commandes Makefile essentielles

```bash
make help           # Liste toutes les cibles
make init           # Scaffolde frontend + backend (one-time)
make up             # Démarre le stack dev
make down           # Arrête le stack
make logs           # Stream des logs
make migrate        # Applique les migrations Alembic
make test           # Tests backend + frontend
make lint           # Lint backend + frontend
make db-shell       # Ouvre psql
make gitleaks       # Scan secrets sur le repo
make bench          # Benchmark M4 pgvector (Story 1.3)
make spike-m3       # Spike M3 LangGraph (Story 1.2)
```

## 🗺️ Structure du repo

```
agentive/
├── Makefile                  # Orchestrateur Docker (ENTRY POINT)
├── docker-compose.yml        # Stack dev (db, backend, frontend, caddy)
├── docker-compose.prod.yml   # Overrides prod
├── .pre-commit-config.yaml   # Hooks via Docker (gitleaks, ruff, eslint)
├── .gitleaks.toml
├── .sops.yaml                # Chiffrement .env
├── backend/                  # Python : FastAPI + features M1-M12
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── alembic/              # Migrations
│   └── src/
│       ├── app/              # Bootstrap FastAPI
│       ├── features/         # M1-M12 (isolées, communication via event bus)
│       ├── shared/           # Transverse (config, repositories, event_bus, llm, auth...)
│       └── infra/            # Adapters (DB, LLM, MCP)
├── frontend/                 # React SPA : 4 espaces UI
│   ├── Dockerfile
│   ├── package.json
│   └── src/
│       ├── app/              # Bootstrap + routes file-based (dashboard, chat, trace, config)
│       ├── features/         # Espaces métier (dashboard, chat, trace, config, playground...)
│       └── shared/           # Composants UI (shadcn), layouts, hooks, api client
├── infra/
│   ├── caddy/                # Reverse proxy
│   ├── postgres/             # init.sql (3 rôles), postgresql.conf
│   └── scripts/              # backup, deploy, rotate_secrets, pre-commit
├── docs/                     # ADR, runbooks, compliance
├── _bmad/                    # Framework BMAD (planning)
└── _bmad-output/             # Artefacts de planification + implémentation
    ├── planning-artifacts/   # PRD, Architecture, UX Spec, Epics
    └── implementation-artifacts/  # Stories, sprint-status.yaml
```

## 🔐 Sécurité baseline (Sprint 0)

- 3 rôles Postgres (`agentive_app`, `agentive_audit_admin`, `agentive_owner`) avec principe du moindre privilège
- RLS (Row Level Security) active dès Sprint 0 sur `memory_chunks`, `workflows`, `audit_events`, `agent_instances`, `chunk_embeddings`
- Audit trail `audit_events` partitionné par mois + `REVOKE DELETE/UPDATE` (immutabilité)
- Index HNSW pré-calibré sur `chunk_embeddings` (m=16, ef_construction=64 — ajusté Story 1.3)
- `gitleaks` pre-commit + GitHub secret scanning
- Secrets chiffrés via SOPS + age (`.env.encrypted` committé)
- Caddy security headers (HSTS, CSP avec nonces, X-Frame-Options DENY)
- CORS whitelist explicite (pas de `*`)
- Auth token statique via `AGENTIVE_API_TOKEN` (middleware FastAPI) — évolue vers session cookies en Growth

## 📚 Documentation

- Planning : `_bmad-output/planning-artifacts/`
  - [PRD](./_bmad-output/planning-artifacts/prd.md)
  - [Architecture](./_bmad-output/planning-artifacts/architecture.md)
  - [UX Design Spec](./_bmad-output/planning-artifacts/ux-design-specification.md)
  - [Epics & Stories](./_bmad-output/planning-artifacts/epics.md)
  - [Implementation Readiness Report](./_bmad-output/planning-artifacts/implementation-readiness-report-2026-04-19.md)
- Implementation : `_bmad-output/implementation-artifacts/`
  - [Sprint status](./_bmad-output/implementation-artifacts/sprint-status.yaml)
  - Stories détaillées (1.1, 1.2, 1.3, ...)
- ADR : `docs/decisions/`
- Runbooks : `docs/runbooks/`

## 🔑 Configuration

Copier `.env.example` vers `.env.dev` (ou `.env`), renseigner les variables :

```bash
cp .env.example .env.dev
```

Les variables critiques :
- `AGENTIVE_API_TOKEN` — token auth MVP (random 32 bytes base64)
- `AGENTIVE_ENCRYPTION_KEY` — clé Fernet pour chiffrement at-rest
- `POSTGRES_APP_PASSWORD`, `POSTGRES_AUDIT_ADMIN_PASSWORD`, `POSTGRES_OWNER_PASSWORD` — mots de passe des 3 rôles PG
- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` — providers LLM (gérés in-app à terme via Epic 9 Story 9.3)

Pour prod, chiffrer `.env` avec SOPS + age (voir `docs/runbooks/rotate-secrets.md`).

## 🧪 Tests & CI

- Tests backend : `make test-backend` (pytest + pytest-asyncio + testcontainers)
- Tests frontend : `make test-frontend` (vitest + @testing-library/react)
- Lint : `make lint` (ruff + eslint + jsx-a11y + tsc --noEmit)
- CI : GitHub Actions (`.github/workflows/ci.yml`) — tout via Docker

## 🚢 Déploiement staging

Staging tourne sur `https://staging.agentive.idem-agency.fr`, derrière **Traefik**
déployé indépendamment depuis le repo `infrastructure_idem_helper/` (reverse
proxy + SSL Let's Encrypt auto).

- **Trigger** : push sur `main` ou manuel (`workflow_dispatch`) → `.github/workflows/deploy-staging.yml`
- **Mécanique** : `git archive HEAD` → SSH → `docker compose -f docker-compose.yml -f docker-compose.staging.yml up -d` + `alembic upgrade head` + smoke tests
- **Observation locale** : `make staging-logs` / `make staging-ps` (requiert `DEPLOY_USER` + `DEPLOY_HOST` dans l'env)
- **Rollback** : `git revert <sha> && git push` (re-déclenche le workflow)

### Prérequis (hors repo, one-time)

1. DNS A-record `staging.agentive.idem-agency.fr` → IP du serveur idem **(avant le premier deploy, sinon Let's Encrypt HTTP-01 échoue)**.
2. Sur le serveur : Traefik + `traefik_network` déployés via `infrastructure_idem_helper/`, user `deploy` dans le groupe `docker`, `mkdir -p /opt/app/agentive-staging && chown deploy:deploy /opt/app/agentive-staging`.
3. GitHub repo → Settings → Environments → créer `staging` et ajouter les secrets :
   - `SSH_PRIVATE_KEY` — clé privée OpenSSH raw (pas de base64)
   - `DEPLOY_HOST` — hostname ou IP du serveur idem
   - `DEPLOY_USER` — user SSH (ex: `deploy`)
   - `STAGING_ENV_FILE` — contenu multi-ligne complet du `.env.staging` (voir `.env.staging.example` pour le template)

## 📦 License

TBD (MIT envisagé pour le MVP)
