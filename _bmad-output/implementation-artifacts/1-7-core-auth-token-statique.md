# Story 1.7: Core auth (token statique MVP + rotation)

Status: review

> 🔐 **Quatrième fondation Core de l'Epic 1** (post-1.4 event_bus, post-1.5 repositories, post-1.6 LLM abstraction). Story **bloquante pour l'exposition Tailscale/VPN** — toutes les routes `/api/v1/*` actuelles sont sans auth (`AuthTokenMiddleware` existant mais NON enregistré). Cette story active la protection en l'enregistrant dans `main.py`.
>
> **Stubs existants à compléter (NE PAS recréer) :**
> - `app/middleware.py:AuthTokenMiddleware` (lignes 67-89) — classe existante, dispatch pass-through, non enregistrée dans `main.py`
> - `shared/auth/__init__.py` — `verify_token()` stub levant `NotImplementedError("Auth token verification implemented in Story 1.7")`
> - `settings.agentive_api_token: SecretStr` — déclaré dans `shared/config.py:54-56`, alias `AGENTIVE_API_TOKEN`, défaut `"change_me"`
>
> **Ce que cette story livre :**
> - `bcrypt>=4.3` ajoutée à `pyproject.toml`
> - `shared/auth/__init__.py` : `verify_token`, `hash_token`, `generate_token`
> - `AuthTokenMiddleware.dispatch` : validation Bearer + 401 RFC 7807 + audit event fire-and-forget
> - `POST /api/v1/admin/rotate-token` : rotation sécurisée avec audit event
> - `app.state.auth_token_hash` initialisé au boot depuis env var
> - Fix des tests Story 1.6 cassés par l'activation de l'auth
>
> **Anti-scope strict :**
> - **PAS** de session-based auth (cookie HTTP-only) — réservé Sprint 4 Growth
> - **PAS** de RBAC (roles owner/collaborator/freelance) — réservé Epic 9 + Sprint 4
> - **PAS** d'`AuditEventRepo.record()` — lève `NotImplementedError`, deferred Story 9.1 ; les audit events auth passent par l'event bus outbox uniquement
> - **PAS** de rate limiting sur l'auth endpoint — réservé Story 9.5
> - **PAS** de multi-token / multi-user auth — `AGENTIVE_API_TOKEN` = 1 token global (MVP single-user)
> - **PAS** de JWT / claims / expiry — comparaison token simple, pas de JWT
> - **PAS** de migration Alembic — le secret store de rotation est `app.state.auth_token_hash` (in-process ; MVP documented limitation : restart réinitialise depuis env var)
>
> **Référence canonique :** Epic 1 lignes 684-710 ; Architecture lignes 362-376 (Auth MVP + security decisions), 609-759 (Security hardening) ; `shared/config.py:54-56` ; `app/middleware.py:67-89`.
> **NFRs ciblés :** **NFR8** (audit trail 100% actions), **NFR9** (aucun secret en clair dans logs).

## Story

As **John** (propriétaire de la plateforme Agentive),
I want un middleware FastAPI validant `Authorization: Bearer <token>` contre `AGENTIVE_API_TOKEN` (comparaison bcrypt hash ou `hmac.compare_digest` en mode plaintext dev) + un endpoint de rotation sécurisée avec audit event sur l'event bus,
So that toutes les routes `/api/v1/*` sont protégées quand Agentive est exposée via Tailscale/VPN, sans complexité RBAC pour le MVP.

## Acceptance Criteria

### AC1 — Dépendance `bcrypt` + module `shared/auth/` implémenté

**Given** le module `agentive_backend.shared.auth` est mis à jour (réécriture du stub existant)
**When** je consulte le module
**Then** trois fonctions publiques sont exposées :
  - `generate_token() -> str` : retourne `secrets.token_urlsafe(32)` — token brut URL-safe (~43 chars)
  - `hash_token(raw: str) -> str` : retourne `bcrypt.hashpw(raw.encode(), bcrypt.gensalt(rounds=12)).decode()` — hash bcrypt `$2b$12$...`
  - `verify_token(raw: str, stored: str) -> bool` : si `stored.startswith("$2b$")` → `bcrypt.checkpw(raw.encode(), stored.encode())`; sinon → `hmac.compare_digest(raw, stored)` (plaintext fallback pour dev — compatible avec le défaut `"change_me"`)
