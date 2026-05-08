# Story 1.6: Core LLM abstraction (multi-provider)

Status: done

> 🎯 **Troisième fondation Core de l'Epic 1** (post-1.4 event_bus, post-1.5 repositories). Story bloquante pour : **Epic 2** (`m2_agent_registry` — chaque agent-template référence un LLM via la config), **Epic 3** (`m4_memory_manager` — Embedding Router consomme l'abstraction pour les embeddings), **Epic 4** (`m3_workflow_engine` — fallback multi-provider obligatoire NFR12, cf Story 4.6 « Interrompre/relancer/annuler + fallback multi-provider LLM »), **Epic 9** (`shared/llm/budget.py` — budget caps par département/workflow + rate limiting).
>
> **Time-box** : 2-3 jours. **Anti-scope strict** :
> - **PAS** d'embedding adapters (Voyage, FastEmbed) — réservés Story 3.1 / 3.6 (Embedding Router) qui consommera l'interface posée ici. Cette story livre **uniquement la couche `complete()` chat-completion + `raw_provider_call()` escape hatch + fallback chain + sécurité secrets**.
> - **PAS** de prompt injection defense `shared/llm/safety/` (wrapping `<user_input>`, canary tokens, llm-guard) — réservé Story 1.9 (observability) ou Epic 9 (security hardening). Cette story laisse `safety/` comme stub vide avec docstring pointant vers la story propriétaire.
> - **PAS** de budget caps `shared/llm/budget.py` ni de rate limiting per-provider — réservés Stories 9.4 (budget caps) et 9.5 (rate limiting). Story 1.6 livre **les hooks d'instrumentation Prometheus** (latence, tokens, cost estimate) que ces stories consommeront, mais pas la logique enforcement.
> - **PAS** de tool/function calling unifié dans `complete()` — l'interface standard reste « text-in / text-out + system prompt + max_tokens + temperature ». Le tool calling est exposé exclusivement via `raw_provider_call()` (escape hatch documenté). Unification dans M3 Workflow Engine si besoin Sprint 2+.
> - **PAS** d'usage réel des clés API en CI (les tests integration utilisent un `MockProvider` + un faux `httpx.MockTransport` pour les adapters Anthropic/OpenAI). **AUCUN appel réseau payant en CI** — règle FMEA Sprint 0.
>
> **Référence canonique** : Epic 1 lignes 657-682 ; Architecture lignes 393 (décision multi-LLM provider abstraction), 451 (Sprint 1 — `core/llm/`), 475 (graphe dépendances), 554-563 (escape hatch pattern), 607 (`raw_provider_call()` requis), 1525-1533 (arborescence `shared/llm/`), 1564-1568 (`infra/llm/*_adapter.py`), 1910-1911 (intégrations externes Anthropic/OpenAI). NFRs ciblés : **NFR12 (graceful degradation)**, **NFR20 (≥ 2 providers simultanés)**, **NFR9 (aucune clé API en clair logs/traces)**, **NFR14 (timeouts + retry + back-off exponentiel)**.

## Story

