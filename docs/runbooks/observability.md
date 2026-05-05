# Observability — guide pratique

Guide opérationnel pour consommer la baseline observabilité Agentive (Sprint 0). Pour les fondations conceptuelles (NFR15/NFR16, choix techniques), voir [`_bmad-output/planning-artifacts/architecture.md`](../../_bmad-output/planning-artifacts/architecture.md) §_API & Communication Patterns_ et §_Infrastructure & Deployment_.

## TL;DR

- **Logs JSON structurés** via `structlog` → stdout, captés par Docker `json-file` driver. Toutes les lignes incluent `timestamp` (ISO UTC), `level`, `event`, et — si une requête HTTP est en cours — `correlation_id`.
- **Correlation ID** généré par `CorrelationIdMiddleware` (UUID v7) au début de chaque requête, propagé via `ContextVar` à tous les logs + events bus + (futur) spans OTel. Echoé en `X-Correlation-ID` response header.
- **Erreurs RFC 7807** : tous les `AgentiveError` raisés sont retournés au format `application/problem+json` avec `type`/`title`/`status`/`detail`/`correlation_id` + champs custom (`agent_id`/`module`/`tenant_id`) au top-level.
- **Redaction 3-stage** automatique : key-name heuristic (`password`/`secret`/`token`/...) → PII regex (emails, IPs) → API keys regex (`sk-ant-*`/`sk-proj-*`/`sk-*`/`pa-*`). Aucun secret ne devrait jamais atteindre stdout.
- **Caddy** émet HSTS + CSP + X-Frame-Options en prod (CSP avec `'nonce-*'` pour le futur). En dev, HSTS est volontairement omis sur localhost self-signed (anti-pollution browser cache).

## Tracer une requête de bout en bout

```bash
# 1. Forcer un correlation_id côté client (sinon le middleware en génère un)
curl -fsS -H "X-Correlation-ID: deadbeef-cafe-7000-8000-000000000001" \
  http://localhost:8080/health
# → response header X-Correlation-ID: deadbeef-cafe-7000-8000-000000000001

# 2. Filtrer les logs backend par correlation_id
docker compose logs backend | jq -c 'select(.correlation_id == "deadbeef-cafe-7000-8000-000000000001")'

# 3. Si une erreur survient, le body de la response contient le même correlation_id :
curl -fsS -H "X-Correlation-ID: ..." -H "Authorization: Bearer $TOKEN" \
  http://localhost:8080/api/v1/agents/templates/does-not-exist
# → 404 application/problem+json
# {"type":"/errors/not-found","title":"Resource not found","status":404,
#  "correlation_id":"...","detail":"..."}
```

## Format des logs JSON

Une ligne stdout typique (pretty-printed pour ce guide) :

```json
{
  "event": "request_completed",
  "level": "info",
  "timestamp": "2026-05-05T14:23:11.812345Z",
  "correlation_id": "01923a8e-1234-7000-8000-abcdef012345",
  "method": "POST",
  "path": "/api/v1/agents/templates",
  "status": 201,
  "duration_ms": 142
}
```

Clés systématiques :
- `event` : nom de l'événement (snake_case, action/résultat)
- `level` : `debug` / `info` / `warning` / `error` / `critical`
- `timestamp` : ISO 8601 UTC
- `correlation_id` : présent si une requête HTTP est en cours (sinon absent)

Clés ad-hoc bound via `logger.bind(...)` ou `structlog.contextvars.bind_contextvars(...)` (ex: `agent_id`, `workflow_id`, `tenant_id`, `duration_ms`).

## Format des erreurs RFC 7807

```json
{
  "type": "/errors/validation",
  "title": "Validation failed",
  "status": 422,
  "detail": "field 'name' must not be empty",
  "correlation_id": "01923a8e-1234-7000-8000-abcdef012345",
  "agent_id": "code-producer-v1",
  "module": "m2_agent_registry",
  "tenant_id": null
}
```

- `Content-Type: application/problem+json` (RFC 7807 §3 — toujours).
- `type` est une URI relative `/errors/<kind>` (par convention Agentive — pas une URL absolue).
- `correlation_id` est toujours présent (même valeur que le response header `X-Correlation-ID`).
- Les champs custom (`agent_id`, `module`, `tenant_id`) viennent de `exc.context` et apparaissent au **top-level** (pas sous une clé enveloppe). Si une clé custom collisionne avec un champ RFC 7807 réservé (`type`/`title`/`status`/`correlation_id`/`detail`), la clé custom est **droppée** et un WARNING `rfc7807_context_collision` est loggé côté serveur.
- **Aucun stack trace** ne fuit dans le body en prod (seulement loggé côté serveur).