**And** `bcrypt>=4.3` est ajouté dans `backend/pyproject.toml` dependencies (groupe principal, après `apscheduler>=3.11.2`, avant `cryptography>=46.0.7`)
**And** un test unitaire `tests/unit/auth/test_auth_utils.py` couvre :
  - `generate_token()` : longueur ≥ 32 chars, deux appels successifs différents (entropie), caractères URL-safe uniquement
  - `hash_token()` : retourne un hash préfixé `$2b$`, round-trip `verify_token(raw, hash_token(raw)) is True`
  - `verify_token()` mode bcrypt : bon token → `True`, mauvais token → `False`
  - `verify_token()` mode plaintext fallback : `verify_token("change_me", "change_me") is True`, `verify_token("other", "change_me") is False`
  - Total : ≥ 6 tests unitaires

### AC2 — `AuthTokenMiddleware` protège `/api/v1/*` (401 RFC 7807)

**Given** `AuthTokenMiddleware` est enregistré dans `app/main.py` après `CorrelationIdMiddleware`
**When** une requête arrive sur `/api/v1/*` sans header `Authorization`
**Then** la réponse est `HTTP 401` avec `Content-Type: application/problem+json` et body RFC 7807 :
  ```json
  {
    "type": "/errors/auth/missing-token",
    "title": "Missing authentication token",
    "status": 401,
    "correlation_id": "<cid>"
  }
  ```
**And** le header `WWW-Authenticate: Bearer` est présent dans la réponse 401
**And** aucun détail du token ou de l'env var n'est révélé dans la réponse

**Given** une requête avec `Authorization: Bearer <mauvais_token>`
**When** `verify_token(raw, request.app.state.auth_token_hash)` retourne `False`
**Then** la réponse est `HTTP 401` avec body RFC 7807 :
  ```json
  {
    "type": "/errors/auth/invalid-token",
    "title": "Invalid authentication token",
    "status": 401,
    "correlation_id": "<cid>"
  }
  ```

**Given** une requête avec `Authorization: Bearer <token_correct>`
**When** `verify_token(raw, request.app.state.auth_token_hash)` retourne `True`
**Then** la requête continue vers le handler normalement

**Given** les routes publiques (allowlist constante) :
  - `/health`
  - `/ready`
  - `/api/v1/docs`
  - `/api/v1/openapi.json`
  - `/api/v1/redoc`
**When** une requête arrive sans header `Authorization`
**Then** la requête passe sans vérification auth

**And** l'implémentation modifie `app/middleware.py:AuthTokenMiddleware.dispatch` (remplacement du pass-through). **Ne PAS créer un nouveau fichier middleware.**

### AC3 — Audit event `system.token.used` publié (fire-and-forget)

**Given** une requête avec un token valide arrive sur `/api/v1/*` (hors allowlist)
**When** `verify_token()` retourne `True`
**Then** un event `system.token.used` est publié sur l'event bus via `publish_and_commit` avec payload :
  ```json
  {
    "actor": "api_token",
    "endpoint": "<request.url.path>",
    "method": "<request.method>"
  }
  ```
