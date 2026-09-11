# Runbooks

Runbooks opérationnels pour Agentive — procédures concrètes pour des situations récurrentes.

## Disponibles

- [`dry-run-predictif.md`](./dry-run-predictif.md) — Dry Run predictif : probable_path, estimation coût/tokens, budget cap (Story 4.4)
- [`event-bus-debug.md`](./event-bus-debug.md) — Debug du bus d'événements LISTEN/NOTIFY (Story 1.4)
- [`hybrid-orchestration.md`](./hybrid-orchestration.md) — Orchestration hybride DSL → règles → LLM, seuil, `routing-stats` (Story 4.3)
- [`llm-usage.md`](./llm-usage.md) — Utilisation `LLMRouter` (Story 1.6)
- [`m3-checkpoint-inspect.md`](./m3-checkpoint-inspect.md) — Inspection checkpoints LangGraph (Story 1.2)
- [`m4-bench-rerun.md`](./m4-bench-rerun.md) — Re-run bench M4 pgvector HNSW (Story 1.3)
- [`observability.md`](./observability.md) — Guide pratique observabilité : logs JSON, RFC 7807, correlation_id, redaction (Story 1.9)
- [`repositories-usage.md`](./repositories-usage.md) — Utilisation Repository Pattern + RLS (Story 1.5)

## À implémenter (progressivement)

- `restore-backup.md` — Procédure restore `pg_dump` depuis `backups/` (Sprint 1+)
- `rotate-token.md` — Rotation `AGENTIVE_API_TOKEN` (Story 1.7)
- `rotate-secrets.md` — Rotation clé SOPS + age + re-chiffrement `.env.encrypted` (Story 9.6)
- `pivot-llm-provider.md` — Bascule Anthropic ↔ OpenAI (Story 1.6)
- `upgrade-pgvector.md` — Upgrade pgvector extension (Sprint 2+)

## Format

Chaque runbook suit le squelette :

1. **Quand l'utiliser** — trigger conditions (alerte, incident, planification)
2. **Prérequis** — permissions, outils, infos à collecter
3. **Procédure step-by-step** — commandes copiables, à exécuter dans l'ordre
4. **Rollback** — comment annuler si ça casse
5. **Vérifications** — comment confirmer que ça a marché
