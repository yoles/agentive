# Conventions de développement Agentive

> **Ce fichier est un résumé.** La source canonique complète des conventions est [`_bmad-output/planning-artifacts/architecture.md`](./_bmad-output/planning-artifacts/architecture.md) (sections "Implementation Patterns & Consistency Rules" et "AI Agent Guidelines").

## Les 10 règles d'or (architecture.md lignes 2200-2215)

1. **Suivre toutes les décisions architecturales documentées** — ne pas en dévier sans ouvrir un ADR dans `docs/decisions/`.
2. **Conventions de nommage** : `snake_case` (Python / DB / JSON), `camelCase` (TypeScript), `PascalCase` (classes / components / types).
3. **Communication inter-features** uniquement via bus d'événements (`shared.event_bus`) ou contrats partagés (`shared.contracts`). Jamais d'import direct d'un module feature vers un autre.
4. **Accès DB uniquement** via `shared.repositories.*` — jamais d'`AsyncSession`/`asyncpg` directs dans le code applicatif (acceptable uniquement dans `infra/db/`).
5. **Configuration uniquement** via `shared.config.settings` — jamais `os.environ` direct.
6. **Toute nouvelle feature** suit la structure type :
   - Frontend : `components/ + hooks/ + services/ + store/ + types/ + utils/ + index.ts` (barrel public API)
   - Backend : `service.py + schemas.py + events.py + tests/ + __init__.py` (barrel public API)
7. **Imports via barrel** : `from features.m3_workflow_engine import WorkflowEngine` ✅, jamais d'imports profonds (`from features.m3_workflow_engine.engine.internal import ...` ❌).
8. **Tous les inputs externes** (user input, tool output) wrappés dans `<user_input>...</user_input>` ou `<tool_output>...</tool_output>` avant tout appel LLM (défense contre prompt injection).
9. **Correlation ID** (UUID v7 / ULID) propagé dans tous les logs et events.
10. **Tenant ID** présent dans tous les nouveaux endpoints, queries, logs, métriques (NULL acceptable MVP, prêt pour Growth multi-tenant).

## Docker-first

- **Aucun runtime sur l'hôte** : Python, Node, uv, etc. tournent exclusivement dans des containers.
- **Makefile racine** = point d'entrée pour toute commande dev (`make dev`, `make test`, `make lint`, `make migrate`, ...).
- **Init des projets via containers éphémères** : `docker run --rm -v $(PWD):/workspace -w /workspace <image>` pour `npm create vite`, `uv init`, etc.
- **Versions latest stable** pinnées via tags Docker (`python:3.14-slim`, `node:24-alpine`, `pgvector/pgvector:pg17`, `caddy:2-alpine`).

## Structure des dossiers (résumé)

```
backend/src/
├── app/          # Bootstrap FastAPI (main, lifespan, middleware setup)
├── features/     # M1-M12 (isolées, communication via event bus uniquement)
├── shared/       # Transverse (config, repositories, event_bus, llm, auth, logging, metrics, contracts, exceptions)
└── infra/        # Adapters (db/session.py, db/models.py, llm/*_adapter.py, mcp/client.py, mcp/sandbox.py)

frontend/src/
├── app/          # Bootstrap + routes file-based (4 espaces : dashboard, chat, trace, config)
├── features/     # Espaces métier (dashboard, chat, trace, config, playground, command-palette, theme, auth)
└── shared/       # Composants UI (shadcn primitives + layouts) + hooks + api + lib + types
```

## Versioning critique

- **`langgraph` est pinné strictement** dans `backend/pyproject.toml` (`==1.1.8`, pas `>=` ni `~=`). Toute upgrade majeure (1.x → 2.x) ou tout breaking change documenté dans le CHANGELOG LangGraph **DOIT** déclencher la re-exécution du spike Story 1.2 (`make spike-m3 && docker compose run --rm backend uv run pytest tests/spike/`) et la mise à jour de [`docs/decisions/m3-spike-result.md`](./docs/decisions/m3-spike-result.md) **avant** le merge. Référence : Architecture G3 (lignes 2068-2072), Story 1.2 AC7.
- Critères de re-validation : checkpointing Postgres, scatter-gather (`Send` + reducer `operator.add`), human-in-the-loop (`interrupt` / `Command(resume=...)`) restent fonctionnels.
- **`pgvector` (image Docker `pgvector/pgvector:pg17`) — paramètres HNSW gating NFR4/NFR5**. Toute upgrade pgvector ≥ 0.5 OU tout changement des paramètres HNSW (`m`, `ef_construction`, `ef_search`) dans `backend/alembic/versions/*` **DOIT** déclencher `make bench && make bench-hnsw-fast && make bench-report` et la mise à jour de [`docs/decisions/hnsw-tuning.md`](./docs/decisions/hnsw-tuning.md) **avant** le merge. Référence : Story 1.3 AC8, Architecture lignes 570-573 + 818-847.
- Critères de re-validation : recall@5 > 0.90 (NFR4), p95 latency < 200ms end-to-end avec reranking (NFR5), pas de régression > 10% vs baseline du précédent rapport.
- **`psycopg[binary]` est pinné `>=3.2.10`** dans `backend/pyproject.toml`. Versions 3.2.4 → 3.2.9 ont un memory leak dans `AsyncConnection.notifies()` quand le générateur n'est pas régulièrement consommé (cf [psycopg #962](https://github.com/psycopg/psycopg/issues/962), corrigé en 3.2.10). Le `OutboxWorker` dépend directement de cette API. Toute upgrade ≥ 3.3 **DOIT** être validée sous charge synthétique (script publiant 1000 events/s pendant 5 min, vérifier que le RSS du process backend ne croît pas linéairement) **avant** le merge. Référence : Story 1.4, `shared/event_bus/outbox.py`.

## Enforcement

- `.import-linter` (backend) : configure les boundaries entre `shared/`, `features/`, `infra/`. CI bloque les violations.
- `eslint-plugin-boundaries` (frontend) : équivalent pour React/TS.
- `pre-commit` : `gitleaks` + `ruff` + `eslint` bloquent les commits non-conformes.
