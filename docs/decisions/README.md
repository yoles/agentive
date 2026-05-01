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