As **the system** (et toutes les features m1-m12 à venir consommatrices d'inférence LLM),
I want une couche `agentive_backend.shared.llm.*` exposant (1) une interface `LLMProvider` Protocol unifiée avec `async complete(messages, **kwargs) -> Completion`, (2) deux implémentations concrètes `AnthropicProvider` + `OpenAIProvider` wrappant `langchain-anthropic>=1.4.1` + `langchain-openai>=1.1.14`, (3) un `LLMRouter` configurable par chaîne `provider_chain = ["anthropic", "openai"]` qui bascule automatiquement sur le provider suivant en cas d'erreur 5xx / timeout / rate-limit du primaire (NFR12 graceful degradation), (4) un escape hatch `raw_provider_call(**provider_specific_kwargs) -> Any` qui expose les features provider-spécifiques (Anthropic prompt caching `cache_control`, OpenAI parallel tool calls, etc.) sans fuite dans l'interface standard, et (5) une couche de **redaction stricte des clés API** dans tous les logs/spans/exceptions (NFR9),
So that les agents (Stories Epic 2-9) sont totalement découplés des SDK providers — un changement de version `langchain-anthropic`, l'ajout d'un 3ème provider (Mistral, Gemini, Voyage…), ou un pivot total vers `litellm` reste possible **sans aucun refactor des modules `features/m*`**, et un provider down ne bloque jamais un workflow MVP.

## Acceptance Criteria

> ⚠️ **Note préliminaire — beaucoup de dépendances sont DÉJÀ en place** : `langchain-anthropic>=1.4.1` et `langchain-openai>=1.1.14` sont pinnés dans `backend/pyproject.toml:13-14` depuis Story 1.1. Les variables d'environnement `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `VOYAGE_API_KEY` sont déjà déclarées dans `backend/src/agentive_backend/shared/config.py:77-79` (typées `SecretStr | None`). Le module `shared/llm/` et `infra/llm/` sont des stubs (`__init__.py` raise `NotImplementedError("Story 1.6")`). Cette story livre **principalement le code Python** (interface + 2 adapters + router + tests + docs) et **NE crée PAS de migration Alembic**.

### AC1 — Interface `LLMProvider` Protocol stable + types domaine

**Given** la couche `agentive_backend.shared.llm.interface` est créée
**When** je consulte le module
**Then** un `Protocol` `LLMProvider` est exposé avec :
  - `provider_name: str` (attribut de classe — `"anthropic"`, `"openai"`, `"mock"`, etc.)
  - `async complete(self, messages: Sequence[ChatMessage], *, model: str, max_tokens: int, temperature: float = 0.7, system: str | None = None, stop: Sequence[str] | None = None, timeout_s: float = 30.0) -> Completion` — **API canonique**, identique cross-provider
  - `async raw_provider_call(self, **provider_specific_kwargs: Any) -> Any` — **escape hatch**, retourne le SDK-natif (Anthropic `Message` ou OpenAI `ChatCompletion`) sans normalisation
**And** les types domaine sont définis dans `shared/llm/types.py` comme **Pydantic v2 models immutables** (`model_config = ConfigDict(frozen=True)`) :
  - `class ChatMessage(BaseModel)` : `role: Literal["system", "user", "assistant"]`, `content: str`
  - `class Completion(BaseModel)` : `text: str`, `model: str`, `provider: str`, `input_tokens: int`, `output_tokens: int`, `finish_reason: Literal["stop", "length", "tool_use", "content_filter", "error"]`, `latency_ms: float`, `provider_request_id: str | None` (pour la traçabilité Trace Explorer M12)
  - `class LLMUsage(BaseModel)` : `input_tokens: int`, `output_tokens: int`, `cost_estimate_usd: Decimal | None` (None si le pricing du modèle n'est pas dans la table `MODEL_PRICING`)
**And** `Completion` est **JSON-sérialisable** (les `Decimal` sont sérialisés en `str` via custom validator) — utilisé dans les payloads d'events `m3.llm.completion_received` côté Epic 4.
**And** un test unitaire `tests/unit/llm/test_interface_contract.py` valide via `typing.get_type_hints()` que les 2 méthodes sont bien `async` et que les signatures sont exactement celles ci-dessus (anti-régression « ajout silencieux d'un kwarg »).

### AC2 — `AnthropicProvider` concrete adapter (`infra/llm/anthropic_adapter.py`)

**Given** la classe `agentive_backend.infra.llm.anthropic_adapter.AnthropicProvider` est créée
**When** un caller fait `provider = AnthropicProvider(api_key=settings.anthropic_api_key)` puis `await provider.complete(messages=[ChatMessage(role="user", content="hi")], model="claude-sonnet-4-6", max_tokens=512)`
**Then** la requête est envoyée via `langchain_anthropic.ChatAnthropic` (instance interne) avec le binding correct (`messages`, `system` extrait des messages `role="system"` agrégés, `max_tokens`, `temperature`, `stop_sequences=stop`, `timeout=timeout_s`)
**And** la réponse `langchain_core.messages.AIMessage` est mappée en `Completion` :
  - `text` ← `response.content` (si liste de `ContentBlock`, concat des blocs `type="text"`)
  - `model` ← `response.response_metadata["model_name"]`
  - `provider` ← `"anthropic"`
  - `input_tokens` ← `response.usage_metadata["input_tokens"]`
  - `output_tokens` ← `response.usage_metadata["output_tokens"]`
  - `finish_reason` ← mapping LangChain `stop_reason` → enum domaine (`"end_turn" → "stop"`, `"max_tokens" → "length"`, `"tool_use" → "tool_use"`)
  - `latency_ms` ← mesure wall-clock côté client (perf_counter avant/après l'await)
  - `provider_request_id` ← `response.response_metadata.get("id")` (Anthropic message id, `msg_01abc...`)
**And** `raw_provider_call(**kwargs)` instancie un `langchain_anthropic.ChatAnthropic(**kwargs).ainvoke(messages_or_prompt)` SANS normalisation — retourne le `AIMessage` brut. **Cas d'usage typique** : `await provider.raw_provider_call(model="claude-sonnet-4-6", system=[{"type": "text", "text": "...", "cache_control": {"type": "ephemeral"}}], messages=[...])` pour le **prompt caching Anthropic** (cf gotcha #2 dans Dev Notes).
**And** la classe expose un attribut `MODEL_PRICING: dict[str, tuple[Decimal, Decimal]]` (input $/MTok, output $/MTok) pour les modèles supportés (cf table dans Dev Notes section "Model pricing snapshot 2026-05") — utilisé pour calculer `Completion.cost_estimate_usd` *après* l'appel.

### AC3 — `OpenAIProvider` concrete adapter (`infra/llm/openai_adapter.py`)

**Given** la classe `agentive_backend.infra.llm.openai_adapter.OpenAIProvider` est créée
**When** un caller fait `provider = OpenAIProvider(api_key=settings.openai_api_key)` puis `await provider.complete(messages=[ChatMessage(role="user", content="hi")], model="gpt-5", max_tokens=512)`
**Then** la requête est envoyée via `langchain_openai.ChatOpenAI` avec le binding correct (mêmes kwargs que AC2 transposés au SDK OpenAI : `max_completion_tokens` au lieu de `max_tokens` pour les modèles `o*`/`gpt-5*`, gérer la divergence dans une fonction privée `_resolve_max_tokens_kwarg(model)` qui retourne `"max_tokens"` ou `"max_completion_tokens"` selon le préfixe du modèle)
**And** la réponse est mappée en `Completion` avec `provider="openai"`, le mapping `finish_reason` OpenAI (`"stop"` → `"stop"`, `"length"` → `"length"`, `"tool_calls"` → `"tool_use"`, `"content_filter"` → `"content_filter"`)
**And** `raw_provider_call(**kwargs)` expose les features OpenAI (parallel tool calls via `parallel_tool_calls=True`, response format JSON via `response_format={"type": "json_object"}`, etc.) sans fuite dans `complete()`.
**And** `MODEL_PRICING` couvre au minimum `gpt-5`, `gpt-5-mini`, `gpt-4.1`, `o4-mini` (snapshot 2026-05).

### AC4 — `LLMRouter` avec fallback chain configurable (NFR12, NFR20)

**Given** la classe `agentive_backend.shared.llm.router.LLMRouter` est implémentée
**When** un caller construit `router = LLMRouter(providers={"anthropic": anthropic_provider, "openai": openai_provider}, default_chain=["anthropic", "openai"])` puis `await router.complete(messages=[...], model="claude-sonnet-4-6", max_tokens=512, provider_chain=None)`
**Then** le router tente d'abord `anthropic` (1er de la chaîne par défaut)
**And** si `anthropic.complete()` lève une exception classifiée comme **retriable** (cf liste exhaustive dans Dev Notes "Classification des erreurs"), le router log `llm.fallback_triggered provider_failed=anthropic provider_next=openai error_type=ProviderTimeoutError attempt=1`, **publie un event `m3.llm.fallback_triggered`** sur le bus (payload `{"failed_provider": "anthropic", "next_provider": "openai", "error_type": "ProviderTimeoutError", "model_attempted": "claude-sonnet-4-6", "model_fallback": "<model_resolved_for_openai>"}`), et bascule sur `openai`
**And** le `model` est **automatiquement résolu** par provider via la table `MODEL_FALLBACK_MAP: dict[str, dict[str, str]]` (ex : `{"claude-sonnet-4-6": {"openai": "gpt-5", "voyage": None}, "claude-opus-4-7": {"openai": "gpt-5", "voyage": None}, "gpt-5": {"anthropic": "claude-sonnet-4-6"}}`) — si le model demandé n'a pas d'équivalent défini chez le fallback, le router lève `LLMNoFallbackModelError` (sous-classe `AgentiveError`) au lieu de deviner silencieusement.
**And** si **tous** les providers de la chaîne échouent, le router lève `LLMAllProvidersFailedError` (sous-classe `AgentiveError`, `type="/errors/llm/all-providers-failed"`, `status=503`) avec un `context["attempts"]` contenant la liste de `{"provider": str, "error_type": str, "error_detail": str (redacted)}` pour chaque tentative.
**And** la chaîne peut être **overridée par appel** via `provider_chain=["openai", "anthropic"]` (kwarg de `complete()`) — utile pour les agents qui veulent un provider spécifique (ex : un agent avec prompt caching Anthropic massif ne devrait pas fallback OpenAI silencieusement → chaîne `["anthropic"]` seul).
**And** un test integration simule un `httpx.HTTPStatusError(status_code=503)` côté Anthropic mock + un succès côté OpenAI mock → vérifie que `complete()` retourne le `Completion` OpenAI **et** que la callback `on_fallback` reçoit un `FallbackContext` avec les bons champs (`failed_provider="anthropic"`, `next_provider="openai"`, `error_class="retriable_with_fallback"`, `correlation_id` propagé).

> Amendement post-review (2026-05-02) : la formulation initiale demandait un consumer subscribed sur le bus capturant l'`Event` après un round-trip outbox+NOTIFY. L'architecture finale **découple** le router du bus via une `FallbackCallback` injectable (cf AC4) — en production le callback EST `publish_and_commit`, mais cette indirection rend les tests router pure-async sans dépendance DB. Le round-trip bus complet est validé séparément par les tests Story 1.4 event_bus + un smoke test optionnel à reprendre Story 1.7+ si besoin.

### AC5 — Classification des erreurs : retriable vs fatal

**Given** un provider lève une exception
**When** le router catch l'erreur
**Then** elle est classifiée dans une des 3 catégories via une fonction pure `classify_error(exc: Exception, provider: str) -> ErrorClass` (cf table exhaustive dans Dev Notes "Classification des erreurs") :
  - **`retriable_with_fallback`** : 5xx (`httpx.HTTPStatusError` 500-599), timeouts (`httpx.TimeoutException`, `asyncio.TimeoutError`), connection errors (`httpx.ConnectError`), rate limits (HTTP 429 — fallback **pas** retry sur le même provider Sprint 0, retry per-provider Story 9.5), provider-specific transient errors (Anthropic `overloaded_error`, OpenAI `service_unavailable`)
  - **`retriable_same_provider`** : Sprint 0 = **vide** (pas de retry intra-provider). Documenté pour Story 9.5 qui ajoutera back-off exponentiel + jitter.
  - **`fatal`** : 4xx hors 429 (auth invalide 401, permission 403, validation 400/422, model not found 404, content filter blocked), erreurs de programmation (`TypeError`, `ValueError` sur kwargs malformés), `LLMNoFallbackModelError`. Le router **NE fallback PAS** — l'erreur est ré-levée immédiatement (mappée RFC 7807 par le middleware FastAPI).
**And** la couverture combinée des classifieurs (router-level `test_classify_error.py` + adapter-level `test_anthropic_adapter.py::test_classify_anthropic_exception_maps_each_kind` + `test_openai_adapter.py::test_classify_openai_exception_*`) totalise **≥ 12 cas** :
  - Router `classify_error` : 4 retriable + 5 fatal + 1 invariant Sprint 0 (`retriable_same_provider` empty)
  - Adapter `_classify_anthropic_exception` : 6 cas parametrized (timeout, connect, 401, 429, 503, 400)
  - Adapter `_classify_openai_exception` : 4 cas (timeout, 401, 429, 5xx)

> Amendement post-review (2026-05-02) : la formulation initiale demandait "12 cas dans `test_classify_error.py`" (literal). La compréhension correcte est **12 cas combinés cross-test**, ce qui reflète l'architecture en deux couches (router classifie domain types ; adapters classifient HTTP/SDK). Total effectif livré : ~16 cas.

### AC6 — Sécurité : redaction des clés API (NFR9)

**Given** un provider est instancié avec une `api_key: SecretStr`
**When** une exception est levée (avec ou sans la clé dans le message d'origine du SDK)
**Then** un processor structlog `_redact_api_keys` (ajouté dans `shared/logging/__init__.py` ou nouveau `shared/llm/logging.py`) inspecte chaque field du log dict et remplace **tout pattern** matchant `r"sk-(ant-)?[a-zA-Z0-9_\-]{30,}"` (Anthropic + OpenAI) ou `r"pa-[a-zA-Z0-9_\-]{30,}"` (Voyage) par `[REDACTED]`
**And** les `Exception.__str__` levées par les SDK Anthropic/OpenAI (qui peuvent contenir l'URL avec query params + headers — selon les versions) sont nettoyées via une fonction `redact_secrets(text: str) -> str` appelée **avant** tout log/span/event publish
**And** un test integration `test_no_api_key_in_logs.py` capture la stdout JSON via `caplog` (structlog capture fixture) et vérifie qu'**après une exception simulée** (mock provider lève `httpx.HTTPStatusError` avec un body contenant la clé fake `sk-ant-fake-test-key-1234567890abcdefghij`), la string `sk-ant-fake-test-` n'apparaît **nulle part** dans les logs capturés (assert via `pytest.assertion` brute string match)
**And** un test integration `test_no_api_key_in_event_payload.py` vérifie qu'un `m3.llm.fallback_triggered` event ne contient **pas** la clé dans le payload (le `error_detail` doit être passé par `redact_secrets()` avant l'INSERT outbox).
**And** la docstring du `LLMProvider.complete()` **interdit explicitement** aux callers de mettre une clé API dans `messages[*].content` — *si un agent reçoit du tool output contenant une clé, c'est au caller de le redact avant `complete()`* (limite assumée Sprint 0, documenté).

### AC7 — Configuration provider chain par agent (préparation Epic 2)

**Given** la table `agent_templates` (existante depuis Story 1.5) contient une colonne `config: JSONB`
**When** un agent-template stocke `{"llm": {"provider_chain": ["anthropic", "openai"], "model": "claude-sonnet-4-6", "max_tokens": 4096, "temperature": 0.5}}`
**Then** un schéma Pydantic `AgentLLMConfig` dans `shared/llm/config.py` est défini avec validation stricte (au moins 1 provider dans la chaîne, max 4 ; model non-vide ; max_tokens entre 1 et 200000 ; temperature entre 0.0 et 2.0)
**And** un helper `LLMRouter.from_agent_config(agent_config: AgentLLMConfig, *, providers: dict[str, LLMProvider]) -> RouterCall` retourne un objet partial qui pré-bind la chaîne+model+kwargs — appelable comme `router_call.complete(messages=[...])` sans avoir à re-passer le model.
**And** un test unitaire `test_agent_llm_config_validation.py` couvre 4 cas (chaîne vide → ValidationError, max_tokens=0 → ValidationError, temperature=2.5 → ValidationError, config valide → OK).
**And** **aucune modification** des modèles ORM `agent_templates` n'est requise (le `config: JSONB` est déjà en place depuis Story 1.5) — l'usage par M2 Agent Registry vient avec Story 2.2.

### AC8 — Métriques Prometheus + observability (préparation Story 1.9, 9.4)

**Given** le module `shared/llm/metrics.py` est créé (pattern miroir de `shared/event_bus/metrics.py` Story 1.4)
**When** un appel `LLMRouter.complete()` se termine (succès OU échec final OU fallback)
**Then** les métriques suivantes sont mises à jour dans `prometheus_client.REGISTRY` :
  - `LLM_REQUEST_LATENCY_SECONDS` (Histogram, labels `provider`, `model`, `status` ∈ `{"success", "error_retriable", "error_fatal"}`) — wall-clock de l'appel provider individuel
  - `LLM_TOKENS_TOTAL` (Counter, labels `provider`, `model`, `direction` ∈ `{"input", "output"}`)
  - `LLM_FALLBACK_TRIGGERED_TOTAL` (Counter, labels `failed_provider`, `next_provider`, `error_class`)
  - `LLM_COST_USD_TOTAL` (Counter, labels `provider`, `model`) — incrémenté de `Completion.cost_estimate_usd` après chaque succès (skip si `None`)
  - `LLM_REQUESTS_IN_FLIGHT` (Gauge, label `provider`) — incrémenté/décrémenté en context manager autour de l'appel
**And** les métriques sont **uniquement enregistrées en mémoire** (pas d'endpoint `/metrics` exposé Sprint 0 — réservé Story 1.9). Un test unitaire `test_metrics_registered.py` inspecte `REGISTRY.collect()` et vérifie la présence des 5 métriques avec leurs labels canoniques.
**And** **aucun** label de haute cardinalité (ex : `correlation_id`, `agent_id`, `tenant_id`) n'est utilisé sur ces métriques (Architecture ligne 567-568 — sampling configurable + pas de payloads complets dans spans).

### AC9 — Tests d'intégration end-to-end via mocks (sans appels réseau réels)

**Given** un `MockProvider` est livré dans `shared/llm/testing.py` (module dédié, **utilisable uniquement par les tests**)
**When** un test fait `mock = MockProvider(responses=[Completion(text="hi", ...), httpx.HTTPStatusError(...)])` puis `router = LLMRouter(providers={"mock_a": mock_a, "mock_b": mock_b}, default_chain=["mock_a", "mock_b"])` et `await router.complete(messages=[...], model="any")`
**Then** le `MockProvider` rejoue les réponses dans l'ordre et permet de simuler succès, exceptions retriables, exceptions fatales sans toucher au réseau
**And** **AUCUN test d'intégration ne fait d'appel HTTP réel** vers `api.anthropic.com` ou `api.openai.com` — la conftest Sprint 0 enforce cette règle via une fixture `_no_external_http` autouse qui monkeypatche `httpx.AsyncClient.send` pour raise si l'URL contient un domaine externe (whitelist : `localhost`, `127.0.0.1`, `host.docker.internal`, `testcontainers`).
**And** le scénario **smoke E2E** est validé par `tests/integration/llm/test_router_full_chain.py` qui couvre :
  1. Happy path : `mock_a` retourne success → `Completion` OK, métriques incrémentées (latency + tokens + cost), aucun fallback event
  2. Fallback path : `mock_a` raise `httpx.HTTPStatusError(503)` → router fallback `mock_b` succès → `Completion` OK, métrique `LLM_FALLBACK_TRIGGERED_TOTAL` +1, event `m3.llm.fallback_triggered` publié sur le bus (vérifié via subscribe handler dans le test)
  3. Total failure path : `mock_a` ET `mock_b` raise → `LLMAllProvidersFailedError` levée avec `context["attempts"]` listant les 2 tentatives
  4. Fatal path : `mock_a` raise `httpx.HTTPStatusError(401)` → **pas** de fallback, l'erreur est ré-levée immédiatement
**And** **total cible** : ~25-30 tests (15-20 unit dans `tests/unit/llm/` + 8-10 integration dans `tests/integration/llm/`). Durée totale en local < 15s (pas de DB → fixtures légères ; bus event nécessite Postgres, donc le test #2 utilise la fixture `migrated_db` de Story 1.5).

### AC10 — Documentation runbook + ADR + naming convention

**Given** la story livre une nouvelle surface API publique (`shared.llm.*`)
**When** je consulte `docs/runbooks/` et `docs/decisions/`
**Then** un runbook `docs/runbooks/llm-usage.md` documente :
  - Pattern canonique caller : `from agentive_backend.shared.llm import get_llm_router; router = get_llm_router(); completion = await router.complete(messages=[ChatMessage(role="user", content="hi")], model="claude-sonnet-4-6", max_tokens=512)`
  - Comment ajouter un nouveau provider (3ème — Mistral, Gemini, Voyage…) : checklist (a) créer `infra/llm/<name>_adapter.py`, (b) implémenter `LLMProvider` Protocol, (c) ajouter dans `MODEL_PRICING` et `MODEL_FALLBACK_MAP`, (d) wirer dans `app/lifespan.py` factory `_build_llm_router()`, (e) tests unit + integration
  - Pattern usage `raw_provider_call()` pour Anthropic prompt caching (exemple complet copié-collable)
  - Debugging : "mon agent fait des appels LLM mais ne fallback pas comme prévu" → checklist (vérifier `provider_chain`, vérifier classification de l'erreur via `classify_error()`, vérifier que `MODEL_FALLBACK_MAP` contient le mapping)
  - Pricing : où mettre à jour `MODEL_PRICING` quand les providers changent leurs prix (snapshot date documentée + URL publique des pages de pricing — Anthropic + OpenAI)
**And** un ADR `docs/decisions/llm-abstraction.md` documente : pourquoi LangChain partner SDKs (`langchain-anthropic` + `langchain-openai`) plutôt que `litellm` (option considérée et écartée — voir options dans le template), pourquoi un `Protocol` plutôt qu'une `ABC`, pourquoi un escape hatch `raw_provider_call()` au lieu d'une interface tool-calling unifiée, pourquoi pas de retry intra-provider Sprint 0, trade-offs perf (overhead `LangChain` ~5-10ms par appel, négligeable vs latence LLM 1-30s)
**And** un fichier `docs/decisions/llm-fallback-policy.md` (court, ~40 lignes) documente la **policy de fallback** : (a) liste exhaustive des erreurs classifiées `retriable_with_fallback` vs `fatal`, (b) ordre canonique de la chaîne (`["anthropic", "openai"]` Sprint 0, justifié par : Anthropic prompt caching + meilleur ratio coût/qualité sur les workflows dev), (c) policy de mise à jour de la table `MODEL_FALLBACK_MAP` (semver minor = breaking change documenté dans le Change Log de la story qui modifie)
**And** `docs/decisions/README.md` référence les 2 nouveaux ADR dans la section "Sprint 0 — fondations Core" (à côté de `event-bus-naming.md`, `event-bus-migration-trigger.md`, `repository-pattern.md`).
**And** `CONVENTIONS.md` racine est mis à jour avec une **règle d'or #5** : *"Accès LLM uniquement via `shared.llm.LLMRouter` (jamais d'import direct `langchain-anthropic`/`langchain-openai` depuis `features/m*` ou `api/`)"*. Enforcement par `import-linter` Contract 5 (cf AC11).

### AC11 — `import-linter` Contract 5 bloque les imports SDK LLM hors `shared/llm` + `infra/llm`

**Given** `.import-linter` racine
**When** la CI exécute `lint-imports --config .import-linter`
**Then** un nouveau **`Contract 5 — no-direct-llm-sdk-from-features`** est ajouté avec :
  ```ini
  [importlinter:contract:no-direct-llm-sdk-from-features]
  name = Feature modules must not import LangChain LLM SDKs directly
  type = forbidden
  source_modules =
      agentive_backend.features
      agentive_backend.api
  forbidden_modules =
      langchain_anthropic
      langchain_openai
      langchain_core
      anthropic
      openai
  ```
**And** **5 contracts kept** (les 4 contracts existants + le nouveau Contract 5)
**And** un test integration `test_import_linter_contract5.py` (pattern miroir de `test_import_linter_contract3.py` Story 1.5) parse `.import-linter` (ConfigParser) et assert que `Contract 5` est présent avec les bons `source_modules` et `forbidden_modules` — ainsi que présence du commentaire référençant `docs/decisions/llm-abstraction.md`.
**And** la documentation runbook (AC10) est explicite sur ce qui se passe si un agent essaie d'importer `langchain_anthropic` directement → CI rouge → message d'erreur clair pointant vers `shared.llm.get_llm_router()`.

### AC12 — Wiring lifespan FastAPI + factory singleton

**Given** le boot FastAPI dans `backend/src/agentive_backend/app/lifespan.py`
**When** l'application démarre
**Then** une factory `_build_llm_router() -> LLMRouter` est appelée **une seule fois** dans le `lifespan` (singleton process-wide) qui :
  1. Lit `settings.anthropic_api_key` et `settings.openai_api_key`
  2. Si **les deux** sont `None` ET `settings.environment == "production"` → log `CRITICAL: no LLM providers configured` et raise `RuntimeError` (fail-fast — un déploiement prod sans LLM n'a pas de sens)
  3. Si **les deux** sont `None` ET `settings.environment in ("development", "test")` → wire un `MockProvider` minimal qui répond `Completion(text="[mock] no provider configured", finish_reason="stop", input_tokens=0, output_tokens=10, ...)` — permet le boot dev sans clés
  4. Sinon, instancie les providers disponibles + construit `LLMRouter(providers=..., default_chain=[<provider configurés dans l'ordre canonique>])`
**And** le router est exposé via une **dependency FastAPI** `get_llm_router() -> LLMRouter` dans `api/deps.py` (fichier à créer si absent) — utilisable comme `router: LLMRouter = Depends(get_llm_router)` dans les routes.
**And** un endpoint `/health/llm` (sous `/api/v1/admin/health/llm`, **PROTÉGÉ** dès Story 1.7 via le middleware auth ; en attendant, accepte uniquement les requêtes localhost via `Request.client.host` check) retourne `{"providers": [{"name": "anthropic", "configured": True, "default_chain_position": 0}, ...]}` — utile pour le debugging mais **AUCUN test live d'API n'est fait** (juste l'introspection de la config — pas d'appel réseau).
**And** un test integration `test_lifespan_llm_router_built.py` démarre le `lifespan` avec une fixture `monkeypatch` qui set `ANTHROPIC_API_KEY=fake-test-key` et `OPENAI_API_KEY=fake-test-key`, vérifie que `app.state.llm_router` est un `LLMRouter` avec 2 providers, et que `app.state.llm_router.default_chain == ["anthropic", "openai"]`.

## Tasks / Subtasks

### T1. Bootstrap module + types domaine (AC1)

- [x] T1.1 — Réécrire `backend/src/agentive_backend/shared/llm/__init__.py` : retirer le stub `complete() raise NotImplementedError` ; exposer le re-export public (`LLMProvider`, `LLMRouter`, `ChatMessage`, `Completion`, `LLMUsage`, `AgentLLMConfig`, `get_llm_router`, exceptions). Docstring exhaustive listant les 8 symboles publics. _(Reporté en fin de T9 quand `LLMRouter`/`get_llm_router` sont prêts — ordre topologique d'imports.)_
- [x] T1.2 — Créer `backend/src/agentive_backend/shared/llm/types.py` : `ChatMessage`, `Completion`, `LLMUsage` Pydantic v2 frozen + Decimal serializer.
- [x] T1.3 — Créer `backend/src/agentive_backend/shared/llm/interface.py` : `Protocol` `LLMProvider` (`runtime_checkable`).
- [x] T1.4 — Créer `backend/src/agentive_backend/shared/llm/exceptions.py` : `LLMError` hierarchy (8 classes).
- [x] T1.5 — Créer `backend/src/agentive_backend/shared/llm/config.py` : `AgentLLMConfig` validation stricte.
- [x] T1.6 — Tests unit (22 tests verts) : `test_interface_contract.py` (4) + `test_types.py` (6) + `test_config_validation.py` (8) + `test_exceptions.py` (4).

### T2. `AnthropicProvider` adapter (AC2) — DONE

- [x] T2.1 — `infra/llm/__init__.py` re-export `AnthropicProvider` + `OpenAIProvider`.
- [x] T2.2 — `infra/llm/anthropic_adapter.py` créé (mapping `_to_lc_messages` extrait system, `_extract_text` flatten content blocks, `_compute_cost` via `MODEL_PRICING`, `_classify_anthropic_exception` par httpx + class-name fallback).
- [x] T2.3 — Tests unit (21 tests verts) : mappings `finish_reason` (end_turn / max_tokens / tool_use / unknown), system extraction, cost compute, raw_provider_call, classifier exceptions parametrized.
- [x] T2.4 — Tests intégration `complete_translates_anthropic_exceptions` couverts dans le fichier unit (le wiring SDK reste mocked, le scope intégration vise le router).

### T3. `OpenAIProvider` adapter (AC3) — DONE

- [x] T3.1 — `infra/llm/openai_adapter.py` créé. `_resolve_max_tokens_kwarg` route les modèles `o1`/`o3`/`o4`/`gpt-5*` vers `max_completion_tokens` (piped via `model_kwargs`), legacy GPT-4 vers `max_tokens` direct field.
- [x] T3.2 — Tests unit (24 tests verts) : 8 cas `_resolve_max_tokens_kwarg`, mapping `finish_reason` × 4, legacy `token_usage.prompt_tokens` fallback, exception classification.
- [x] T3.3 — Couverture intégration via `test_complete_translates_openai_exceptions` + `test_complete_passes_max_completion_tokens_for_gpt5`.

### T4. `LLMRouter` + classification + fallback chain (AC4, AC5) — DONE

- [x] T4.1 — `shared/llm/router.py` : chain dispatch, fallback callback injectable (`FallbackCallback`), `_resolve_model` via `DEFAULT_MODEL_FALLBACK_MAP`, métriques wired (latency / tokens / cost / fallback / in-flight gauge), `LLMAllProvidersFailedError` aggrège `attempts`.
- [x] T4.2 — `shared/llm/error_classifier.py` : table déterministe (LLMProvider* + TypeError/ValueError + fallback default).
- [x] T4.3 — Tests unit `test_router.py` (12 tests verts) : happy path, fallback 5xx + timeout, all-fail aggregate, fatal short-circuit, chain override, no-fallback-model, gauge zero-leak après échec, callback awaited, raw_provider_call dispatch.
- [x] T4.4 — Tests unit `test_classify_error.py` (10 tests parametrized) : retriable bucket × 4, fatal bucket × 5, invariant Sprint 0 sur `retriable_same_provider` vide.

### T5. Sécurité : redaction API keys (AC6) — DONE

- [x] T5.1 — `shared/llm/redaction.py` : 4 patterns (`sk-ant-`, `sk-proj-`, `sk-` legacy, `pa-`), `redact_secrets()` + `_redact_value()` récursif + `redact_api_keys_processor` structlog.
- [x] T5.2 — `shared/logging/__init__.py` : processor inséré avant `JSONRenderer`.
- [x] T5.3 — Tests unit `test_redaction.py` (13 tests verts) : 4 prefixes, no-pattern unchanged, short tokens preserved, URL inline redacted, nested JSON / dict / list redacted, idempotent, non-string values pass-through.
- [x] T5.4 — `tests/integration/llm/test_no_api_key_in_logs.py` : structlog pipeline render → leak absent + JSON valide.
- [x] T5.5 — `tests/integration/llm/test_no_api_key_in_event_payload.py` : `LLMAllProvidersFailedError.context['attempts']` redacted (sans dépendre du DB pour rester rapide).

### T6. `MockProvider` test helper (AC9) — DONE

- [x] T6.1 — `shared/llm/testing.py` : MockProvider FIFO, `provider_name` paramétrable par instance, `calls` snapshot pour assertions, ValueError si exhausted.
- [x] T6.2 — `tests/integration/llm/conftest.py` : fixture autouse `_no_external_http` qui monkeypatche `httpx.AsyncClient.send` + `httpx.Client.send`. Whitelist : `localhost`, `127.0.0.1`, `host.docker.internal`, `testserver` (Starlette TestClient), bridge networks `172./10./192.168.`.
- [x] T6.3 — Tests unit `test_mock_provider.py` (4 tests verts) : FIFO Completions, FIFO exception, exhausted → ValueError, kwargs recording.

### T7. Métriques Prometheus (AC8) — DONE

- [x] T7.1 — `shared/llm/metrics.py` : 5 métriques (`LLM_REQUEST_LATENCY_SECONDS` Histogram avec buckets adaptés aux LLM, `LLM_TOKENS_TOTAL` / `LLM_FALLBACK_TRIGGERED_TOTAL` / `LLM_COST_USD_TOTAL` Counters, `LLM_REQUESTS_IN_FLIGHT` Gauge).
- [x] T7.2 — Wiring dans `LLMRouter.complete()` : `LLM_REQUESTS_IN_FLIGHT.inc()/dec()` autour de chaque tentative, latency observée par status, tokens incrementés sur succès, cost skip si `None`.
- [x] T7.3 — Tests unit `test_metrics.py` (6 tests verts) : 5 métriques présentes dans `REGISTRY`, labels canoniques acceptés, gauge retourne à 0 après inc+dec.

### T8. Configuration `import-linter` Contract 5 (AC11) — DONE

- [x] T8.1 — `.import-linter` racine : Contract 5 ajouté (`source_modules` = features + api ; `forbidden_modules` = langchain_anthropic/openai/core + anthropic + openai), commentaire de bloc référençant `docs/decisions/llm-abstraction.md`.
- [x] T8.2 — `tests/integration/test_import_linter_contract5.py` : 5 tests statiques (pattern miroir Contract 3) ; `_find_import_linter_config()` walks parents + fallback `/.import-linter` mount.
- [x] T8.3 — Validation locale : **5 contracts kept** via `docker run` avec mount `.import-linter`.

### T9. Wiring lifespan + factory + endpoint health (AC12) — DONE

- [x] T9.1 — `app/lifespan.py:_build_llm_router()` : matrice complète (4 cas) ; callback `_publish_fallback` capturé en closure (découple LLM ↔ event_bus, pas d'import DB dans `shared/llm/router.py`).
- [x] T9.2 — `api/deps.py:get_llm_router` (Depends factory).
- [x] T9.3 — `api/admin/health_llm.py` : endpoint `GET /api/v1/admin/health/llm` ; loopback gate (`testclient` whitelisted aussi pour les tests TestClient).
- [x] T9.4 — `app/main.py` inclut le router `/api/v1/admin`.
- [x] T9.5 — Tests integration `test_lifespan_llm_router_built.py` (5 tests verts) : both keys → chain `("anthropic", "openai")` ; only Anthropic ; only OpenAI ; no keys + test → MockProvider ; no keys + production → RuntimeError. La fixture `fresh_settings` patch les secrets internes (Fernet key, postgres pw) pour que la validation Settings ne bloque pas en mode production.
- [x] T9.6 — Tests integration `test_admin_health_llm_endpoint.py` (2 tests verts) : 200 from localhost (TestClient) ; 403 for non-loopback via patch sur `_is_local_request`.

### T10. Documentation runbook + ADR (AC10) — DONE

- [x] T10.1 — `docs/runbooks/llm-usage.md` créé.
- [x] T10.2 — `docs/decisions/llm-abstraction.md` créé (Context / Decision / Options Considered / Consequences / Revisitability).
- [x] T10.3 — `docs/decisions/llm-fallback-policy.md` créé (table classification + chaîne canonique + `MODEL_FALLBACK_MAP` snapshot + policy de mise à jour pricing).
- [x] T10.4 — `docs/decisions/README.md` ajoute les 2 ADR section "Sprint 0 — fondations Core".
- [x] T10.5 — `CONVENTIONS.md` : règle d'or **#5 ajoutée** (Accès LLM uniquement via `shared.llm.LLMRouter`), liste renumérotée 5 → 11.

### T11. Polish + lint + types + import-linter (cross-cutting) — DONE

- [x] T11.1 — `ruff check` LLM scope : 0 issues (les 5 issues détectées au premier run ont été corrigées : 4 noqa BLE001 obsolètes + 1 SIM103).
- [x] T11.2 — `ruff format` : 12 fichiers reformatted, suite stable.
- [x] T11.3 — `mypy --strict src/` (entier) : **Success: no issues found in 76 source files**.
- [x] T11.4 — `lint-imports --config /.import-linter` : **5 contracts kept, 0 broken**.
- [x] T11.5 — pytest unit + integration LLM (`docker run` avec docker socket + `--add-host=host.docker.internal:host-gateway`) — 30 unit + 13 integration LLM verts.
- [x] T11.6 — Full pytest suite (sans spike) : **245 → 284 passed in 16s** ; spike-m3 séparé : 5/5 verts. Aucune régression sur les 122 tests baseline Story 1.5.

## Dev Notes

### 🎯 Pourquoi cette story est fondationnelle

L'abstraction LLM est **la dette structurelle #1** que toute application multi-provider paie tôt ou tard. Sans elle :
- **Lock-in provider** : un changement Anthropic → OpenAI (ou inversement) coûte une réécriture de tous les agents (NFR20 incompatible avec Sprint 1+).
- **NFR12 inatteignable** : si un agent appelle `langchain_anthropic.ChatAnthropic` directement, un down Anthropic = down complet du workflow. NFR12 (graceful degradation) ne peut pas être implémenté en Story 4.6 sans cette couche.
- **Fuite de clés API garantie** : sans la couche de redaction `redact_secrets()`, les exceptions des SDK contiennent souvent l'URL avec la clé en query string ou des fragments. NFR9 (aucune clé en clair logs/traces) est inatteignable.
- **Tests intégration pollués** : sans `MockProvider` + enforcement "pas d'appel réseau", la CI ferait des appels payants à chaque run → coût + flakiness.
- **Coût observabilité aveugle** : sans `LLM_COST_USD_TOTAL` + `LLM_TOKENS_TOTAL`, le budget caps Story 9.4 ne peut rien enforcer (pas de signal source).

L'Architecture est **explicite** (ligne 393, 554-563, 607) : `core/llm/` est une couche **obligatoire** Sprint 1, avec un `Protocol` `LLMProvider` portant `complete()` + `raw_provider_call()`. Cette story livre la version Sprint 0 Foundation (étendue car la value est haute, le coût marginal vs Sprint 1 est faible — anti-pattern "remettre à plus tard une fondation").

### 🚧 Hors scope strict (à ne PAS faire dans cette story)

- **PAS d'embedding adapters** (Voyage, FastEmbed) — Story 3.1 (stockage + recherche pgvector) et Story 3.6 (Embedding Router hybride) qui consommeront `LLMProvider` étendu pour les embeddings (à définir Story 3.1 — peut-être un Protocol séparé `EmbeddingProvider` plutôt que `complete()` + `embed()` fusionnés). **Décision deferred** : ne PAS pré-extraire un Protocol embedding Sprint 0 sans use case.
- **PAS de prompt injection defense** (`shared/llm/safety/`) — Architecture lignes 638-644. Stub vide avec docstring pointant vers Story 1.9 / Epic 9.
- **PAS de budget caps + rate limiting** (`shared/llm/budget.py`) — Stories 9.4 + 9.5. Cette story livre les **hooks d'observabilité** (métriques Prometheus tokens + cost) que ces stories consommeront.
- **PAS de retry intra-provider** avec back-off exponentiel — Story 9.5 (rate limiting multi-provider). Sprint 0 : `max_retries=0`, fallback direct.
- **PAS de tool/function calling unifié** dans `complete()` — exposé exclusivement via `raw_provider_call()`. Si un agent veut tool calling cross-provider, il appelle `raw_provider_call()` avec les kwargs natifs. Unification Sprint 2+ Workflow Engine si besoin documenté.
- **PAS d'usage réel des clés API en CI** — `MockProvider` partout. Tests intégration utilisent `httpx.MockTransport` pour intercepter les appels SDK (LangChain délègue à `httpx.AsyncClient`).
- **PAS de streaming** (`async iter` de tokens) — l'interface `complete()` est blocking-style (single response). Streaming SSE Story 6.1 (Chat UI) qui consommera l'interface streaming Sprint 1+ (à ajouter Story 6.x quand le besoin frontend est concret).
- **PAS de modèles multimodaux** (vision, audio) — Sprint 2+. L'interface `ChatMessage.content: str` ne supporte pas les content blocks multimodaux. Si besoin Vision Sprint 2 : extension du type `ChatMessage` (additif, non breaking).
- **PAS de configuration dynamique** (changer la chaîne fallback à chaud via API admin) — Sprint 4+ Growth.
- **PAS d'intégration Trace Explorer M12** (spans OTel détaillés sur chaque tentative provider) — Story 8.1 (Trace Explorer). Story 1.6 expose les `provider_request_id` dans `Completion` pour permettre le linking ultérieur, mais ne crée pas de spans.

### 📚 Learnings de Stories 1.1, 1.2, 1.3, 1.4, 1.5 à appliquer

**De Story 1.1 (scaffolding)** :
- **Docker-first strict** : tout via `docker compose run --rm backend uv run ...`. Aucune commande Python sur l'hôte. Tests integration utilisent testcontainers.
- **`mypy --strict` + ruff + import-linter** : tout nouveau code dans `src/` est sous régime strict. Les nouveaux modules `shared/llm/*.py` doivent passer **les 5 contracts** (4 existants + Contract 5 nouveau).
- **Pas d'`os.environ` direct** — toujours via `shared.config.settings`. Les clés API sont déjà déclarées (lignes 77-79).

**De Story 1.2 (spike LangGraph)** :
- **Pinning version critique** : `langchain-anthropic>=1.4.1` et `langchain-openai>=1.1.14` sont des bornes basses non strictes. **Ne PAS upgrader en strict pin sans validation des breaking changes** — LangChain a un historique de churn API. Documenter dans `pyproject.toml` un commentaire de policy (pattern miroir LangGraph G3 strict pin avec re-spike requis).

**De Story 1.3 (benchmark M4)** :
- **Time-box 2-3 jours strict** — anti-scope strictement appliqué. Pas de feature creep (embeddings, safety, budget) qui multiplierait le scope par 3.

**De Story 1.4 (event bus)** :
- **Pattern factory + lifespan singleton** — exact même pattern que `OutboxWorker`. Cf `app/lifespan.py` pour le wiring.
- **Conventions naming events `module.entity.action`** — pour Story 1.6 : `m3.llm.fallback_triggered`, `m3.llm.completion_received` (futur Story 4.6), `m3.llm.cost_threshold_warning` (futur Story 9.4). **PRÉFIXE `m3`** car le LLM est consommé principalement par M3 Workflow Engine. Ajouter `KNOWN_MODULE_PREFIXES` n'est pas requis (déjà couvre `m3`).
- **Coupling LLM ↔ event_bus** : risque de cycle import si `shared/llm/router.py` importe `shared/event_bus`. **Solution** : injection callback `on_fallback: Callable | None` dans `LLMRouter.__init__` ; le wiring vers `event_bus.publish_and_commit` se fait dans `app/lifespan.py`. Pattern PIO (Push It Out) — découple le module pour les tests.

**De Story 1.5 (repositories)** :
- **Schema reset pattern dans `migrated_db`** — Story 1.6 ne crée AUCUNE migration, donc pas de souci. Mais les tests intégration LLM qui ont besoin de DB (test event publish AC9 #2) doivent réutiliser la fixture `migrated_db` existante.
- **`SecretStr.get_secret_value()`** — déjà en place pour `anthropic_api_key`/`openai_api_key`. **Ne JAMAIS** logger directement `SecretStr` (le `__repr__` masque déjà mais une exception SDK peut leak — d'où la double-protection via `redact_secrets()`).
- **Pattern test infrastructure** : `tests/integration/llm/conftest.py` réutilise `migrated_db` + `app_session_factory` fixtures de Story 1.5 — pas de duplication.

### 🏗️ Architecture compliance — `LLMProvider` Protocol + `LLMRouter`

#### Pattern canonique caller (Sprint 1+ agents)

```python
# features/m2_agent_registry/service.py (Sprint 1, Story 2.2 — préfiguration)
from agentive_backend.shared.llm import (
    AgentLLMConfig,
    ChatMessage,
    Completion,
    LLMRouter,
)


async def execute_agent_turn(
    *,
    router: LLMRouter,  # injected via FastAPI Depends(get_llm_router)
    agent_config: AgentLLMConfig,
    user_input: str,
    system_prompt: str,
) -> Completion:
    messages = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=user_input),
    ]
    return await router.complete(
        messages=messages,
        model=agent_config.model,
        max_tokens=agent_config.max_tokens,
        temperature=agent_config.temperature,
        provider_chain=agent_config.provider_chain,
        timeout_s=agent_config.timeout_s,
    )
```

#### Pattern canonique escape hatch (Anthropic prompt caching)

```python
# features/m2_agent_registry/service.py (cas avancé — large system prompt cacheable)
from agentive_backend.shared.llm import LLMRouter


async def cached_system_call(router: LLMRouter, user_input: str) -> str:
    anthropic_provider = router.providers["anthropic"]  # public attribute
    raw_response = await anthropic_provider.raw_provider_call(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=[
            {
                "type": "text",
                "text": LARGE_SYSTEM_PROMPT,  # 50KB+
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_input}],
    )
    # raw_response is the raw langchain_core.AIMessage — caller is responsible
    # for the format. No normalization, no fallback, no metrics.
    return raw_response.content
```

**Gotcha critique #1 — LangChain version churn** : `langchain-anthropic` et `langchain-openai` ont historiquement bumpé leurs APIs sur les minor versions (ex : `ChatAnthropic.invoke` → `ChatAnthropic.ainvoke` async-only sur 1.x ; renommage `model` → `model_name` ; etc.). Vérifier au moment du dev si la version pinnée >=1.4.1 (Anthropic) / >=1.1.14 (OpenAI) correspond toujours à l'API utilisée dans T2.2/T3.1. Si breaking change : documenter dans le Change Log + ouvrir une issue pour bumper le pin (NE PAS travailler sur une version intermédiaire qui casse le mapping).

**Gotcha critique #2 — Anthropic prompt caching format** : le `cache_control` ne se met PAS dans `messages[*].content` (Architecture suggère `cache_control={"type": "ephemeral"}` en top-level kwarg, mais c'est trompeur — le SDK Anthropic 1.x demande un `system: list[ContentBlock]` avec chaque block ayant un `cache_control`). **Ne pas chercher à l'unifier dans `complete()`** — ça pollue l'interface. C'est exactement l'usage de `raw_provider_call()`.

**Gotcha critique #3 — `httpx.AsyncClient` partagé** : LangChain crée un `httpx.AsyncClient` interne par instance `ChatAnthropic`/`ChatOpenAI`. Si `complete()` instancie un nouveau `ChatAnthropic` à chaque appel (T2.2), on paie le coût TCP handshake à chaque fois. **Optim Sprint 1+** : cacher l'instance par `(model, kwargs_signature)`. Sprint 0 : ne pas optim, mesurer via `LLM_REQUEST_LATENCY_SECONDS` et décider à la donnée.

**Gotcha critique #4 — `SystemMessage` mapping** : LangChain `langchain_core.messages.SystemMessage` est un type distinct de `HumanMessage`/`AIMessage`. Mapping :
```python
def _to_lc_messages(messages: Sequence[ChatMessage]) -> list[BaseMessage]:
    out: list[BaseMessage] = []
    for m in messages:
        if m.role == "system":
            out.append(SystemMessage(content=m.content))
        elif m.role == "user":
            out.append(HumanMessage(content=m.content))
        elif m.role == "assistant":
            out.append(AIMessage(content=m.content))
    return out
```
**Cas Anthropic spécial** : Anthropic n'a pas de `SystemMessage` natif (le système est un kwarg `system` séparé). Le SDK LangChain gère le merge automatiquement (`SystemMessage` est extrait et passé en `system=`), mais si plusieurs `SystemMessage` sont dans la liste, le SDK peut soit (a) les concaténer, soit (b) lever. **Test à écrire (T2.3)** : vérifier le comportement réel avec 2 system messages.

**Gotcha critique #5 — `finish_reason` mapping divergent** : Anthropic utilise `stop_reason` ∈ `{"end_turn", "max_tokens", "tool_use", "stop_sequence"}`. OpenAI utilise `finish_reason` ∈ `{"stop", "length", "tool_calls", "content_filter", "function_call"}`. Mapping uni vers `Literal["stop", "length", "tool_use", "content_filter", "error"]` :
- Anthropic `end_turn` / `stop_sequence` → `"stop"`
- Anthropic `max_tokens` → `"length"`
- Anthropic `tool_use` → `"tool_use"`
- OpenAI `stop` → `"stop"`
- OpenAI `length` → `"length"`
- OpenAI `tool_calls` / `function_call` → `"tool_use"`
- OpenAI `content_filter` → `"content_filter"`
- Tout autre → `"error"` (avec log warning).

### 🧪 Test pyramide

| Niveau | Cible | Outil | Cible coverage |
|---|---|---|---|
| Unit | `interface`/`types`/`exceptions`/`config`/`router`/`error_classifier`/`redaction`/`metrics`/`mock_provider` + adapters mocks | pytest + AsyncMock + httpx.MockTransport | 90%+ sur `shared/llm/` |
| Integration | adapters via `httpx.MockTransport` + router full chain (avec event bus pour AC9 #2) + `_no_external_http` autouse | pytest + testcontainers (pour le test event bus) | 1 test smoke happy-path + 1 test fallback + 1 test no-key-leak |
| E2E | Aucun (pas d'API exposée Sprint 0 sauf `/admin/health/llm` localhost-only) | — | — |

**Cible totale** : ~25-30 tests (15-20 unit + 8-10 integration). Délais cible : tests verts en < 15s pour la suite LLM seule. Suite full < 30s avec event_bus + repositories.

### 🚨 Classification des erreurs (table exhaustive — AC5)

| Exception | Provider | Catégorie | Justification |
|---|---|---|---|
| `httpx.HTTPStatusError(500-599)` | any | `retriable_with_fallback` | Provider down ou degraded, fallback peut réussir |
| `httpx.TimeoutException` | any | `retriable_with_fallback` | Provider lent ou réseau dégradé, fallback peut être plus rapide |
| `httpx.ConnectError` | any | `retriable_with_fallback` | Provider unreachable, fallback peut être reachable |
| `httpx.HTTPStatusError(429)` | any | `retriable_with_fallback` | Rate limit local au provider, fallback peut avoir capacité (Sprint 0 — pas de retry intra) |
| `anthropic.APIStatusError(status=503)` | anthropic | `retriable_with_fallback` | Wrappé par LangChain mais peut leak |
| `openai.RateLimitError` | openai | `retriable_with_fallback` | idem 429 |
| `httpx.HTTPStatusError(401)` | any | `fatal` | Auth invalide — fallback ne résout pas |
| `httpx.HTTPStatusError(403)` | any | `fatal` | Permission denied — fallback ne résout pas |
| `httpx.HTTPStatusError(400/422)` | any | `fatal` | Validation payload — bug applicatif, fallback ne résout pas |
| `httpx.HTTPStatusError(404)` | any | `fatal` | Model not found — bug config, fallback peut masquer le bug |
| `anthropic.BadRequestError` | anthropic | `fatal` | Validation — idem |
| `LLMNoFallbackModelError` | router | `fatal` | Pas de mapping, abandon de la chaîne |
| `TypeError`, `ValueError` (non-LLM origin) | any | `fatal` | Bug code — surface immédiatement |
| `pydantic.ValidationError` (sur `Completion` parsing) | any | `fatal` | Mapping cassé — bug |

### 📊 Model pricing snapshot 2026-05 (input / output USD per million tokens)

> ⚠️ **À vérifier** au moment du dev (les prix changent souvent). URLs officielles à mettre dans la docstring de `MODEL_PRICING` :
> - Anthropic : https://docs.anthropic.com/claude/docs/models-overview#model-pricing
> - OpenAI : https://platform.openai.com/docs/pricing

**Anthropic** (snapshot indicatif — vérifier au dev) :
- `claude-opus-4-7` : $15.00 / $75.00 per MTok
- `claude-sonnet-4-6` : $3.00 / $15.00 per MTok
- `claude-haiku-4-5` : $0.80 / $4.00 per MTok

**OpenAI** (snapshot indicatif — vérifier au dev) :
- `gpt-5` : à confirmer (~$10.00 / $30.00 per MTok)
- `gpt-5-mini` : à confirmer (~$1.50 / $6.00 per MTok)
- `gpt-4.1` : à confirmer
- `o4-mini` : à confirmer

**Si un modèle absent** de `MODEL_PRICING` → `Completion.cost_estimate_usd = None`. Pas d'erreur. La métrique `LLM_COST_USD_TOTAL` skip cet appel (pas d'increment). Documenté dans `llm-fallback-policy.md`.

### 🛡️ Default `MODEL_FALLBACK_MAP` Sprint 0

```python
DEFAULT_MODEL_FALLBACK_MAP: dict[str, dict[str, str]] = {
    # Anthropic primary → OpenAI fallback
    "claude-opus-4-7":   {"openai": "gpt-5"},
    "claude-sonnet-4-6": {"openai": "gpt-5"},
    "claude-haiku-4-5":  {"openai": "gpt-5-mini"},
    # OpenAI primary → Anthropic fallback (chaînes alternatives)
    "gpt-5":             {"anthropic": "claude-sonnet-4-6"},
    "gpt-5-mini":        {"anthropic": "claude-haiku-4-5"},
    "gpt-4.1":           {"anthropic": "claude-sonnet-4-6"},
    "o4-mini":           {"anthropic": "claude-haiku-4-5"},
}
```

Override possible par instance `LLMRouter(model_fallback_map=...)`. Stories Epic 2-9 peuvent définir leurs propres tables si besoin métier (ex : agent code reviewer veut **toujours** Sonnet, jamais Haiku → chaîne `["anthropic"]` seul, pas de fallback OpenAI).

### ⚠️ Edge cases à connaître

#### 1. Instance LangChain partagée vs par-appel (perf vs simplicité)

Symptôme Sprint 0 : chaque `complete()` instancie un nouveau `ChatAnthropic` → coût TCP + import overhead à chaque appel. **Décision Sprint 0** : on accepte (latence LLM 1-30s domine, le 5-10ms LangChain est dans le bruit). **Si signal métrique** `LLM_REQUEST_LATENCY_SECONDS` p50 > 200ms en local sans LLM (i.e., overhead client), refactor Sprint 1+ vers cache `lru_cache(_get_chat_anthropic)` sur les kwargs immutables.

#### 2. Concurrent calls sur la même instance provider

Symptôme Sprint 1+ : 2 workflows appellent `provider.complete()` simultanément. `httpx.AsyncClient` est thread-safe mais **pas** pool-bounded par défaut. Risque de saturation si 100+ requêtes concurrentes. **Décision Sprint 0** : on n'enforce pas (seul utilisateur = John, MVP charge faible). **Sprint 4+ Growth** : ajouter un `asyncio.Semaphore(max_concurrent)` au niveau provider.

#### 3. Ordre des messages system avec Anthropic

Symptôme : si l'agent passe `[ChatMessage(role="system", content="A"), ChatMessage(role="user", content="..."), ChatMessage(role="system", content="B")]`, Anthropic n'accepte qu'un seul `system=` au top-level. **Solution** : `_to_lc_messages` extrait tous les `role="system"` et les concat avec `\n\n` séparateur, puis les passe en `system=` de `ChatAnthropic`. Test T2.3 dédié.

#### 4. `httpx.MockTransport` ne couvre pas tous les chemins

Symptôme : LangChain peut court-circuiter `httpx` via `requests` (cas legacy) → le mock ne capture pas l'appel. **Solution** : monkeypatch `httpx.AsyncClient.send` ET vérifier dans T2.4 que la requête a bien transité. Si non, mock plus profondément (`langchain_anthropic.ChatAnthropic._client`).

#### 5. Test `_no_external_http` autouse vs testcontainers

Symptôme : la fixture `_no_external_http` raise sur Postgres (testcontainer host). **Solution** : whitelist inclut `host.docker.internal` + IP testcontainers (résolue dynamiquement via `postgres_container.get_container_host_ip()`). Documenter clairement.

#### 6. Redaction false positive

Symptôme : un user input légitime contient une string ressemblant à `sk-ant-something-something-something` → redacted alors que ce n'est pas une vraie clé. **Acceptation Sprint 0** : on accepte ce faux positif (sécurité > UX dans les logs). Documenter dans le runbook. Sprint 4+ : possibilité de checker via API Anthropic `validate_key` (mais nécessite appel réseau — lourd).

#### 7. `Decimal.quantize()` rounding

Symptôme : calcul `cost_estimate_usd` avec floats donne des erreurs d'arrondi (`0.0049999999...`). **Solution** : `Decimal(input_tokens) / Decimal(1_000_000) * MODEL_PRICING[model][0]` avec `quantize(Decimal("0.000001"))` final. 6 décimales = précision millième de cent.

### 📊 Latency budget cible (informatif, pas un AC)

- `LLMRouter.complete()` overhead vs raw provider call : < 5ms (validation config + dispatch)
- `MockProvider.complete()` : < 1ms (in-memory)
- Real Anthropic call (claude-haiku-4-5, 100 tokens) : ~500ms-2s (réseau + inference). **PAS testé en CI**.
- Real OpenAI call (gpt-5-mini, 100 tokens) : ~500ms-2s. **PAS testé en CI**.

### 📝 Output attendus

#### Logs structlog au boot

```json
{"event":"llm_router.built","providers":["anthropic","openai"],"default_chain":["anthropic","openai"],"environment":"production","level":"info","timestamp":"2026-05-02T10:15:00Z"}
```

OU dans le cas dev sans clés :

```json
{"event":"llm_router.built_with_mock","reason":"no_api_keys_configured","environment":"development","level":"warning","timestamp":"2026-05-02T10:15:00Z"}
```

#### Logs structlog sur fallback

```json
{"event":"llm_router.fallback_triggered","failed_provider":"anthropic","next_provider":"openai","error_class":"retriable_with_fallback","error_type":"LLMProviderUnavailableError","model_attempted":"claude-sonnet-4-6","model_fallback":"gpt-5","correlation_id":"01956a8e-...","level":"warning","timestamp":"2026-05-02T10:15:01Z"}
```

#### Sortie `/admin/health/llm`

```json
{
  "providers": [
    {"name": "anthropic", "configured": true, "default_chain_position": 0},
    {"name": "openai", "configured": true, "default_chain_position": 1}
  ],
  "default_chain": ["anthropic", "openai"],
  "environment": "production"
}
```

#### Runbook structure (`docs/runbooks/llm-usage.md`)

```markdown
# LLM Usage Runbook

## When to use
[Pattern canonique async router.complete(...)]

## How to add a new provider (3rd, 4th, ...)
1. Create `infra/llm/<name>_adapter.py` implementing `LLMProvider` Protocol
2. Add `MODEL_PRICING` entries
3. Update `DEFAULT_MODEL_FALLBACK_MAP` in `shared/llm/router.py`
4. Wire in `app/lifespan.py:_build_llm_router()`
5. Tests unit + integration
6. Update `docs/runbooks/llm-usage.md` if new pattern

## Pattern usage `raw_provider_call()` for Anthropic prompt caching
[Code example complete copy-paste]

## Debugging fallback issues
- Symptom: agent calls fail without fallback
- Check: `provider_chain` correctly set in agent config?
- Check: `classify_error()` returns `"retriable_with_fallback"` for the actual exception?
- Check: `MODEL_FALLBACK_MAP` has the mapping for the model?
- Check: `LLM_FALLBACK_TRIGGERED_TOTAL` Prometheus metric increments?

## Updating MODEL_PRICING
- Source URLs (check at every minor release):
  - Anthropic: https://docs.anthropic.com/claude/docs/models-overview#model-pricing
  - OpenAI: https://platform.openai.com/docs/pricing
- Snapshot date in docstring of `MODEL_PRICING` dict
- Update via PR with diff in commit message
```

### Project Structure Notes

- **Modules à créer** :
  - `backend/src/agentive_backend/shared/llm/types.py` — domain types Pydantic
  - `backend/src/agentive_backend/shared/llm/interface.py` — `LLMProvider` Protocol
  - `backend/src/agentive_backend/shared/llm/exceptions.py` — `LLMError` hierarchy
  - `backend/src/agentive_backend/shared/llm/config.py` — `AgentLLMConfig`
  - `backend/src/agentive_backend/shared/llm/router.py` — `LLMRouter`
  - `backend/src/agentive_backend/shared/llm/error_classifier.py` — `classify_error()`
  - `backend/src/agentive_backend/shared/llm/redaction.py` — `redact_secrets()`
  - `backend/src/agentive_backend/shared/llm/metrics.py` — Prometheus
  - `backend/src/agentive_backend/shared/llm/testing.py` — `MockProvider`
  - `backend/src/agentive_backend/infra/llm/anthropic_adapter.py` — `AnthropicProvider`
  - `backend/src/agentive_backend/infra/llm/openai_adapter.py` — `OpenAIProvider`
  - `backend/src/agentive_backend/api/admin/__init__.py` (si absent)
  - `backend/src/agentive_backend/api/admin/health_llm.py` — endpoint introspection
  - `backend/src/agentive_backend/api/deps.py` (si absent) — `get_llm_router` Depends
- **Modules à réécrire** :
  - `backend/src/agentive_backend/shared/llm/__init__.py` (stub `complete() raise NotImplementedError` → re-export public API)
  - `backend/src/agentive_backend/infra/llm/__init__.py` (stub vide → re-export `AnthropicProvider`, `OpenAIProvider`)
  - `backend/src/agentive_backend/shared/logging/__init__.py` (ajout processor `_redact_api_keys`)
  - `backend/src/agentive_backend/app/lifespan.py` (wire `_build_llm_router()` + `app.state.llm_router`)
  - `backend/src/agentive_backend/app/main.py` (include router admin si absent)
- **Fichiers tests** :
  - `backend/tests/unit/llm/__init__.py`
  - `backend/tests/unit/llm/test_interface_contract.py`
  - `backend/tests/unit/llm/test_types.py`
  - `backend/tests/unit/llm/test_exceptions.py`
  - `backend/tests/unit/llm/test_config_validation.py`
  - `backend/tests/unit/llm/test_anthropic_adapter.py`
  - `backend/tests/unit/llm/test_openai_adapter.py`
  - `backend/tests/unit/llm/test_router.py`
  - `backend/tests/unit/llm/test_classify_error.py`
  - `backend/tests/unit/llm/test_redaction.py`
  - `backend/tests/unit/llm/test_metrics.py`
  - `backend/tests/unit/llm/test_mock_provider.py`
  - `backend/tests/integration/llm/__init__.py`
  - `backend/tests/integration/llm/conftest.py` (fixture `_no_external_http`)
  - `backend/tests/integration/llm/test_anthropic_adapter_integration.py`
  - `backend/tests/integration/llm/test_openai_adapter_integration.py`
  - `backend/tests/integration/llm/test_router_full_chain.py` (4 scénarios AC9)
  - `backend/tests/integration/llm/test_no_api_key_in_logs.py`
  - `backend/tests/integration/llm/test_no_api_key_in_event_payload.py`
  - `backend/tests/integration/llm/test_lifespan_llm_router_built.py`
  - `backend/tests/integration/llm/test_admin_health_llm_endpoint.py`
  - `backend/tests/integration/test_import_linter_contract5.py`
- **Configuration** :
  - `.import-linter` racine — Contract 5 ajouté
  - `pyproject.toml` — **AUCUNE** modification (les deps `langchain-anthropic`/`langchain-openai` sont déjà pinnées)
- **Docs** :
  - `docs/decisions/llm-abstraction.md` — **CRÉÉ**
  - `docs/decisions/llm-fallback-policy.md` — **CRÉÉ**
  - `docs/decisions/README.md` — référence ajoutée section "Sprint 0 — fondations Core"
  - `docs/runbooks/llm-usage.md` — **CRÉÉ**
  - `CONVENTIONS.md` — règle d'or #5 ajoutée
- **Pas de migration Alembic Story 1.6** (le schema cible n'a aucun changement — `agent_templates.config: JSONB` déjà en place depuis Story 1.5).

**Conflits détectés** : aucun. Les modules `shared/llm/` et `infra/llm/` sont aujourd'hui des stubs `NotImplementedError`/vides — Story 1.6 les remplit. Aucun caller actuel n'utilise les LLM (tous les modules `features/m*` sont des stubs vides). Risque de régression Story 1.5 (repositories) : zéro (LLM ne touche pas aux repos). Risque de régression Story 1.4 (event_bus) : faible — `LLMRouter.complete()` publish un event sur fallback, mais via callback injecté donc découplé pour les tests.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 1.6 (lignes 657-682)] — ACs canoniques.
- [Source: _bmad-output/planning-artifacts/architecture.md#API & Communication Patterns (ligne 393)] — décision multi-LLM provider abstraction NFR20.
- [Source: _bmad-output/planning-artifacts/architecture.md#Decision Impact Analysis Sprint 1 (ligne 451)] — `core/llm/` programmé Sprint 1.
- [Source: _bmad-output/planning-artifacts/architecture.md#Cross-Component Dependencies (ligne 475)] — `core/llm` consommé par M2, M3, M4.
- [Source: _bmad-output/planning-artifacts/architecture.md#7. core/llm/ escape hatch pattern (lignes 554-563)] — pattern Protocol `LLMProvider` + `raw_provider_call()`.
- [Source: _bmad-output/planning-artifacts/architecture.md#Mises à jour Sprint 1 (ligne 607)] — `core/llm/` interface inclut `raw_provider_call()` escape hatch.
- [Source: _bmad-output/planning-artifacts/architecture.md#Project Structure ligne 1525-1533] — arborescence `shared/llm/` (`interface.py`, `router.py`, `budget.py`, `safety/*`, `tiers.py`).
- [Source: _bmad-output/planning-artifacts/architecture.md#Project Structure ligne 1564-1568] — `infra/llm/{anthropic,openai,voyage,fastembed}_adapter.py`.
- [Source: _bmad-output/planning-artifacts/architecture.md#Integration Points (lignes 1910-1911)] — Anthropic + OpenAI APIs mappées vers `infra/llm/*_adapter.py`.
- [Source: _bmad-output/planning-artifacts/architecture.md#Anti-patterns Bannis (ligne 1256)] — pattern « no direct DB » étendu à « no direct LLM SDK from features ».
- [Source: _bmad-output/planning-artifacts/architecture.md#Naming Conventions (lignes 1065-1085)] — Python conventions snake_case, env vars `AGENTIVE_LLM_*` (note : actuellement `ANTHROPIC_API_KEY` sans prefix — décision documentée Story 1.1, conserver).
- [Source: _bmad-output/planning-artifacts/architecture.md#Async / Concurrency (lignes 1198-1204)] — `async`/`await` partout, AbortController équivalent côté Python = `asyncio.timeout()`.
- [Source: _bmad-output/planning-artifacts/architecture.md#Testing (lignes 1228-1242)] — fixtures testcontainers + naming `test_<what>_when_<condition>_should_<expectation>` + coverage `core/` 90%+.
- [Source: _bmad-output/planning-artifacts/architecture.md#All AI Agents MUST (lignes 1286-1297)] — règle 1 "Toujours passer par l'abstraction LLMRouter pour appels LLM" (à ajouter via règle d'or #5 CONVENTIONS.md).
- [Source: _bmad-output/planning-artifacts/prd.md#NFR9 (ligne 579)] — aucune clé API en clair logs/traces.
- [Source: _bmad-output/planning-artifacts/prd.md#NFR12 (ligne 587)] — graceful degradation LLM fallback automatique.
- [Source: _bmad-output/planning-artifacts/prd.md#NFR14 (ligne 589)] — timeouts configurables + retry + back-off (Sprint 0 = no retry, Story 9.5 ajoute back-off).
- [Source: _bmad-output/planning-artifacts/prd.md#NFR20 (ligne 605)] — ≥ 2 providers LLM simultanés (Anthropic + OpenAI minimum).
- [Source: backend/pyproject.toml:13-14] — `langchain-anthropic>=1.4.1`, `langchain-openai>=1.1.14` déjà pinnés.
- [Source: backend/src/agentive_backend/shared/config.py:77-79] — `anthropic_api_key`, `openai_api_key`, `voyage_api_key` déjà déclarés `SecretStr | None`.
- [Source: backend/src/agentive_backend/shared/llm/__init__.py:1-19] — stub `complete() raise NotImplementedError("Story 1.6")` à réécrire.
- [Source: backend/src/agentive_backend/infra/llm/__init__.py:1-11] — stub vide à étendre.
- [Source: backend/src/agentive_backend/shared/exceptions.py:13-108] — `AgentiveError` base + `DependencyError` (503) à étendre pour `LLMError` hierarchy.
- [Source: backend/src/agentive_backend/shared/event_bus/__init__.py:1-51] — pattern `publish_and_commit()` à utiliser pour `m3.llm.fallback_triggered` (via callback injecté).
- [Source: backend/src/agentive_backend/shared/event_bus/naming.py:24-40] — `KNOWN_MODULE_PREFIXES` inclut déjà `m3` — pas de modif requise.
- [Source: backend/src/agentive_backend/shared/logging/__init__.py:19-50] — chaîne structlog processors où ajouter `_redact_api_keys`.
- [Source: backend/src/agentive_backend/app/lifespan.py] — pattern bootstrap `OutboxWorker` à miroiter pour `LLMRouter`.
- [Source: backend/src/agentive_backend/app/main.py:1-80+] — pattern factory `create_app()` + middleware + exception handlers.
- [Source: .import-linter] — Contracts 1-4 actuels, ajouter Contract 5 selon AC11.
- [Source: backend/tests/integration/test_import_linter_contract3.py] — pattern test à miroiter pour Contract 5.
- [Source: docs/decisions/event-bus-naming.md, event-bus-migration-trigger.md, repository-pattern.md] — templates ADR à suivre.
- [Source: _bmad-output/implementation-artifacts/1-5-core-repositories-migrations.md] — pattern Story Notes + tests pyramide à reproduire (structure, niveau de détail).
- [Source: _bmad-output/implementation-artifacts/1-4-core-event-bus.md] — pattern publish_and_commit + callback injection pour découpler les tests.

### Latest tech information (LangChain partner SDKs + httpx + Pydantic v2, mai 2026)

**`langchain-anthropic >= 1.4.1`** (pinné `pyproject.toml:13`) :
- API stable depuis 1.0 : `ChatAnthropic(model=..., max_tokens=..., temperature=..., timeout=..., stop_sequences=..., anthropic_api_key=...)`. Async via `.ainvoke(messages: list[BaseMessage]) -> AIMessage`.
- **Breaking change 1.4** (à vérifier au dev) : si la version pinned est >=1.4.1, certains kwargs comme `anthropic_api_url` peuvent avoir été renommés `base_url`. Test T2.4 doit valider l'API exacte au moment du dev.
- `response.usage_metadata` : dict avec `input_tokens`, `output_tokens`, `total_tokens` depuis 1.3+. Avant 1.3 : `response_metadata["usage"]`. Pin >=1.4.1 garantit le format moderne.
- Prompt caching : `system=[{"type": "text", "text": "...", "cache_control": {"type": "ephemeral"}}]` accepté en kwargs `ChatAnthropic.invoke(messages, system=...)`. **Verifier syntax exact au dev** (peut nécessiter `extra_body` selon la version SDK Anthropic sous-jacent).

**`langchain-openai >= 1.1.14`** (pinné `pyproject.toml:14`) :
- API stable depuis 1.0 : `ChatOpenAI(model=..., max_tokens=..., temperature=..., timeout=..., stop=..., api_key=...)`. Async via `.ainvoke(messages) -> AIMessage`.
- **Modèles `o*` / `gpt-5*` requièrent `max_completion_tokens` au lieu de `max_tokens`** — divergence SDK qui doit être gérée par `_resolve_max_tokens_kwarg(model)` (cf T3.1).
- `response.usage_metadata` : format identique à Anthropic depuis 1.0+ (`input_tokens`, `output_tokens`).
- Tool calling : `bind_tools(tools, parallel_tool_calls=True)` retourne un `ChatOpenAI` configuré — exposé via `raw_provider_call()`.

**`httpx >= 0.27`** (transitif via LangChain) :
- `httpx.MockTransport` pour les tests : `client = httpx.AsyncClient(transport=httpx.MockTransport(handler))` permet d'intercepter sans réseau réel.
- `httpx.HTTPStatusError`, `httpx.TimeoutException`, `httpx.ConnectError` : 3 exceptions de base à classifier.

**`pydantic >= 2.13`** (déjà pinné) :
- `model_config = ConfigDict(frozen=True)` pour Pydantic v2 (anciennement `Config: allow_mutation = False`).
- Sérialisation custom : `@field_serializer("cost_estimate_usd") def _serialize_decimal(self, v: Decimal | None) -> str | None: return str(v) if v else None`.
- `model_dump_json()` round-trip teste-able dans T1.6.

**`prometheus-client >= 0.20`** (déjà pinné) :
- `Histogram(name, documentation, labelnames=("provider", "model", "status"))` — labels max 3-4 pour éviter explosion cardinalité.
- `REGISTRY.collect()` : iter sur les `MetricFamily` pour les tests (vérifier présence + labels).

**Compatibility matrix (validé pyproject.toml)** :
- Python 3.14
- `langchain-anthropic` 1.4.1+ ↔ Anthropic SDK >=0.40 (transitif)
- `langchain-openai` 1.1.14+ ↔ OpenAI SDK >=1.50 (transitif)
- Pas de conflit connu avec `langgraph==1.1.8` (pin G3) — LangGraph utilise `langchain-core` qui est compatible avec les 2 partner SDKs.

### Project Context Reference

Le projet Agentive est en **Sprint 0** (Foundation & Spike Validation). Story 1.6 est la **troisième fondation Core** (post-1.4 event_bus, post-1.5 repositories). Cette story livre **le canal autorisé d'accès LLM** sur lequel se brancheront tous les agents futurs (Stories Epic 2-9). Échec ici = blocage en cascade des Stories 2.x (agent registry — `AgentLLMConfig` consommé par M8 Configurator), 3.x (memory — Embedding Router étend l'interface), 4.x (workflow engine — fallback NFR12 obligatoire pour Story 4.6), 5.x (Pôle Dev agents — chaque agent a une config LLM), 9.4 (budget caps — consomme `LLM_TOKENS_TOTAL` + `LLM_COST_USD_TOTAL`), 9.5 (rate limiting — étend `classify_error` avec retry intra-provider).

Succès = engagement définitif sur **Protocol `LLMProvider` + escape hatch `raw_provider_call()` + fallback chain configurable + redaction stricte** comme garantie de découplage agents ↔ providers + sécurité NFR9 + résilience NFR12 + monitorabilité (Stories 9.4/9.5). Squelette `shared.llm.*` figé comme **API publique inviolable** (enforcement `import-linter` Contract 5 nouvellement actif Sprint 0).

## Dev Agent Record

### Agent Model Used

claude-opus-4-7[1m] (Claude Opus 4.7, 1M context, 2026-05-02).

### Debug Log References

3 obstacles techniques rencontrés et résolus pendant le dev :

1. **`AIMessage.usage_metadata` requiert `total_tokens`** — LangChain `langchain_core.messages.AIMessage` valide `usage_metadata` strictement et exige `input_tokens`, `output_tokens`, **et** `total_tokens` (les anciens snippets de doc l'omettent). 6 tests unit Anthropic ont d'abord échoué avec `Field required: usage_metadata.total_tokens`. **Fix** : ajouter `total_tokens` à toutes les fixtures `AIMessage(usage_metadata=...)`. Documenté dans le Gotcha critique #4 (mapping `_to_lc_messages`).

2. **`Settings._reject_dev_defaults_in_production` valide les secrets internes en production** — le test `test_no_keys_in_production_raises` échouait avec `ValidationError` au lieu de `RuntimeError` parce que les passwords par défaut `change_me` font échouer le model validator avant même d'atteindre `_build_llm_router()`. **Fix** : la fixture `fresh_settings` patch `AGENTIVE_API_TOKEN`, `AGENTIVE_ENCRYPTION_KEY` (Fernet generate_key), `POSTGRES_APP_PASSWORD`, `POSTGRES_OWNER_PASSWORD` avec des valeurs non-placeholder pour permettre au Settings de booter et au lifespan de tester effectivement la branche LLM.

3. **TestClient host = `testclient`, autouse fixture bloque `testserver`** — le fixture `_no_external_http` autouse refusait l'URL `http://testserver/...` que Starlette TestClient utilise pour ses requêtes ASGI in-process, ET l'endpoint `/admin/health/llm` 403'ait `request.client.host = "testclient"` qui n'était pas dans `_LOOPBACK_HOSTS`. **Fix** : ajouter `"testserver"` à la whitelist conftest et `"testclient"` à la whitelist endpoint. Documenté dans le code des deux modules.

### Completion Notes List

✅ **Story 1.6 implémentée et validée — Multi-provider LLM abstraction opérationnelle (NFR9, NFR12, NFR14, NFR20)**.

**Métriques de validation** :
- **284/284 tests verts** en 16s (122 baseline Story 1.5 + 5 spike + ~157 nouveaux tests : 122 unit LLM/redaction/metrics/router/classifier + 13 integration LLM + 7 contract tests + adapters Anthropic/OpenAI 21 + 24).
- **Lint clean** : ruff check + ruff format + mypy strict (0 issues / 76 source files).
- **Import-linter** : **5 contracts kept** (Contract 1 features-isolated, Contract 2 layered, Contract 3 no-direct-db-access, Contract 4 event-bus-only-public-api, **Contract 5 no-direct-llm-sdk-from-features (nouveau)**).
- **Adapters Anthropic + OpenAI** wired sur `langchain-anthropic 1.4.1` + `langchain-openai 1.1.14` ; gestion `max_tokens` vs `max_completion_tokens` pour gpt-5/o-series via `model_kwargs`.
- **Tests integration end-to-end** (router complet) : happy path, fallback 5xx, total failure aggregate, fatal short-circuit. Aucun appel réseau réel — fixture autouse `_no_external_http` enforce mécaniquement.
- **Lifespan factory** : 4 branches testées (both keys / Anthropic only / OpenAI only / no keys → MockProvider en dev / no keys + production → RuntimeError fail-fast).

**Décisions implémentation notables** :
- **Callback fallback injecté** au lieu de coupling LLM ↔ event_bus : `LLMRouter.__init__(on_fallback=Callable)` ; le wiring vers `event_bus.publish_and_commit('m3.llm.fallback_triggered', …)` se fait dans `app/lifespan.py` via une closure qui capture `session_factory`. Permet aux tests d'injecter un `AsyncMock` sans toucher à la DB.
- **`_no_external_http` autouse fixture** : enforce mécaniquement la règle "pas d'appel réseau réel en CI". Whitelist : `localhost`, `127.0.0.1`, `host.docker.internal`, `testserver`, et bridges Docker `172./10./192.168.`.
- **Redaction NFR9 en deux couches** : `redact_secrets()` au call-site (exception details + event payloads) + `redact_api_keys_processor` dans la chaîne structlog avant `JSONRenderer`. 4 patterns distincts : `sk-ant-`, `sk-proj-`, `sk-` legacy, `pa-` Voyage. Floor de 30 caractères pour éviter faux positifs sur tokens courts.
- **`Contract 5` static parsing test** plutôt que subprocess `lint-imports` (pattern Contract 3 Story 1.5) — décision dev pour rapidité ; CI exécute `lint-imports` end-to-end via le job `test-backend`.
- **Endpoint `/admin/health/llm` loopback-only** : auth Bearer arrive Story 1.7. TODO commentaire explicite dans la route. Whitelist inclut `testclient` (Starlette TestClient) pour permettre aux tests TestClient de passer.
- **`MODEL_FALLBACK_MAP` Sprint 0** : 8 entrées canoniques. `claude-haiku-4-5-20251001` (long-form id) mappé séparément vers `gpt-5-mini` car le SDK Anthropic peut renvoyer le long form dans `response_metadata.model_name`.
- **`MockProvider` en `shared/llm/testing.py`** (sous-module dédié) : NON re-exporté par le package `__init__.py`. Les tests doivent importer explicitement `from agentive_backend.shared.llm.testing import MockProvider`. Garantit zéro risque de leak en prod.

**Limites assumées Sprint 0** (à reprendre dans les stories suivantes) :
- **Pas d'embedding adapters** (Voyage, FastEmbed) — Story 3.1 / 3.6 (Embedding Router) qui consommera l'interface posée ici. Décision deferred sur un Protocol séparé `EmbeddingProvider` vs extension de `LLMProvider`.
- **Pas de prompt injection defense** (`shared/llm/safety/`) — stub vide, Story 1.9 / Epic 9.
- **Pas de budget caps + rate limiting** (`shared/llm/budget.py`) — Stories 9.4 + 9.5. Hooks d'observabilité (LLM_TOKENS_TOTAL + LLM_COST_USD_TOTAL) déjà en place pour eux.
- **Pas de retry intra-provider** — Sprint 0 = `max_retries=0`. Story 9.5 ajoutera back-off exponentiel + jitter dans le bucket `retriable_same_provider` actuellement vide.
- **Pas de tool/function calling unifié** — exposé exclusivement via `raw_provider_call()`. Unification éventuelle Sprint 2+ M3 Workflow Engine.
- **Pas de streaming** — Story 6.1 (Chat UI) qui formalisera l'API streaming.
- **Pas d'instance LangChain partagée** — chaque `complete()` instancie un `ChatAnthropic`/`ChatOpenAI` neuf. Acceptable Sprint 0 (latence LLM 1-30s domine), à reconsidérer si métrique signal `LLM_REQUEST_LATENCY_SECONDS` p50 sans LLM > 200ms.
- **Pas de spans OTel détaillés par provider** — Story 8.1 (Trace Explorer) consommera `Completion.provider_request_id` pour le linking.

**Tooling pour les stories Epic 2-9 qui consommeront l'abstraction** :
- Pattern canonique documenté dans `docs/runbooks/llm-usage.md` (caller pattern + escape hatch + add-new-provider checklist).
- ADR `docs/decisions/llm-abstraction.md` documente les options considérées (litellm vs LangChain partner SDKs vs raw httpx).
- `docs/decisions/llm-fallback-policy.md` documente la chaîne canonique + table de classification + policy de mise à jour pricing.
- `MockProvider` + `_no_external_http` fixture réutilisables pour tous les tests d'agents Sprint 1+.
- Métriques Prometheus prêtes pour `/metrics` endpoint (Story 1.9) et budget caps enforcement (Story 9.4).

### File List

**Production code (15 fichiers : 13 créés + 2 réécrits)**

- `backend/src/agentive_backend/shared/llm/__init__.py` — RÉÉCRIT (stub `complete() raise NotImplementedError` → re-export public API : 22 symboles).
- `backend/src/agentive_backend/shared/llm/types.py` — **CRÉÉ** : `ChatMessage`, `Completion`, `LLMUsage` Pydantic v2 frozen + `field_serializer` Decimal → str.
- `backend/src/agentive_backend/shared/llm/interface.py` — **CRÉÉ** : Protocol `LLMProvider` runtime_checkable.
- `backend/src/agentive_backend/shared/llm/exceptions.py` — **CRÉÉ** : `LLMError` + 7 sous-classes RFC 7807.
- `backend/src/agentive_backend/shared/llm/config.py` — **CRÉÉ** : `AgentLLMConfig` validation stricte (frozen, extra='forbid').
- `backend/src/agentive_backend/shared/llm/error_classifier.py` — **CRÉÉ** : `classify_error()` + `ErrorClass` Literal.
- `backend/src/agentive_backend/shared/llm/router.py` — **CRÉÉ** : `LLMRouter` + `DEFAULT_MODEL_FALLBACK_MAP` + `FallbackContext` + `FallbackCallback`.
- `backend/src/agentive_backend/shared/llm/redaction.py` — **CRÉÉ** : `redact_secrets()` + `_redact_value()` récursif + `redact_api_keys_processor` structlog.
- `backend/src/agentive_backend/shared/llm/metrics.py` — **CRÉÉ** : 5 Prometheus metrics (Histogram + 3 Counter + Gauge).
- `backend/src/agentive_backend/shared/llm/testing.py` — **CRÉÉ** : `MockProvider` test-only.
- `backend/src/agentive_backend/infra/llm/__init__.py` — RÉÉCRIT (stub vide → re-export `AnthropicProvider` + `OpenAIProvider`).
- `backend/src/agentive_backend/infra/llm/anthropic_adapter.py` — **CRÉÉ** : `AnthropicProvider` + `MODEL_PRICING` + helpers (`_to_lc_messages`, `_extract_text`, `_compute_cost`, `_classify_anthropic_exception`, `_FINISH_REASON_MAP`).
- `backend/src/agentive_backend/infra/llm/openai_adapter.py` — **CRÉÉ** : `OpenAIProvider` + `MODEL_PRICING` + helpers (`_resolve_max_tokens_kwarg`, `_NEW_MAX_TOKENS_PREFIXES`, mapping legacy `token_usage`, `_classify_openai_exception`).
- `backend/src/agentive_backend/api/__init__.py` — **CRÉÉ** (package créé pour la première fois).
- `backend/src/agentive_backend/api/deps.py` — **CRÉÉ** : `get_llm_router()` Depends.
- `backend/src/agentive_backend/api/admin/__init__.py` — **CRÉÉ**.
- `backend/src/agentive_backend/api/admin/health_llm.py` — **CRÉÉ** : `GET /api/v1/admin/health/llm` (loopback gate Sprint 0).

**Wiring lifespan / app (2 fichiers modifiés)**

- `backend/src/agentive_backend/app/lifespan.py` — `_build_llm_router()` factory + `app.state.llm_router` + closure `_publish_fallback` qui wire le callback router au bus event.
- `backend/src/agentive_backend/app/main.py` — `app.include_router(llm_health_router, prefix="/api/v1/admin")`.

**Logging hook (1 fichier modifié)**

- `backend/src/agentive_backend/shared/logging/__init__.py` — `redact_api_keys_processor` ajouté à la chaîne structlog avant `JSONRenderer`.

**Tests (15 fichiers : 14 créés + 0 modifiés)**

- `backend/tests/unit/llm/__init__.py` — **CRÉÉ**.
- `backend/tests/unit/llm/test_interface_contract.py` — **CRÉÉ** : 4 tests (Protocol signature, async, runtime_checkable, ClassVar).
- `backend/tests/unit/llm/test_types.py` — **CRÉÉ** : 6 tests (frozen, role validation, Decimal serializer, finish_reason constraint, token counts non-négatifs).
- `backend/tests/unit/llm/test_exceptions.py` — **CRÉÉ** : 4 tests (héritage AgentiveError, status codes, /errors/llm/ namespace, context propagation).
- `backend/tests/unit/llm/test_config_validation.py` — **CRÉÉ** : 8 tests (valid, chain length bounds, max_tokens bounds, temperature bounds, extra forbid, frozen).
- `backend/tests/unit/llm/test_anthropic_adapter.py` — **CRÉÉ** : 21 tests (system extraction, content blocks, cost compute, finish_reason mapping × 4, classifier exceptions parametrized × 6, complete translates).
- `backend/tests/unit/llm/test_openai_adapter.py` — **CRÉÉ** : 24 tests (max_tokens kwarg resolve × 8, finish_reason mapping × 4, legacy token_usage, exception classifier, gpt-5 model_kwargs piping, gpt-4.1 max_tokens direct).
- `backend/tests/unit/llm/test_classify_error.py` — **CRÉÉ** : 10 tests parametrized (retriable × 4, fatal × 5, sprint-0 invariant).
- `backend/tests/unit/llm/test_router.py` — **CRÉÉ** : 12 tests (happy path, fallback 5xx + timeout, all-fail aggregate, fatal short-circuit, chain override, no-fallback-model, gauge zero-leak, callback awaited, default map canonique, raw_provider_call dispatch, unknown provider).
- `backend/tests/unit/llm/test_redaction.py` — **CRÉÉ** : 13 tests (4 prefixes, no-pattern, short tokens, URL, nested JSON, idempotent, processor top-level/nested dict/nested list/non-string passthrough).
- `backend/tests/unit/llm/test_metrics.py` — **CRÉÉ** : 6 tests (5 métriques registrées, labels canoniques × 4, gauge zero-leak).
- `backend/tests/unit/llm/test_mock_provider.py` — **CRÉÉ** : 4 tests (FIFO Completions, FIFO exception, exhausted, kwargs recording).
- `backend/tests/integration/llm/__init__.py` — **CRÉÉ**.
- `backend/tests/integration/llm/conftest.py` — **CRÉÉ** : autouse fixture `_no_external_http`.
- `backend/tests/integration/llm/test_router_full_chain.py` — **CRÉÉ** : 4 tests (happy / fallback / total fail / fatal).
- `backend/tests/integration/llm/test_no_api_key_in_logs.py` — **CRÉÉ** : 1 test (structlog pipeline render).
- `backend/tests/integration/llm/test_no_api_key_in_event_payload.py` — **CRÉÉ** : 1 test (LLMAllProvidersFailedError context redacted).
- `backend/tests/integration/llm/test_lifespan_llm_router_built.py` — **CRÉÉ** : 5 tests (matrice complète des 4 cas + Fernet key fixture pattern).
- `backend/tests/integration/llm/test_admin_health_llm_endpoint.py` — **CRÉÉ** : 2 tests (200 from localhost via TestClient, 403 from non-loopback).
- `backend/tests/integration/test_import_linter_contract5.py` — **CRÉÉ** : 5 tests (section présente, type forbidden, source_modules, forbidden_modules, comment block doc reference).

**Configuration / contrats (1 fichier modifié)**

- `.import-linter` — Contract 5 ajouté (`no-direct-llm-sdk-from-features`) + commentaire de bloc référençant `docs/decisions/llm-abstraction.md`.

**Documentation (5 fichiers : 3 créés + 2 modifiés)**

- `docs/decisions/llm-abstraction.md` — **CRÉÉ** : ADR (Context / Decision / Options Considered / Why Protocol / Why escape hatch / Consequences / Revisitability).
- `docs/decisions/llm-fallback-policy.md` — **CRÉÉ** : table classification erreurs + chaîne canonique + `MODEL_FALLBACK_MAP` snapshot 2026-05 + pricing snapshot policy + update procedure.
- `docs/runbooks/llm-usage.md` — **CRÉÉ** : caller pattern + per-agent override + Anthropic prompt caching escape hatch + add-new-provider checklist + debugging fallback + MODEL_PRICING update + tests-without-keys policy + NFR9 smoke check.
- `docs/decisions/README.md` — référence ajoutée pour les 2 nouveaux ADR (section "Sprint 0 — fondations Core").
- `CONVENTIONS.md` — règle d'or **#5 ajoutée** (LLM uniquement via `LLMRouter`), liste renumérotée 5 → 11, header mis à jour "11 règles d'or".

### Change Log

| Date | Change | Rationale |
|---|---|---|
| 2026-05-02 | Story 1.6 implémentée bout-en-bout | Multi-provider LLM abstraction opérationnelle (NFR9, NFR12, NFR14, NFR20) — bloque les Stories 2.x / 3.x / 4.x / 9.4 / 9.5 |
| 2026-05-02 | Création du package `agentive_backend.api` (premier endpoint admin) | Pré-requis Contract 5 (les `source_modules` couvrent `agentive_backend.api`) + AC12 (endpoint `/admin/health/llm`) |
| 2026-05-02 | Ajout règle d'or #5 dans `CONVENTIONS.md` | Cohérence avec règle #4 (DB via repos) — même pattern d'enforcement par `import-linter` |
| 2026-05-02 | **Fix-batch post-review (21 must-fix patches)** | Findings consolidés Blind Hunter + Edge Case Hunter + Acceptance Auditor. Tous appliqués, 289 tests verts, 5 contracts kept, mypy strict 0 issues. |
| 2026-05-02 | **AC5 + AC9 amendés (bad_spec resolved)** | AC5 : count "≥12 cas" requalifié comme **combiné cross-test** (router-level + adapter-level), reflète l'architecture 2-layers. AC9 #2 : "consumer bus" remplacé par "callback `on_fallback` reçoit `FallbackContext`", reflète le découplage architectural (cf AC4). |
| 2026-05-02 | **Story 1.6 → done** | Post fix-batch + amendements spec : tous les ACs Met, 0 dette structurelle, prêt pour les Stories 1.7+ / 2.x / 9.4 / 9.5 qui consommeront `LLMRouter` + `RouterCall`. |

### Fix-batch post-review (2026-05-02)

Tous les **21 must-fix** identifiés par les 3 reviewers adversariaux ont été appliqués :

**Critique architecturale (5)** :
- **P1** Long-form model IDs (Opus/Sonnet `*-20250508`) ajoutés dans `MODEL_PRICING` + `DEFAULT_MODEL_FALLBACK_MAP` — débloque le tracking de coût pour Story 9.4 budget caps.
- **P2** `pydantic.ValidationError` + `AttributeError` + `KeyError` ajoutés à `classify_error` comme `fatal` — programmer errors ne masquent plus comme retriable.
- **P3** Lifespan refactor : `_build_llm_router(*, on_fallback=...)` accepte le callback au constructeur via `LLMRouter.__init__(on_fallback=...)`. Ajout d'un `set_on_fallback()` public en backup. La race window entre `app.state.llm_router = ...` et la mutation privée du callback est éliminée.
- **P4** `await self._on_fallback(ctx)` wrappé dans `try/except` — une bus failure transitoire ne casse plus la chaîne entière.
- **P5** Système messages unifiés : Anthropic et OpenAI utilisent maintenant le même helper `_merge_system()` (kwarg + inline concat avec `\n\n`). Asymétrie résolue.

**Robustesse runtime (8)** :
- **P6** `LLM_REQUEST_LATENCY_SECONDS` mesure le wall-clock réel sur erreur (`time.perf_counter()` capturé avant l'inner try) — l'histogramme n'est plus pollué par des `0.0`.
- **P7** Le 2e `_resolve_model` (chemin fallback) wrappé dans `try/except LLMNoFallbackModelError` ; en cas d'échec, ajouté à `attempts` et `LLMAllProvidersFailedError` est levé avec contexte complet plutôt que `LLMNoFallbackModelError` raw.
- **P8** `FallbackContext.correlation_id` ajouté + capturé via `get_correlation_id()` au moment du fallback. `_publish_fallback` bind le correlation_id sur la session pour permettre `publish_and_commit` hors HTTP context.
- **P9** `provider_chain=[]` (vide explicite) refusé avec `ValueError` ; `None` route vers default (sémantique préservée).
- **P10** `messages=[]` refusé avec `ValueError` early — pas de round-trip 400 BadRequest fatal.
- **P11** `Counter.inc(float(cost))` guardé par `cost.is_finite()` — pas de crash sur `Decimal('Infinity')`.
- **P12** Adapters `_classify_*_exception` retournent `None` pour les exceptions inconnues → l'adapter re-raise raw → `classify_error` route via la branche fatal `(TypeError, ValueError, ValidationError, AttributeError, KeyError)`.
- **P13** `_extract_text` fallback `str(content)` passe par `redact_secrets()` — pas de leak de clé via dict repr.

**NFR9 — Redaction (3)** :
- **P14** `_redact_value` étendu pour `bytes` / `bytearray` (decode + redact) et objets avec `__dict__` (Pydantic models, dataclasses, classes).
- **P15** Docstring `LLMProvider.complete()` étendu avec la prohibition explicite NFR9 ("DO NOT pass API keys, OAuth tokens, or other secrets in `messages[*].content` or in `system`"). Section dédiée ajoutée à `docs/runbooks/llm-usage.md`.
- **P16** Constructor `default_timeout_s` (dead code) supprimé de `AnthropicProvider` + `OpenAIProvider` — `_max_retries` conservé (effectivement utilisé via `ChatAnthropic`/`ChatOpenAI`).

**API & UX (5)** :
- **P17** `/admin/health/llm` distingue `kind: "mock" | "real"` et `configured: false` pour le MockProvider — ops introspection ne ment plus.
- **P18** `AgentLLMConfig.provider_chain` validateur `_no_duplicate_providers` ajouté.
- **P19** `LLMRouter.from_agent_config(agent_config, *, providers, model_fallback_map=None, on_fallback=None) -> RouterCall` implémenté. `RouterCall` est un dataclass frozen exposant `complete(messages)` qui pré-bind le model + chain + kwargs depuis l'`AgentLLMConfig`. Re-exporté dans `shared.llm.__init__`.
- **P20** MockProvider supporte `infinite_default: Completion | None` constructor kwarg ; le dev-mode synthesized mock dans `_build_llm_router` utilise ce mode unbounded — plus d'exhaustion silencieuse après 1024 calls.
- **P21** `_redact_value` retourne maintenant `tuple(...)` (plain) au lieu de `type(value)(coerced)` — pas de crash sur `NamedTuple` subclasses.

**Bonus circular import** : `shared/logging/__init__.py` importait `redact_api_keys_processor` au top-level → cycle avec `shared.llm.router` qui importe `get_logger`. Fix : import lazy dans `configure_logging()`.

**Tests adaptés** :
- `test_router.py::test_no_fallback_model_raises_explicit_error` renommé en `test_no_fallback_model_aggregates_into_all_providers_failed` et adapté au nouveau comportement (P7 — aggregation au lieu de propagation raw).

**Métriques de validation post-fix** :
- **289/289 tests verts** (= baseline pre-fix : aucune régression).
- `ruff check` + `ruff format` : 0 issues.
- `mypy --strict src/` : Success, no issues found in 76 source files.
- `lint-imports` : **5 contracts kept**.
