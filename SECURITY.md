# Security Policy

## Reporting a Vulnerability

Si vous découvrez une vulnérabilité dans Agentive, merci de la signaler en privé à l'équipe plutôt que d'ouvrir une issue publique. Ouvrir un canal privé via le repo (GitHub Security Advisory) ou contacter directement le mainteneur.

## Security Features (Sprint 0 baseline)

- **Authentification** : token statique via `AGENTIVE_API_TOKEN` (évoluera vers session cookies HTTP-only en Growth)
- **Autorisation** : RLS Postgres sur toutes les tables critiques (défense-en-profondeur multi-tenant)
- **Audit trail** : 100% des actions tracées dans `audit_events`, rétention ≥ 90 jours, table partitionnée immutable (`REVOKE DELETE, UPDATE`)
- **Chiffrement at-rest** : Fernet / AES-256 sur clés API LLM, credentials MCP, données clients sensibles
- **Sandbox outils MCP** : `bubblewrap` (avec fallback `setrlimit`) — network whitelist, mount read-only, timeout
- **Secrets management** :
  - `.env.encrypted` chiffré via SOPS + age (committé)
  - `gitleaks` pre-commit + GitHub secret scanning
  - Aucune clé API en clair dans le code / logs / traces (redaction `structlog`)
- **Security headers Caddy** : HSTS, CSP avec nonces, X-Content-Type-Options=nosniff, X-Frame-Options=DENY, Referrer-Policy=strict-origin-when-cross-origin
- **CORS** : whitelist explicite (pas de `*`)
- **Rate limiting** : `slowapi` (FastAPI) avec queues + backoff exponentiel

## Principe du moindre privilège — Rôles Postgres

| Rôle | Usage | Permissions |
|---|---|---|
| `agentive_owner` | Migrations Alembic, propriétaire des objets | ALL sur tables applicatives |
| `agentive_app` | Application runtime (FastAPI) | SELECT/INSERT/UPDATE/DELETE sur tables métier ; **aucun droit sur `audit_events` partitions** |
| `agentive_audit_admin` | Audit log writes et lecture | INSERT-only sur `audit_events` parent table ; SELECT sur partitions |

## Token Rotation

```bash
# Via l'API (à implémenter Story 1.7)
curl -X POST https://localhost:8443/api/v1/admin/rotate-token \
  -H "Authorization: Bearer $OLD_TOKEN"
# Réponse : { "new_token": "..." } (affichée une seule fois)
```

Runbook complet : [`docs/runbooks/rotate-token.md`](./docs/runbooks/rotate-token.md) (à créer lors de l'implémentation de Story 1.7).

## Disclosure Policy

- Vulnérabilités confirmées sont patchées dans les 14 jours ouvrés
- Un CVE est demandé pour toute vulnérabilité d'impact ≥ HIGH
- La section "Fixed in" du changelog référence toujours la version contenant le fix
