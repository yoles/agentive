# Architecture Decision Records

Cf [`_bmad-output/planning-artifacts/architecture.md`](../../_bmad-output/planning-artifacts/architecture.md) section "ADR-001 — Stratégie d'Initialisation du Projet" pour le format de référence.

Structure attendue : `NNN-short-slug.md` (ex: `001-starter-template.md`, `002-event-bus-postgres.md`).

## ADR existants

### Sprint 0 — gating critiques

- [`m3-spike-result.md`](./m3-spike-result.md) — **Story 1.2 (gating critique #1)** : verdict GO LangGraph 1.1.8 (checkpointing Postgres, scatter-gather, human-in-the-loop) ⇒ Sprint 1 débloqué.
- [`m4-bench-result.md`](./m4-bench-result.md) — **Story 1.3 (gating critique #2)** : verdict GO/NO-GO pgvector HNSW (recall@5 + p95 latency vs NFR4/NFR5) ⇒ Stories Epic 3 débloquées.
- [`hnsw-tuning.md`](./hnsw-tuning.md) — **Rapport auto-généré** par `make bench-report` : tuning paramétrique HNSW (`m`, `ef_construction`, `ef_search`), comparaison avec ivfflat, configuration retenue.

### Sprint 0 — fondations Core

- [`event-bus-naming.md`](./event-bus-naming.md) — **Story 1.4** : convention `module.entity.action` enforce par `validate_event_type` ; warn-on-typo sur préfixes inconnus ; pourquoi 3 segments.
- [`event-bus-migration-trigger.md`](./event-bus-migration-trigger.md) — **Story 1.4** : déclencheurs objectifs (latence p95 > 100ms 24h, backlog > 1000, multi-instance) + plan de migration en 3 phases vers Redis Streams.
- [`repository-pattern.md`](./repository-pattern.md) — **Story 1.5** : Repository Pattern + RLS tenant binding via `BaseRepo.with_tenant()` (`set_config('app.tenant_id', :tid, true)`) ; rationale vs Active Record / DAO ; defense-in-depth avec `FORCE ROW LEVEL SECURITY` + `import-linter` Contract 3.
- [`llm-abstraction.md`](./llm-abstraction.md) — **Story 1.6** : `LLMProvider` Protocol + `LLMRouter` multi-provider avec fallback (NFR12, NFR20) wrappant `langchain-anthropic` + `langchain-openai` ; rationale vs LiteLLM / SDK direct ; escape hatch `raw_provider_call()` ; redaction NFR9 + `import-linter` Contract 5.
- [`llm-fallback-policy.md`](./llm-fallback-policy.md) — **Story 1.6** : table de classification des erreurs (retriable_with_fallback / fatal), chaîne canonique Sprint 0 `["anthropic", "openai"]`, `MODEL_FALLBACK_MAP`, policy de mise à jour pricing.

### Sprint 1 — refactoring

- [`module-naming.md`](./module-naming.md) — **Revue DDD 2026-07** : suppression des préfixes de planning `mN` des feature modules et du namespace d'events (préfixe event = nom du package) ; table de correspondance PRD ↔ code ; backfill `outbox_events` ; vigilance frontière `agent_configurator`/`agent_registry`.

### Sprint 2 — moteur de workflows

- [`dev-pole-code-search-server.md`](./dev-pole-code-search-server.md) — **Story 5.2** : pourquoi le serveur MCP de lecture de code est **interne** et non tiers (l'image backend est `python:3.14-slim` + bubblewrap + curl : ni Node, ni `npx`, ni `ripgrep`), et pourquoi sa politique d'accès vit dans `Settings` (`AGENTIVE_DEV_CODE_ROOTS`) plutôt que dans la colonne que **D62** décrivait — une allowlist qui décide de ce qu'un agent LLM peut lire doit être lisible dans un diff git, pas modifiable par un `UPDATE`. Ferme la substance de D62 pour les serveurs internes ; amende l'ADR de la Story 5.1.
- [`dev-pole-workflow-tooling.md`](./dev-pole-workflow-tooling.md) — **Story 5.1** : pourquoi le Dev Lead reste sans outil en Sprint 2 (`tools: []`) et pourquoi aucun serveur MCP interne n'est écrit ; stdio impossible (`unshare_net`), SSE possible mais rouvre **D63** — arbitrage de sécurité qui mérite sa propre story plutôt qu'un effet de bord ; le *mécanisme* d'assignation est livré et bloquant, la Story 5.2 l'exerce avec la première liste non vide du dépôt. ⚠️ Sa phrase « les serveurs de la 5.2 sont tiers » est AMENDÉE par la Story 5.2 : l'image backend n'a ni Node ni ripgrep.
- [`dev-lead-prompt-quality-protocol.md`](./dev-lead-prompt-quality-protocol.md) — **Story 5.1** : la qualité de la décomposition ne se prouve pas par la suite de tests (`MockProvider` prouve le contrat, pas le jugement) ; protocole manuel avec vraies clés au runbook § 7, table de résultat versionnée — **vide aujourd'hui**, ce qui est l'affirmation visible qu'il n'a jamais tourné.
- [`workflow-creation-idempotency.md`](./workflow-creation-idempotency.md) — **Story 4.8** : idempotence de `POST /api/v1/workflows` par empreinte de contenu SHA-256 sous index unique **partiel** `NULLS NOT DISTINCT` ; pourquoi ni `Idempotency-Key` ni pré-`SELECT` ; `200` + `idempotent_replay` sur rejeu ; conséquences assumées (unicité `(name, dag)` permanente, empreinte sur la forme soumise, rows antérieures hors idempotence).