**And** la publication est **fire-and-forget** via `asyncio.create_task` — une erreur DB **NE bloque PAS** la requête ; l'erreur est loggée en WARNING avec `correlation_id`
**And** les valeurs `path` et `method` sont capturées **avant** le `create_task` (ne pas passer `request` dans la coroutine — la request peut être GC avant que la task s'exécute)
**And** le middleware accède à la session factory via `request.app.state.session_factory` (wired dans `lifespan.py`, disponible depuis Story 1.5)
**And** `system.token.used` utilise le préfixe `system` — déjà présent dans `KNOWN_MODULE_PREFIXES` (`shared/event_bus/naming.py:24`). **Ne PAS ajouter** un préfixe `m0` (absent de `KNOWN_MODULE_PREFIXES`, le middleware lui-même documente `m0.token.used` — c'est un vestige du draft initial, ignorer)
**And** un test integration `tests/integration/auth/test_middleware_audit_event.py` vérifie qu'après une requête authentifiée, une row `outbox_events` avec `event_type="system.token.used"` est présente en DB (fixture `migrated_db` de Story 1.5)

### AC4 — Endpoint `POST /api/v1/admin/rotate-token`

**Given** John fait `POST /api/v1/admin/rotate-token` avec le token courant en `Authorization: Bearer`
**When** le middleware valide le token (AC2)
**Then** le handler de rotation :
  1. Génère `new_token = generate_token()`
  2. Calcule `new_hash = hash_token(new_token)` (toujours bcrypt, jamais plaintext)
  3. Met à jour `request.app.state.auth_token_hash = new_hash`
  4. Publie `system.token.rotated` via `publish_and_commit` avec payload `{"actor": "api_token"}`
  5. Retourne `HTTP 200` :
    ```json
    {
      "new_token": "<token_brut>",
      "note": "Save this token immediately — it will not be shown again. Update AGENTIVE_API_TOKEN in your .env to persist across restarts."
    }
    ```
**And** après rotation, les requêtes avec l'ANCIEN token retournent 401 immédiatement
**And** l'endpoint est dans `api/admin/rotate_token.py` (nouveau fichier), exporté depuis `api/admin/__init__.py`, inclus dans `main.py` (section admin endpoints, à côté de `llm_health_router`)
**And** si la requête est faite sans `Authorization` ou avec un mauvais token → le middleware renvoie 401 (même comportement que les autres routes `/api/v1/*`)
**And** un test integration `tests/integration/auth/test_rotate_token.py` couvre :
  - Rotation avec bon token → 200, nouveau token fonctionnel, ancien token → 401
  - Tentative de rotation sans auth → 401 (géré par middleware)

### AC5 — Initialisation `app.state.auth_token_hash` dans `lifespan.py`

**Given** l'application FastAPI démarre
**When** `lifespan` s'exécute (après le boot DB existant)
**Then** `app.state.auth_token_hash` est initialisé depuis `settings.agentive_api_token.get_secret_value()` :
  - Si la valeur commence par `$2b$` → bcrypt hash détecté, utiliser directement, logger `auth.token_initialized mode=bcrypt`
  - Sinon → plaintext mode, stocker la valeur brute, logger `auth.token_initialized mode=plaintext`
**And** si `settings.environment == "production"` ET la valeur ne commence pas par `$2b$` → logger `CRITICAL` + lever `RuntimeError` avec message d'aide :
  ```
  AGENTIVE_API_TOKEN must be a bcrypt hash in production.
  Generate one with: python -c "import bcrypt, secrets; print(bcrypt.hashpw(secrets.token_urlsafe(32).encode(), bcrypt.gensalt(12)).decode())"
  ```
**And** un test integration `tests/integration/auth/test_lifespan_auth_init.py` couvre :
  - Démarrage avec `AGENTIVE_API_TOKEN=change_me` (env test) → `app.state.auth_token_hash == "change_me"`, mode "plaintext"
  - Démarrage avec `AGENTIVE_API_TOKEN=$2b$12$<valid_hash>` → mode "bcrypt"
  - Démarrage en production avec valeur plaintext → `RuntimeError`

### AC6 — Tests Story 1.6 mis à jour post-activation auth

**Given** `AuthTokenMiddleware` est maintenant enregistré dans `main.py`
**When** les tests existants `tests/integration/test_admin_health_llm_endpoint.py` (Story 1.6) s'exécutent
**Then** ils passent — ajouter `headers={"Authorization": "Bearer change_me"}` aux requêtes TestClient qui appellent des routes `/api/v1/*`
**And** les tests `tests/integration/test_lifespan_llm_router_built.py` (Story 1.6) passent — la fixture `fresh_settings` doit couvrir `AGENTIVE_API_TOKEN` (déjà défini avec défaut `"change_me"` dans Settings, aucune action requise si `fresh_settings` ne le surcharge pas)

### AC7 — Couverture tests (≥ 18 tests verts)

**Given** la story est implémentée
**Then** la suite couvre :
  - Unit `tests/unit/auth/test_auth_utils.py` : ≥ 6 tests
  - Integration `tests/integration/auth/test_middleware_auth.py` : ≥ 6 tests (no header → 401, bad token → 401, good token → 200, routes publiques /health /ready sans auth, `X-Correlation-ID` présent dans la réponse 401)
  - Integration `tests/integration/auth/test_middleware_audit_event.py` : ≥ 2 tests
  - Integration `tests/integration/auth/test_rotate_token.py` : ≥ 2 tests
  - Integration `tests/integration/auth/test_lifespan_auth_init.py` : ≥ 3 tests
**And** total cible : ≥ 18 tests verts (6 unit + 12 integration)
**And** full suite : ≥ ~302 passed (284 baseline Story 1.6 + ≥ 18 nouveaux), 0 régression

### AC8 — mypy --strict + ruff + import-linter OK

**Given** la story est implémentée
**Then** `lint-imports --config .import-linter` : **5 contracts kept, 0 broken** (aucun nouveau contract nécessaire — `shared/auth` est un module shared standard, conforme au layered contract)
**And** `mypy --strict src/` : 0 issues
**And** `ruff check` + `ruff format` : 0 issues

## Tasks / Subtasks

### T1. Dépendance bcrypt + `shared/auth/` (AC1)

- [x] T1.1 — Ajouter `bcrypt>=4.3` dans `backend/pyproject.toml` (groupe principal, après `"apscheduler>=3.11.2"`, avant `"cryptography>=46.0.7"`)
- [x] T1.2 — Réécrire `backend/src/agentive_backend/shared/auth/__init__.py` : implémenter `generate_token()`, `hash_token()`, `verify_token()` (modes bcrypt + plaintext fallback via `hmac.compare_digest`). Remplacer le stub existant intégralement.
- [x] T1.3 — Créer `tests/unit/auth/__init__.py` + `tests/unit/auth/test_auth_utils.py` (≥ 6 tests verts)

### T2. Boot init `app.state.auth_token_hash` (AC5)

- [x] T2.1 — Dans `backend/src/agentive_backend/app/lifespan.py` : ajouter l'initialisation de `app.state.auth_token_hash` après le boot DB existant. Detect mode (`$2b$` prefix = bcrypt, sinon plaintext), fail-fast prod sur plaintext, log INFO `auth.token_initialized mode=<mode>`.
- [x] T2.2 — Créer `tests/integration/auth/__init__.py` + `tests/integration/auth/test_lifespan_auth_init.py` (3 tests verts, réutilise pattern `fresh_settings` fixture de Story 1.6)

### T3. `AuthTokenMiddleware` implémentation (AC2, AC3)

- [x] T3.1 — Implémenter `AuthTokenMiddleware.dispatch` dans `app/middleware.py` :
  - Constante `_PUBLIC_ROUTES: frozenset[str]` en haut du module (hors classe) — `/health`, `/ready`, `/api/v1/docs`, `/api/v1/openapi.json`, `/api/v1/redoc`
  - Si `request.url.path in _PUBLIC_ROUTES` → `call_next(request)` direct
  - Extraire Bearer token du header `Authorization` (absent → 401 `/errors/auth/missing-token` + `WWW-Authenticate: Bearer`)
  - `verify_token(raw, request.app.state.auth_token_hash)` → False → 401 `/errors/auth/invalid-token`
  - True → capturer `path = request.url.path`, `method = request.method`, `factory = request.app.state.session_factory`; lancer `asyncio.create_task(_publish_token_used_event(factory, path, method))`; `call_next(request)`
  - Implémenter `async def _publish_token_used_event(factory, path, method)` comme fonction module-level (pas méthode de la classe)
- [x] T3.2 — Enregistrer `AuthTokenMiddleware` dans `app/main.py` (LIFO Starlette : AuthToken ajouté EN PREMIER pour que CorrelationId s'exécute EN PREMIER)
- [x] T3.3 — Créer `tests/integration/auth/test_middleware_auth.py` (7 tests verts)
- [x] T3.4 — Créer `tests/integration/auth/test_middleware_audit_event.py` (2 tests verts, avec `migrated_db` fixture + `httpx.AsyncClient` + ASGI transport)

### T4. Endpoint `POST /api/v1/admin/rotate-token` (AC4)

- [x] T4.1 — Créer `backend/src/agentive_backend/api/admin/rotate_token.py` avec handler `rotate_token(request: Request)` : generate_token → hash_token → update `request.app.state.auth_token_hash` → publish `system.token.rotated` → retourner 200 avec `new_token` + `note`
- [x] T4.2 — Exporter `rotate_token_router` depuis `api/admin/__init__.py` (aux côtés de `llm_health_router`)
- [x] T4.3 — Inclure `rotate_token_router` dans `app/main.py` section "Admin endpoints" (à côté de `llm_health_router`)
- [x] T4.4 — Créer `tests/integration/auth/test_rotate_token.py` (2 tests verts)

### T5. Fix tests Story 1.6 post-activation auth (AC6)

- [x] T5.1 — Tests Story 1.6 vérifiés : `test_admin_health_llm_endpoint.py` crée sa propre mini-app sans `AuthTokenMiddleware` → aucune modification requise, tests passent sans auth headers.
- [x] T5.2 — `test_lifespan_llm_router_built.py` : `fresh_settings` patch déjà `AGENTIVE_API_TOKEN=test-fixture-token` (plaintext) → compatible avec le nouveau `_init_auth_token` (mode plaintext). Aucune modification requise.

### T6. Polish (AC8)

- [x] T6.1 — `mypy --strict src/` : 0 issues (77 source files)
- [x] T6.2 — `ruff check --fix` + `ruff format` : 0 issues restants
- [x] T6.3 — `lint-imports` (docker run avec mount `.import-linter`) : 5 contracts kept
- [x] T6.4 — Full pytest suite (docker run avec socket) : **307 passed, 1 skipped** (skip attendu), 0 régression

## Dev Notes

### 🎯 Pourquoi cette story est fondationnelle

Sans auth, toute exposition réseau d'Agentive (Tailscale, VPN, port-forward) est une surface d'attaque ouverte. L'architecture est explicite (ligne 362) : auth MVP = token statique env var + validation middleware FastAPI. Cette story est la pré-condition minimale pour déployer Agentive hors localhost.

### 🚧 Stubs existants — NE PAS recréer, modifier en place

**CRITIQUE** : ces fichiers existent déjà avec des stubs. Les modifier, pas les recréer :

| Fichier | Stub existant | Action |
|---|---|---|
| `shared/auth/__init__.py` | `verify_token()` raise `NotImplementedError` | Réécrire intégralement |
| `app/middleware.py:67-89` | `AuthTokenMiddleware.dispatch` → `call_next(request)` | Implémenter `dispatch` |
| `shared/config.py:54-56` | `agentive_api_token: SecretStr` déclaré | Aucune modification |
| `app/main.py:50` | `app.add_middleware(CorrelationIdMiddleware)` | Ajouter auth middleware APRÈS |

### 🔐 Pattern `verify_token` dual-mode

```python
import bcrypt
import hmac

def verify_token(raw: str, stored: str) -> bool:
    if stored.startswith("$2b$"):
        try:
            return bcrypt.checkpw(raw.encode(), stored.encode())
        except Exception:
            return False
    # Plaintext fallback — dev default "change_me" compatible
    return hmac.compare_digest(raw, stored)
```

**Pourquoi dual-mode** : le défaut `AGENTIVE_API_TOKEN=change_me` dans `.env.example` est plaintext. Forcer bcrypt-only casserait immédiatement les tests et le dev local. Le dual-mode maintient la DX dev tout en supportant bcrypt for production.

### 🔒 Réponses 401 — JSONResponse directement (pas AgentiveError)

**NE PAS** lever `AgentiveError` depuis le middleware. Les middlewares Starlette s'exécutent en dehors du scope de l'`@app.exception_handler(AgentiveError)` de FastAPI. Les exceptions levées dans un middleware Starlette ne passent PAS par les handlers d'exception FastAPI.

**Pattern correct** — retourner un `JSONResponse` directement :

```python
from fastapi.responses import JSONResponse
from agentive_backend.shared.correlation import get_correlation_id

# 401 missing token
return JSONResponse(
    status_code=401,
    content={
        "type": "/errors/auth/missing-token",
        "title": "Missing authentication token",
        "status": 401,
        "correlation_id": get_correlation_id(),
    },
    media_type="application/problem+json",
    headers={"WWW-Authenticate": "Bearer"},
)
```

**IMPORTANT** : `CorrelationIdMiddleware` s'exécute AVANT `AuthTokenMiddleware` (ordre d'ajout dans `create_app`). La ContextVar `correlation_id` est DÉJÀ initialisée quand `AuthTokenMiddleware.dispatch` s'exécute. `get_correlation_id()` retourne le bon cid.

### 📡 Audit event via event bus — PAS via AuditEventRepo

**CRITIQUE** : `AuditEventRepo.record()` lève `NotImplementedError("... wiring deferred to Story 9.1")`. Ne pas appeler.

Les audit events auth passent par l'event bus outbox — même pattern que Story 1.6 (`m3.llm.fallback_triggered` via `publish_and_commit`).

```python
from agentive_backend.shared.event_bus import publish_and_commit
from agentive_backend.shared.logging import get_logger

_log = get_logger(__name__)

async def _publish_token_used_event(
    factory: async_sessionmaker[AsyncSession],
    path: str,
    method: str,
) -> None:
    try:
        async with factory() as session:
            await publish_and_commit(
                session,
                "system.token.used",
                {"actor": "api_token", "endpoint": path, "method": method},
            )
    except Exception:
        _log.warning("auth.audit_event_publish_failed", endpoint=path)
```

**Pourquoi capturer `path`, `method`, `factory` avant `create_task`** : la `request` Starlette est scope-bound et peut être GC après que `call_next` retourne. Passer des str et la factory (app-scoped) évite toute référence à l'objet `request` dans la coroutine background.

### 🔄 Secret store : `app.state.auth_token_hash`

Le hash actif est stocké dans `app.state.auth_token_hash`. Accès thread-safe dans Python async (event loop single-threaded, assignment atomique). Limites MVP documentées :
- Restart réinitialise depuis `AGENTIVE_API_TOKEN` (pas de persistance rotation)
- La réponse de rotation inclut une note explicite : _"Update AGENTIVE_API_TOKEN in your .env to persist across restarts."_

**Wiring dans `lifespan.py`** (pattern miroir `app.state.llm_router` Story 1.6) :

```python
# Dans la section startup du lifespan context manager
raw = settings.agentive_api_token.get_secret_value()
if settings.is_production and not raw.startswith("$2b$"):
    raise RuntimeError(
        "AGENTIVE_API_TOKEN must be a bcrypt hash in production. "
        'Generate one: python -c "import bcrypt, secrets; '
        "print(bcrypt.hashpw(secrets.token_urlsafe(32).encode(), bcrypt.gensalt(12)).decode())\""
    )
mode = "bcrypt" if raw.startswith("$2b$") else "plaintext"
app.state.auth_token_hash = raw
log.info("auth.token_initialized", mode=mode, env=settings.environment)
```

### 🧪 Testing : TestClient + auth

Après enregistrement du middleware, **toutes** les routes `/api/v1/*` nécessitent un header `Authorization`. Pattern pour les tests :

```python
# Option 1 — header par requête
client = TestClient(app)
response = client.get("/api/v1/admin/health/llm", headers={"Authorization": "Bearer change_me"})

# Option 2 — client avec auth par défaut (préféré pour les tests qui testent autre chose que l'auth)
client = TestClient(app, headers={"Authorization": f"Bearer {settings.agentive_api_token.get_secret_value()}"})
```

**Fixture recommandée pour les tests integration non-auth** :

```python
@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.agentive_api_token.get_secret_value()}"}
```

### ⚠️ Attention : allowlist — exact match, pas startswith

```python
_PUBLIC_ROUTES: frozenset[str] = frozenset({
    "/health",
    "/ready",
    "/api/v1/docs",
    "/api/v1/openapi.json",
    "/api/v1/redoc",
})

# Dans dispatch :
if request.url.path in _PUBLIC_ROUTES:
    return await call_next(request)
```

Utiliser `in` (exact match) plutôt que `startswith`. Un `startswith("/api/v1/docs")` permettrait un bypass via `/api/v1/docs/../admin`.

### 🔢 bcrypt rounds=12

`rounds=12` est le standard OWASP pour les tokens longs. Rotation bcrypt coûte ~150ms @ rounds=12 — acceptable pour une opération admin rare. **Ne pas descendre** en dessous de 10 rounds même pour les tests (utiliser `rounds=4` uniquement si les tests mesurent un bottleneck significatif — `bcrypt.checkpw` est rapide même à 12 rounds pour 1 appel).

### 📚 Learnings des Stories 1.4, 1.5, 1.6 à appliquer

**De Story 1.4 (event bus) :**
- `publish_and_commit(session, event_type, payload)` — fire-and-forget avec error swallowing
- `MissingCorrelationIdError` si ContextVar vide hors HTTP context — dans la task background, le ContextVar est propagé automatiquement par `asyncio.create_task` (contexte copié au moment du `create_task`, pas de MissingCorrelationIdError si appelé depuis le middleware HTTP)

**De Story 1.5 (repositories) :**
- `SecretStr.get_secret_value()` pour `agentive_api_token` — déjà disponible
- Fixture `migrated_db` pour tests nécessitant DB (outbox event audit)
- Pattern `async_sessionmaker` disponible via `request.app.state.session_factory`

**De Story 1.6 (LLM abstraction) :**
- Pattern `app.state.xxx` initialisé dans `lifespan.py`, accédé via `request.app.state` — reproduire exactement
- `asyncio.create_task` pour fire-and-forget (même pattern que `_publish_fallback` callback dans LLMRouter)
- Fixture `fresh_settings` pour tests lifespan — inclure `AGENTIVE_API_TOKEN` dans le patching si nécessaire
- `_is_local_request` dans `health_llm.py` peut rester pour défense-en-profondeur — ne pas supprimer sans raison ; les tests doivent passer avec le middleware auth activé

### 🏷️ Naming event — `system.token.used` vs `m0.token.used`

Le commentaire dans `app/middleware.py:75` mentionne `m0.token.used`. C'est un vestige du draft initial — `m0` n'existe PAS dans `KNOWN_MODULE_PREFIXES` (`naming.py:24`). Utiliser `system.token.used` et `system.token.rotated` :
- `system` est dans `KNOWN_MODULE_PREFIXES`
- Cohérent avec `system.app.started` / `system.app.shutdown` (même catégorie : événements système)

**NE PAS** ajouter `m0` à `KNOWN_MODULE_PREFIXES` — l'auth est un composant système transversal, pas un feature module numéroté.

### 🔧 Structure des fichiers à créer/modifier

```
backend/src/agentive_backend/
├── shared/
│   └── auth/
│       └── __init__.py          # MODIFIER — réécrire le stub
├── app/
│   ├── lifespan.py              # MODIFIER — ajouter auth_token_hash init
│   ├── middleware.py            # MODIFIER — implémenter AuthTokenMiddleware.dispatch
│   └── main.py                  # MODIFIER — enregistrer AuthTokenMiddleware
├── api/
│   └── admin/
│       ├── __init__.py          # MODIFIER — exporter rotate_token_router
│       └── rotate_token.py      # CRÉER
backend/pyproject.toml           # MODIFIER — ajouter bcrypt>=4.3

tests/
├── unit/
│   └── auth/
│       ├── __init__.py          # CRÉER
│       └── test_auth_utils.py   # CRÉER
└── integration/
    └── auth/
        ├── __init__.py                      # CRÉER
        ├── conftest.py                      # CRÉER — re-export DB fixtures
        ├── test_middleware_auth.py          # CRÉER
        ├── test_middleware_audit_event.py   # CRÉER
        ├── test_rotate_token.py             # CRÉER
        └── test_lifespan_auth_init.py      # CRÉER
```

---

## Dev Agent Record

### Implementation Plan

Story 1.7 livrée en 6 tâches selon red-green-refactor :
1. **T1** — `bcrypt>=4.3` + `shared/auth/__init__.py` (dual-mode verify_token) + 10 unit tests
2. **T2** — `_init_auth_token()` dans `lifespan.py` + 3 integration tests lifespan
3. **T3** — `AuthTokenMiddleware.dispatch` complet + `_background_tasks` set pour GC-safety + enregistrement LIFO dans `main.py` + 7+2 integration tests
4. **T4** — `api/admin/rotate_token.py` + wiring + 2 integration tests
5. **T5** — Vérification tests 1.6 : aucune modification requise (mini-apps sans AuthTokenMiddleware)
6. **T6** — mypy 0 issues / ruff 0 issues / 5 contracts kept / 307 passed

### Decisions clés prises

- **Starlette LIFO** : AuthToken enregistré AVANT CorrelationId → CorrelationId s'exécute en premier (order d'exécution inverse de l'enregistrement)
- **`httpx.AsyncClient` + ASGI** pour les tests audit event (même event loop que pytest-asyncio → `asyncio.create_task` complète avant l'assertion)
- **`_background_tasks` set** module-level pour éviter le GC des fire-and-forget tasks (RUF006)
- **Pas d'import de `clean_repository_tables` dans conftest.py auth** pour éviter le trigger DB sur les tests purement in-memory (rotate_token, middleware_auth)
- **`seed_session_factory`** pour nettoyer les outbox_events entre tests audit (isolation sans dépendance `autouse`)

### Completion Notes

✅ Story 1.7 implémentée et validée — 307 tests passed (23 nouveaux), 0 régression.
- `shared/auth/__init__.py` : `generate_token`, `hash_token`, `verify_token` (dual-mode bcrypt/plaintext)
- `app/middleware.py` : `AuthTokenMiddleware` complet + `_background_tasks` + `_PUBLIC_ROUTES` allowlist
- `app/lifespan.py` : `_init_auth_token()` — fail-fast prod sur plaintext, log mode
- `app/main.py` : middleware enregistrés en ordre LIFO correct + route `/api/v1/admin/rotate-token`
- `api/admin/rotate_token.py` : rotation token + audit event + note de persistence
- `pyproject.toml` : `bcrypt>=4.3` ajouté

---

## File List

**Créés :**
- `backend/src/agentive_backend/api/admin/rotate_token.py`
- `backend/tests/unit/auth/__init__.py`
- `backend/tests/unit/auth/test_auth_utils.py`
- `backend/tests/integration/auth/__init__.py`
- `backend/tests/integration/auth/conftest.py`
- `backend/tests/integration/auth/test_middleware_auth.py`
- `backend/tests/integration/auth/test_middleware_audit_event.py`
- `backend/tests/integration/auth/test_rotate_token.py`
- `backend/tests/integration/auth/test_lifespan_auth_init.py`

**Modifiés :**
- `backend/pyproject.toml` — ajout `bcrypt>=4.3`
- `backend/src/agentive_backend/shared/auth/__init__.py` — réécriture stub → implémentation
- `backend/src/agentive_backend/app/lifespan.py` — ajout `_init_auth_token()`
- `backend/src/agentive_backend/app/middleware.py` — implémentation `AuthTokenMiddleware.dispatch` + `_background_tasks`
- `backend/src/agentive_backend/app/main.py` — enregistrement middleware LIFO + route rotate-token
- `backend/src/agentive_backend/api/admin/__init__.py` — export `rotate_token_router`

---

## Change Log

- 2026-05-02 — Story 1.7 implémentée : auth statique MVP + rotation + audit events (307 tests, 0 régression)
- 2026-05-03 — Code review fix-batch (18 patches P1-P18) appliqué post-bmad review (3 reviewers parallèles : Acceptance Auditor + Blind Hunter + Edge Case Hunter — 40 findings bruts, 18 patches retenus, 10 deferred). 326 tests passed (vs 307 baseline), 0 régression.

### Fix-batch détail (18 patches)

| # | Sévérité | Patch | Fichiers |
|---|---|---|---|
| P1 | CRITICAL | Add `app.state.session_factory = session_factory` dans lifespan + defensive `getattr` dans middleware/rotate (sinon 500 sur chaque requête prod) | `app/lifespan.py`, `app/middleware.py`, `api/admin/rotate_token.py` |
| P2 | CRITICAL | Skip auth pour `request.method == "OPTIONS"` (CORS preflight était bloqué → toute UI navigateur cassée) | `app/middleware.py` |
| P3 | HIGH | bcrypt prefix regex `^\$2[aby]\$\d{2}\$` au lieu de `$2b$` only + smoke `bcrypt.checkpw` au boot (sinon hash `$2y$` traité en plaintext = bypass risk) | `shared/auth/__init__.py`, `app/lifespan.py` |
| P4 | HIGH | Reject empty `AGENTIVE_API_TOKEN` au boot ET dans `verify_token` (sinon `Bearer ` vide authentifie en dev) | `shared/auth/__init__.py`, `app/lifespan.py` |
| P5 | HIGH | `asyncio.Lock` sur la rotation (sinon concurrent rotations → ghost tokens jamais utilisables) | `api/admin/rotate_token.py` |
| P6 | MEDIUM | `Cache-Control: no-store, Pragma: no-cache` sur la réponse rotate-token (token leak via proxy/cache) | `api/admin/rotate_token.py` |
| P7 | MEDIUM | Bearer scheme case-insensitive via `partition(" ")` + `.lower()` (RFC 7235) | `app/middleware.py` |
| P8 | MEDIUM | Drain `_background_tasks` au shutdown via `asyncio.gather` timeout 5s (sinon audit events perdus en rolling deploy) | `app/lifespan.py` |
| P9 | MEDIUM | Fix `test_rotate_token_old_token_rejected_after_rotation` faux positif → register `/api/v1/protected` route stub | `tests/integration/auth/test_rotate_token.py` |
| P10 | MEDIUM | Test `test_audit_publish_db_failure_does_not_block_request` ajouté | `tests/integration/auth/test_middleware_audit_event.py` |
| P11 | LOW | Reject `Bearer ` (empty token after stripping) → 401 missing-token au lieu de invalid-token | `app/middleware.py` |
| P12 | LOW | `log.critical` avant chaque RuntimeError dans `_init_auth_token` (empty / prod-plaintext / malformed bcrypt) | `app/lifespan.py` |
| P13 | LOW | Replace `asyncio.sleep(0.2)` par `_drain_background_tasks()` deterministic | `tests/integration/auth/test_middleware_audit_event.py` |
| P14 | LOW | Test `test_rotation_publishes_token_rotated_event` ajouté | `tests/integration/auth/test_middleware_audit_event.py` |
| P15 | LOW | Fix commentaire misleading `_PUBLIC_ROUTES` (ASGI normalise traversal — le risque réel est le prefix-leak) | `app/middleware.py` |
| P16 | LOW | Tighten `test_generate_token_length` à `>= 43` (URL-safe base64 de 32 bytes) | `tests/unit/auth/test_auth_utils.py` |
| P17 | LOW | Audit failure log : `_log.warning` → `_log.exception` (visibilité monitoring) | `app/middleware.py`, `api/admin/rotate_token.py` |
| P18 | LOW | Better empty token error message dans `_init_auth_token` | `app/lifespan.py` |

### 19 nouveaux tests post-fix-batch (326 vs 307 baseline)

- `test_auth_utils.py` : +6 tests (empty raw/stored × 3, $2a$/$2y$ variants × 2, plaintext-prefixed-with-dollar)
- `test_lifespan_auth_init.py` : +3 tests (empty raises, $2y$ accepted, malformed bcrypt raises)
- `test_middleware_auth.py` : +6 tests (case-insensitive scheme × 2, empty Bearer × 2, OPTIONS preflight bypass, missing app.state → 503)
- `test_rotate_token.py` : +2 tests (Cache-Control no-store, concurrent rotation lock)
- `test_middleware_audit_event.py` : +2 tests (system.token.rotated published, DB-down audit doesn't block request)

### 10 findings deferred (acknowledged scope/MVP)

D1: rate-limit + admin role séparé (Story 9.5) | D2: multi-worker desync (Sprint 4+) | D3: race rotation mid-flight (acceptable MVP) | D4: PUBLIC_ROUTES trailing slash (operator config) | D5: supprimer `_is_local_request` (spec dit optionnel) | D6: `note` prose dans réponse (MVP UX) | D7: `system.token.used` + `system.token.rotated` doublons sur rotate (semantic noise, documenté) | D8: timing channel bcrypt vs plaintext (info-leak mineur) | D9: high-cardinality task names (pas d'APM Sprint 0) | D10: audit payload path PII (Sprint 1 quand tenant routes shippent)
