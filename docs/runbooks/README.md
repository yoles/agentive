# Runbooks

Runbooks opérationnels pour Agentive — procédures concrètes pour des situations récurrentes.

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