## Comment ajouter une redaction pattern

Trois cas selon ce qu'il faut masquer :

1. **Pattern de clé API d'un nouveau provider LLM** (ex: Mistral `mst-*`) → modifier `backend/src/agentive_backend/shared/llm/redaction.py` (`_API_KEY_PATTERNS`) + ajouter un test dans `tests/unit/llm/test_redaction.py`.
2. **Donnée PII supplémentaire** (ex: téléphones, IBAN) → modifier `backend/src/agentive_backend/shared/logging/redaction.py` (ajouter un nouveau `_X_RE` + l'appeler dans `redact_pii`) + test dans `tests/unit/logging/test_redaction.py`.
3. **Nouveau token sensible côté nom de clé JSON** (ex: `refresh_token`) → ajouter le terme à `_SENSITIVE_KEY_TOKENS` dans `shared/logging/redaction.py` + test.

L'ordre des processors dans `configure_logging()` est : key-name → PII → API keys → JSONRenderer. **Ne pas casser cet ordre** sans révision (les sentinelles `[REDACTED_*]` sont conçues pour ne pas se re-matcher entre étapes).

## Caddy security headers — dev vs prod

| Header | Dev (`Caddyfile.dev` localhost self-signed) | Prod (`Caddyfile`) |
|---|---|---|
| Strict-Transport-Security | **Omis** (anti-pollution browser cache localhost) | `max-age=31536000; includeSubDomains` |
| Content-Security-Policy | Permissive (`'unsafe-inline'`/`'unsafe-eval'` pour Vite HMR) | Strict avec `'nonce-{http.request.uuid}'` |
| X-Content-Type-Options | `nosniff` | `nosniff` |
| X-Frame-Options | `DENY` | `DENY` |
| Referrer-Policy | `strict-origin-when-cross-origin` | `strict-origin-when-cross-origin` |
| Permissions-Policy | `geolocation=(), microphone=(), camera=()` | `geolocation=(), microphone=(), camera=()` |
| CORS allow-origin | `http://localhost:5173` (whitelist explicite) | `{$CADDY_CORS_ALLOW_ORIGIN}` (env-driven) |

Le Caddyfile.dev sert sur ports `8080` (HTTP→HTTPS redirect) et `8443` (HTTPS self-signed). Le Caddyfile prod sert sur `80`+`443` avec auto-HTTPS Let's Encrypt OU `tls internal` selon `CADDY_TLS_VALUE`.

## Que faire si on voit une fuite de secret en logs ?

1. **Rotation immédiate** :
   - API token (`AGENTIVE_API_TOKEN`) → `POST /api/v1/admin/rotate-token` (Story 1.7) + `docs/runbooks/rotate-token.md`.
   - LLM API key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `VOYAGE_API_KEY`) → révoquer côté provider, regénérer, mettre à jour `.env.encrypted` (SOPS + age) + redéployer.
2. **Test régression** : ajouter un test unitaire qui reproduit le pattern fuité dans `tests/unit/logging/test_redaction.py` ou `tests/unit/llm/test_redaction.py`. Le test doit échouer sans le fix.
3. **Fix** : si c'est un nouveau pattern, étendre les regex (cf section précédente). Si c'est un nouveau code-path qui logge sans passer par structlog, redirecter vers `get_logger(__name__)`.
4. **Audit** : grep le repo pour `print(`, `logging.info(`, `logger.info(...,`. Tous les call-sites doivent passer par structlog. Le linter ruff a une règle pour bloquer `print()` en code applicatif (`backend/src/`).

## À venir Sprint 1+

- **OpenTelemetry SDK** + `/metrics` Prometheus endpoint (Story 7.1) → alimente le M6 Dashboard temps réel.
- **OpenTelemetry traces** (Story 8.x Sprint 3+) → alimente le M12 Trace Explorer, ajoute `span_id` / `parent_span_id` aux logs (correlation hiérarchique H5).
- **Loki + Promtail** (Sprint 4+) si l'agrégation `docker logs` cesse de suffire.
- **PII Detection structurée** via Microsoft Presidio (Sprint 4+ Compliance Growth) — distinct de la redaction défensive actuelle.
- **Sentry / error aggregator** : pas dans la roadmap MVP. La RFC 7807 + `correlation_id` + structured logs suffisent en self-hosted single-node.
