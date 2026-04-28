# Architecture Decision Records

Cf [`_bmad-output/planning-artifacts/architecture.md`](../../_bmad-output/planning-artifacts/architecture.md) section "ADR-001 — Stratégie d'Initialisation du Projet" pour le format de référence.

Structure attendue : `NNN-short-slug.md` (ex: `001-starter-template.md`, `002-event-bus-postgres.md`).

## ADR existants

### Sprint 0 — gating critiques

- [`m3-spike-result.md`](./m3-spike-result.md) — **Story 1.2 (gating critique #1)** : verdict GO LangGraph 1.1.8 (checkpointing Postgres, scatter-gather, human-in-the-loop) ⇒ Sprint 1 débloqué.
- [`m4-bench-result.md`](./m4-bench-result.md) — **Story 1.3 (gating critique #2)** : verdict GO/NO-GO pgvector HNSW (recall@5 + p95 latency vs NFR4/NFR5) ⇒ Stories Epic 3 débloquées.
- [`hnsw-tuning.md`](./hnsw-tuning.md) — **Rapport auto-généré** par `make bench-report` : tuning paramétrique HNSW (`m`, `ef_construction`, `ef_search`), comparaison avec ivfflat, configuration retenue.
