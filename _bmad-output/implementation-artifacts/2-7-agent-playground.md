# Story 2.7 : Agent Playground (test en isolation)

Status: ready-for-dev

> 🎯 **Septième story Epic 2 — Agent Platform.** Cette story livre le **Playground d'agent** : John peut tester un agent-template en isolation stricte (entrée manuelle alignée sur `input_contract`, sous-ensemble d'outils activés, exécution `LLMRouter.complete` + appels MCP sandboxés Story 2.6) et inspecter le résultat complet (prompt final résolu, output brut LLM, output parsé selon contrat, tokens consommés + coût estimé) **sans déclencher event_bus.publish ni memory_chunks writes**. Couvre **FR48**.
>
> **Cible architecturale** : backend `features/m7_playground/` nouveau (router + service) — endpoint `POST /api/v1/playground/agents/{template_id}/run` qui charge le template (Story 2.4 snapshot pattern) + filtre les tools assignés au sous-ensemble demandé + appelle `LLMRouter.complete` (`shared/llm/router.py`) + résout les tool calls via `infra/mcp/client.py::call_tool` (Story 2.6) + retourne un body riche (`prompt`, `raw_output`, `parsed_output`, `tokens`, `cost_estimate_usd`, `tool_invocations`). Frontend `features/playground/` nouveau — page `/config/playground/$templateId` (TanStack Router) + `AgentTesterForm` + `OutputInspector` + `ToolsActivatePanel`.
>
> **Pourquoi maintenant ?** Story 2.6 livre `call_tool` sandboxé qui rend l'exécution runtime sûre. Sans Playground, John doit construire un workflow complet (Story 4.x) pour tester un agent — itérer sur les prompts coûte un workflow_run + side effects DB + events sur le bus. Le Playground supprime cette friction Sprint 1.
>
> **Anti-scope strict** : (a) PAS de workflow_engine integration (Story 4.x — Playground bypasse `shared.event_bus` entièrement), (b) PAS de SSE/streaming (Sprint 1 = POST synchrone ; streaming chat Story 6.1), (c) PAS de save history / compare runs (Sprint 4+), (d) PAS d'agent_instance row créée (distinct Story 2.4 `instantiate_from_template` — Playground = snapshot éphémère en mémoire), (e) PAS de multi-step chains (Sprint 4+ — un Playground run = 1 LLM call + 0..N tool calls résolus dans cette même call), (f) PAS de Push Memory (FR20 — Story 3.5), (g) PAS de modification du template depuis Playground (Story 2.3 fait l'édition).

## Story

As John,
I want un Playground permettant de tester un agent avec entrée manuelle et inspection complète de l'output sans déclencher events/memory writes,
So that j'itère rapidement sur les prompts sans side-effects (FR48).

## Acceptance Criteria

**AC1 — Page Playground rendue avec formulaire input + tools subset**

**Given** un agent-template existant (`template_id` valide via Story 2.1/2.4)
**When** j'ouvre `/config/playground/{templateId}` dans le navigateur
**Then** un formulaire affiche les champs requis par le `input_contract` de l'agent (cf `config.input_contract.core` Story 2.2)
**And** les outils assignés à l'agent (Story 2.5 `GET /agents/templates/{id}/tools`) sont listés avec checkbox d'activation par défaut tous cochés
**And** je peux décocher un sous-ensemble pour la run en cours (UI-only, pas de PUT/persistence)

**AC2 — Isolation stricte (no events, no memory writes, no instance row)**

**Given** je lance l'exécution Playground via `POST /api/v1/playground/agents/{template_id}/run`
**When** le service exécute le LLM + tool calls
**Then** AUCUN `event_bus.publish` n'est appelé (vérifiable via monkeypatch + assert call_count == 0)
**And** AUCUNE row `agent_instances` n'est créée (`SELECT count(*) FROM agent_instances WHERE template_id = :tid` constant avant/après)
**And** AUCUNE row `memory_chunks` n'est insérée
**And** AUCUNE row `outbox_events` n'est insérée pour cette run
**And** AUCUN log structlog n'inclut `event_type=m{N}.{verb}` pour cette run (sauf l'event `m7.playground.run_completed` AC5 si décidé — voir Décisions exécution)
**And** le run est marqué `playground_only=true` dans les traces OTel (attribut span `playground.run=true`)

**AC3 — Résultat riche dans la réponse (prompt + output + tokens + cost)**

**Given** l'exécution se termine sans erreur
**When** la réponse `200 OK` revient
**Then** le body contient :
- `prompt_resolved: str` — le prompt final envoyé au LLM (system_prompt + variables résolues depuis input ; secrets redacted via P-05 Story 2.6 reuse)
- `raw_output: str` — output brut LLM (texte)
- `parsed_output: dict[str, Any] | None` — output parsé selon `agent_template.config.output_contract` (None si parsing échoue ; échec non-fatal)
- `tokens: {input_tokens: int, output_tokens: int}` — depuis `Completion.token_usage`
- `cost_estimate_usd: float | None` — depuis `Completion.cost_estimate_usd` (None si modèle absent du `DEFAULT_MODEL_FALLBACK_MAP`)
- `model_used: str` — modèle effectivement utilisé (peut différer du `agent_template.config.llm.model` en cas de fallback `Story 1.6`)
- `provider_used: str` — provider effectivement utilisé (anthropic/openai)
- `tool_invocations: list[{tool_name: str, arguments_redacted: dict, result_summary: str, duration_ms: int, status: 'success'|'error'}]` — chronologique
- `duration_ms_total: int` — wall-clock total (LLM + tool calls)

**AC4 — Re-run avec ajustements sans recharger la page**

**Given** un Playground run vient de se terminer
**When** je modifie l'input dans le formulaire et clique "Run again"
**Then** le state du formulaire est préservé (rien n'est reset, pas de page reload)
**And** un nouveau `POST /playground/...` part avec le payload modifié
**And** le résultat précédent est remplacé dans le panel `OutputInspector` (pas de stack/historique — anti-scope explicite)

**AC5 — Audit event minimal `m7.playground.run_completed` (decision tranchable)**

**Given** une run Playground terminée
**When** l'audit est traité
**Then** un event `m7.playground.run_completed` est publié via `event_bus.publish` (audit bypass pattern Story 2.1 P-15) avec payload `{template_id, tools_activated, duration_ms_total, tokens, cost_estimate_usd, model_used, status ∈ success/llm_error/tool_error}` — **PAS de prompt_resolved, raw_output, parsed_output, arguments** (secret-safety + volume).
**And** `git grep "audit-event bypass cleanup"` retourne **9 hits** post-Story 2.7 (8 baseline post-Story 2.6 + 1 nouveau pour `m7.playground.run_completed`).
**Décision à confirmer** : si John veut ZÉRO event (AC2 stricte interpretation), retirer AC5 + accepter D75 nouveau "Playground runs invisibles dans l'audit trail". Recommandation dev : garder AC5 (audit minimal sans secrets) — la valeur "compter les invocations Playground" pour FinOps Story 9.4 budget caps est élevée, et le contenu (prompt/output) reste hors du payload.

**AC6 — Erreurs propres + isolation préservée même en cas d'échec**

**Given** un Playground run qui échoue
**When** la cause est LLM provider error (rate limit, timeout réseau, malformed response)
**Then** la réponse `503` ou `502` (selon `Story 1.6` classification) + body RFC 7807 + le code se charge de NE PAS publier l'audit event AC5 (status=llm_error → soit on log via `_log.warning` soit on publish event avec status=llm_error — à clarifier T4.5)

**Given** un Playground run où un tool call échoue (MCPToolError ou timeout)
**When** la cause est un tool isError ou timeout sandbox (Story 2.6)
**Then** la réponse `200 OK` continue avec `tool_invocations[i].status='error'` (le LLM peut traiter l'erreur) ET le run se termine avec l'output LLM final (qui peut inclure une explication d'échec)
**Note**: les erreurs tool ne stoppent PAS le run — c'est au LLM de décider. Cohérent UX Playground "voir tout ce qui s'est passé".

**Given** un Playground run avec `template_id` inexistant
**When** la requête arrive
**Then** `404 NotFoundError` RFC 7807 + AUCUN audit event publié.

**AC7 — Smoke runtime + capture exhaustive (décision spec Story 2.5 P-04)**

**Given** la stack Docker dev tourne avec `AGENTIVE_ALLOW_MCP_REGISTRATION=true` (P-23 Story 2.5)
**When** John exécute le smoke prescrit en Dev Notes
**Then** capture exhaustive : (1) GET template Story 2.1 ; (2) POST tools/server + assign 2 tools ; (3) POST /playground/agents/{id}/run avec arguments + tool subset 1/2 → 200 + body complet (prompt_resolved + raw_output + parsed_output + tokens + cost + tool_invocations × 1) ; (4) re-run avec arguments modifiés → 200, distinct output ; (5) POST avec template_id ghost → 404 ; (6) DB SELECT count(*) FROM agent_instances + outbox_events WHERE event_type IN ('m2.agent_instance.created', 'm5.tool.invoked', 'm7.playground.run_completed') GROUP BY event_type → 0 m2.agent_instance.created + 0 m5.tool.invoked (Playground bypasse Story 2.6 audit aussi) + 1 m7.playground.run_completed.

**AC8 — Tests + lint verts (baseline + nouveaux)**

**Given** la baseline post-Story 2.6 = **559 backend + 100 frontend**
**When** la Story 2.7 est implémentée
**Then** ≥ 25 nouveaux tests : ≥ 15 backend (≥ 5 service unit + ≥ 4 e2e + ≥ 3 schemas + ≥ 2 isolation tests + 1 ToolInvocationLog event) + ≥ 10 frontend (≥ 4 PlaygroundPage + ≥ 3 OutputInspector + ≥ 3 hooks utilities)
**And** baseline 559 + 100 strictement préservée (0 régression)
**And** `make lint` (ruff + mypy + eslint + tsc) vert
**And** smoke runtime AC7 capturé en Completion Notes

---

## Pré-requis

> **Vérifier avant T0**

- Story 2.6 done ✅ (commit Cluster C `f1f5ed0` sur staging). `infra/mcp/client.py::call_tool` opérationnel + admin-gate `AGENTIVE_ALLOW_MCP_REGISTRATION`.
- Story 2.4 done ✅ — `agent_templates.config.input_contract` + `output_contract` accessibles.
- Story 2.5 done ✅ — `GET /api/v1/agents/templates/{id}/tools` retourne assigned tools avec `input_schema`.
- Story 1.6 done ✅ — `shared/llm/router.py::LLMRouter.complete(messages)` retourne `Completion(content, token_usage, cost_estimate_usd, model_used, provider_used)` + fallback automatique.
- Story 2.3 done ✅ — page `/config/agents/$templateId` route TanStack existante (la page playground réutilise le même `app/routes/config/` namespace).
- Pas de table DB nouvelle requise.
- Pas de dépendance pip nouvelle (LLMRouter + call_tool existants).
- Pas de nouvelle migration Alembic.
- Pattern audit-event bypass `event_bus.publish_and_commit('m7.playground.run_completed', ...)` avec TODO Story 9.1 cleanup sur 1 ligne (9 hits attendus post-2.7).
- Pattern `_redact_arguments` Story 2.6 P-05 réutilisé pour redacter les `arguments` des tool_invocations dans la réponse + audit payload.

---

## Tasks / Subtasks

- [ ] **T0 — Pré-flight checks**
  - [ ] T0.1 Lire `features/m2_agent_registry/service.py::instantiate_from_template` pour comprendre le snapshot pattern Story 2.4.
  - [ ] T0.2 Lire `features/m5_tool_hub/service.py::invoke_tool` pour comprendre le pattern de tool execution Story 2.6.
  - [ ] T0.3 Lire `shared/llm/router.py::LLMRouter.complete` signature + `Completion` shape (`shared/llm/types.py`).
  - [ ] T0.4 Vérifier baseline tests : `make test` → 559 backend + 100 frontend, 0 failure.

- [ ] **T1 — Backend : `features/m7_playground/__init__.py` + structure**
  - [ ] T1.1 Créer dossier `features/m7_playground/` avec `__init__.py` exportant `router` + `PlaygroundService`.
  - [ ] T1.2 Module dépendances declarées en docstring : `core/event_bus` JAMAIS importé (anti-scope strict — sauf pour AC5 audit event minimal via pattern bypass) ; `shared/repositories.MemoryChunkRepo` JAMAIS importé.

- [ ] **T2 — Schemas Pydantic (T2.1-T2.3)**
  - [ ] T2.1 `RunPlaygroundRequest` : `arguments: dict[str, Any]` + `enabled_tool_ids: list[UUID] | None` (None = tous les assigned tools activés) + `extra="forbid"` + `timeout_seconds: float = Field(default=30.0, gt=0, le=120)`.
  - [ ] T2.2 `ToolInvocationLog` : `tool_id, tool_name, server_id, arguments_redacted, result_summary, duration_ms, status: Literal["success", "error", "timeout"]`.
  - [ ] T2.3 `RunPlaygroundResponse` : tous les champs AC3 + `tool_invocations: list[ToolInvocationLog]` + `extra="ignore"`.
  - [ ] T2.4 Tests `tests/unit/m7_playground/test_schemas.py` ≥ 3 tests.

- [ ] **T3 — Event `PlaygroundRunCompletedEvent` (T3.1-T3.2, si AC5 confirmé)**
  - [ ] T3.1 Créer `shared/contracts/events/playground_events.py` avec `PlaygroundRunCompletedEvent(event_type="m7.playground.run_completed", template_id, tools_activated, duration_ms_total, input_tokens, output_tokens, cost_estimate_usd, model_used, status: Literal["success", "llm_error", "tool_error"], actor="system", tenant_id)`.
  - [ ] T3.2 Export dans barrel `shared/contracts/events/__init__.py`.
  - [ ] T3.3 Tests unit `tests/unit/shared/contracts/test_playground_events.py` ≥ 3 tests.

- [ ] **T4 — Service `PlaygroundService` (T4.1-T4.6)**
  - [ ] T4.1 Constructeur : `template_repo`, `tool_repo`, `assignment_repo` (AgentTemplateToolRepo), `llm_router: LLMRouter`. Pas de `instance_repo` (anti-scope AC2). Pas de `memory_chunk_repo` (anti-scope).
  - [ ] T4.2 Méthode `async def run(*, template_id, arguments, enabled_tool_ids, timeout_seconds, tenant_id=None) -> RunPlaygroundResponse` :
    - Charge template via `template_repo.get_by_id_in_session` (404 si missing).
    - Charge tools assignés via `assignment_repo.list_by_template_in_session`.
    - Filtre par `enabled_tool_ids` (si None : tous activés).
    - Construit le snapshot config-from-template (réutilise pattern Story 2.4 `instantiate_from_template` MAIS sans INSERT agent_instance — copie en mémoire uniquement).
    - Résout le `prompt_resolved` = `template.config.system_prompt` + variable substitution depuis `arguments` (réutilise helper Story 2.2 si existe, sinon `str.format_map` simple).
    - Appelle `llm_router.complete(messages=[{role: system, content: prompt_resolved}, {role: user, content: json.dumps(arguments)}])` avec timeout.
    - Si le LLM répond avec tool_calls → résout via `call_tool` pour chaque (Story 2.6) → re-call LLM avec tool results. **Sprint 1 : 1 itération max** (anti-scope multi-step). Si > 1 tool_calls demandés, log warning + tronquer à 1.
    - Parse output via `output_contract` (best-effort, JSON.loads + Pydantic ; échec → `parsed_output=None`).
    - Construit `RunPlaygroundResponse` avec tous les champs AC3.
  - [ ] T4.3 Publish event AC5 `m7.playground.run_completed` via pattern bypass (single-tx fresh session — pas dans `template_repo.with_tenant` car AC2 isolation). TODO Story 9.1 cleanup sur 1 ligne (9e hit).
  - [ ] T4.4 OTel : marquer le span `playground.run=true` via `from opentelemetry import trace; current_span = trace.get_current_span(); current_span.set_attribute("playground.run", True)` (best-effort si OTel n'est pas wired Sprint 1 — Story 1.9 a posé l'infra structlog mais pas OTel formel).
  - [ ] T4.5 Erreur handling : LLM error → DependencyError 503 + audit event `status=llm_error` ; Tool error → continue + log dans `tool_invocations[].status='error'` (run continue, output final retourne avec tool error visible) ; Template inexistant → NotFoundError 404, PAS d'audit event publié.
  - [ ] T4.6 Tests unit `tests/unit/m7_playground/test_service.py` ≥ 5 tests :
    - happy path (LLM mocked → 1 tool call mocked → output parsed)
    - isolation : `event_bus.publish` count == 1 (l'audit final uniquement, pas plus)
    - isolation : aucun `agent_instances` INSERT (assert via repo mock)
    - template inexistant → NotFoundError 404 sans audit
    - tool error → run continue + tool_invocations[i].status='error'

- [ ] **T5 — Router HTTP (T5.1-T5.3)**
  - [ ] T5.1 `router.py` avec `POST /api/v1/playground/agents/{template_id}/run` → `RunPlaygroundResponse`. Gated par `AGENTIVE_ALLOW_MCP_REGISTRATION` (P-23 cohérent : le Playground appelle `call_tool` aussi, surface RCE/SSRF identique).
  - [ ] T5.2 `_build_service` factory pattern Story 2.5 P-09 (LLMRouter récupéré via `request.app.state.llm_router` Story 1.6).
  - [ ] T5.3 App main `include_router(playground_router, prefix="/api/v1")`.

- [ ] **T6 — Tests intégration backend (T6.1-T6.3)**
  - [ ] T6.1 `tests/integration/m7_playground/conftest.py` re-export `make_e2e_app` Story 2.5 amend (étendu avec m7 router).
  - [ ] T6.2 `tests/integration/m7_playground/test_playground_e2e.py` ≥ 4 tests :
    - happy path POST run → 200 + body shape AC3 (LLM mocked via app.state.llm_router monkeypatch).
    - 404 template inexistant.
    - 403 si AGENTIVE_ALLOW_MCP_REGISTRATION=false.
    - isolation atomicité : assert 0 row `agent_instances`, 1 outbox event `m7.playground.run_completed` (status=success), 0 `m2.agent_instance.created`, 0 `m5.tool.invoked` (Playground bypass).
  - [ ] T6.3 Tests doivent monkeypatch `LLMRouter.complete` via `app.state.llm_router.complete = AsyncMock(...)` pour ne pas appeler de provider réel.

- [ ] **T7 — Frontend `features/playground/` nouveau (T7.1-T7.8)**
  - [ ] T7.1 Créer `frontend/src/features/playground/` avec barrel `index.ts`.
  - [ ] T7.2 `types.ts` : `RunPlaygroundRequest`, `RunPlaygroundResponse`, `ToolInvocationLog` (miroir backend Pydantic schemas).
  - [ ] T7.3 `api.ts` : `runPlayground(templateId, request)` fonction wrappant POST.
  - [ ] T7.4 `hooks.ts` : `usePlaygroundRun(templateId)` (TanStack Query useMutation — pas de cache, chaque clic relance).
  - [ ] T7.5 `PlaygroundPage.tsx` : assemble le layout (split view : form à gauche, output à droite).
  - [ ] T7.6 `AgentTesterForm.tsx` : form input alignée sur `input_contract.core` (text inputs simples Sprint 1 — pas de JSON Schema form builder, défer Sprint 4+) + checkboxes "Tools activés" depuis `useAgentTools(templateId)` Story 2.5.
  - [ ] T7.7 `OutputInspector.tsx` : tabs (Prompt | Raw Output | Parsed Output | Tool Invocations | Tokens & Cost). Code-syntaxed display pour Prompt + Raw + Parsed (pre/code pour Sprint 1, pas de syntax highlighting — défer Sprint 4+).
  - [ ] T7.8 Route TanStack Router : `app/routes/config/playground/$templateId.tsx`.

- [ ] **T8 — Tests frontend (T8.1-T8.4)**
  - [ ] T8.1 `PlaygroundPage.test.tsx` ≥ 4 tests : empty state (pas encore lancé) ; loading state (mutation pending) ; success state (output displayed) ; error state (toast + form preserved AC4).
  - [ ] T8.2 `AgentTesterForm.test.tsx` ≥ 3 tests : input rendering depuis input_contract ; tool checkboxes ; submit handler.
  - [ ] T8.3 `OutputInspector.test.tsx` ≥ 3 tests : tab switching ; tokens display ; tool invocations chronological order.

- [ ] **T9 — Sidebar + route ajoutée**
  - [ ] T9.1 Étendre la sidebar config (Story 1.8 pattern) avec entrée "Playground" → `/config/playground` (page liste templates) — OU accès depuis la page template `$templateId.tsx` via un bouton "Tester dans Playground" → `/config/playground/{templateId}`. Recommandation : bouton dans la page template (UX-DR §"contextuels").

- [ ] **T10 — Documentation + sprint-status**
  - [ ] T10.1 sprint-status.yaml : `2-7-agent-playground: backlog → ready-for-dev` (auto via workflow).
  - [ ] T10.2 (Post-dev) Completion Notes avec capture smoke AC7.

---

## Dev Notes

### Décisions techniques d'implémentation

1. **Snapshot from template, PAS d'agent_instance row** — Playground réutilise la logique de snapshot Story 2.4 (`template_id, template_version, name, archetype, config`) mais en MÉMOIRE uniquement. Aucun INSERT `agent_instances`. Le Trace Explorer (Story 8.x) verra ces runs uniquement via `m7.playground.run_completed` audit event.

2. **Single-step Sprint 1** — un Playground run = exactement 1 LLM call principal + 0..1 itération tool_calls. Le LLM peut demander plusieurs tool calls dans une même réponse, mais le service n'itère PAS pour re-call le LLM avec les résultats puis re-call les outils en chaîne (multi-step workflow). Anti-scope strict : workflow_engine Story 4.x. Documenter en réponse `tool_invocations` qui est résolu (max 5 tool calls par run, cap dur).

3. **Isolation via PAS d'import** — la garantie AC2 vient de la structure : `PlaygroundService` n'importe PAS `shared.event_bus.publish` directement (sauf l'unique helper d'audit AC5 single-line), n'importe PAS `MemoryChunkRepo`, n'importe PAS `instance_repo`. Cette discipline est vérifiable via `import-linter` Contract 3 (cf Story 1.5).

4. **AC5 audit event minimal** — décision tranchable : si John refuse l'event, retirer T3 + T4.3 + modifier AC2 pour dire "vraiment 0 event" + ajouter D75 défer "Playground runs invisibles audit". Recommandation dev : garder l'event (FinOps value, secrets-safe car payload limité aux métriques).

5. **`call_tool` réutilisé** — pas de nouveau code pour l'exécution tool. Le service appelle directement `infra.mcp.client.call_tool(transport, connection_config, tool_name, arguments, timeout, backend)` avec `backend` résolu via `app.state.mcp_sandbox_backend` (P-03 Story 2.6 pattern). Les events `m5.tool.invoked` NE doivent PAS être publiés depuis le Playground — donc PlaygroundService appelle `call_tool` BAS-NIVEAU sans passer par `ToolHubService.invoke_tool` (qui publie le m5 event). Anti-scope strict AC2.

6. **`enabled_tool_ids` filter logic** — si client envoie `enabled_tool_ids=[tool_a]`, mais que `tool_a` n'est PAS assigné au template, retourner 422 (cohérent AC1 + REST). Si client envoie `enabled_tool_ids=None` (omis), tous les assigned tools sont activés.

7. **Prompt resolution simple** — `str.format_map(arguments)` pour Sprint 1. Variables dans `system_prompt` sont notées `{key}` matching `arguments` keys. Si une variable n'est pas trouvée dans arguments, `KeyError` → 422 ValidationError avec détail. Pas de Jinja2 / pas de Mustache Sprint 1.

8. **Argument input UX Sprint 1** — formulaire texte simple : un `<textarea>` JSON éditeur (lint syntaxe via try/catch JSON.parse). Pas de form builder dynamique depuis `input_contract.core` JSON Schema Sprint 1 (D76 défer). Justification : `input_contract.core` Sprint 1 est `dict[str, str]` (variable name → variable type description) — pas un vrai JSON Schema. Form builder visuel arrivera Story 4.x quand `input_contract` aura une structure formelle.

9. **`Completion.cost_estimate_usd: Decimal | None`** — sérialiser via `field_serializer` Pydantic (`Decimal → float`) pour le JSON output. Si None (modèle absent du fallback map), retourner `null` côté JSON.

10. **OTel attribute** — `playground.run=true` posé sur le span actuel. Si OpenTelemetry n'est pas activé runtime Sprint 1 (Story 1.9 a installé l'infra mais pas le tracer provider), le `get_current_span()` retourne `INVALID_SPAN` qui accepte `.set_attribute` no-op. Best-effort.

11. **LLM message construction** — pour Sprint 1, message system = `prompt_resolved`, message user = `json.dumps(arguments)`. Pour les outils, le LLMRouter Sprint 1 ne gère PAS le format tool_use Anthropic / OpenAI function-calling formel. Anti-scope Story 4.x. **Conséquence importante** : Sprint 1 = le LLM ne sait PAS appeler de tools structurellement, le Playground retourne `tool_invocations=[]` UNLESS le LLM répond avec une réponse texte qui DEMANDE explicitement un tool (alors le service parse `[CALL_TOOL: name(args)]` syntaxe ad-hoc). **À simplifier Sprint 1** : ignorer tool calls dans la première itération ; T4.2 step "résout tool calls" → no-op Sprint 1, retourner `tool_invocations=[]`. La structure `tool_invocations` reste dans la réponse pour Story 4.x.

12. **Frontend output split view** — Sprint 1 : split horizontal simple via Tailwind grid `grid grid-cols-2`. Resize handle défer (D77 — Sprint 2+).

13. **JSON output handling** — `parsed_output: dict | None` — si le LLM retourne du markdown / texte plat, parsing JSON échoue, `parsed_output=None`. Le `raw_output` reste affiché. UX : un badge "parsing failed" affiché dans `OutputInspector` tab "Parsed Output".

14. **Pas de history persistence** — Le `OutputInspector` affiche UNIQUEMENT le dernier run. Pas de localStorage / pas de DB. Cohérent AC4 + anti-scope strict.

15. **Path TanStack Router** — `app/routes/config/playground/$templateId.tsx` (segment dynamique). La page liste est `app/routes/config/playground/index.tsx` (peut afficher juste "Choisissez un template" Sprint 1).

### Anti-scope strict (Story 2.7)

- ❌ **Workflow engine integration** — Story 4.x.
- ❌ **Multi-step tool chains** — Sprint 1 = 1 LLM call max (tool calls effectivement no-op Sprint 1, structure prête pour Story 4.x).
- ❌ **Save run history** — D78 défer Sprint 4+.
- ❌ **Compare runs side-by-side** — D79 défer Sprint 4+.
- ❌ **SSE streaming** — Story 6.1 chat UI streaming.
- ❌ **Push Memory** — FR20 Story 3.5.
- ❌ **Form builder dynamique depuis input_contract** — D76 défer Story 4.x.
- ❌ **Resize handle split view** — D77 défer Sprint 2+.
- ❌ **`m5.tool.invoked` published from Playground** — Anti-scope AC2 strict. PlaygroundService bypass `ToolHubService.invoke_tool`, call `call_tool` directement.
- ❌ **Tenant_id Sprint 1** — multi-tenant Story 12 défer.

### Nouveaux defer attendus Story 2.7 (D75+)

- **D75** — Playground runs sans aucun event audit (si AC5 retiré). Tradeoff documenté.
- **D76** — Form builder dynamique depuis `input_contract` JSON Schema → Story 4.x quand contracts formels.
- **D77** — Split view resize handle frontend → Sprint 2+.
- **D78** — Save run history (DB table `playground_runs`) → Sprint 4+.
- **D79** — Compare 2 runs side-by-side → Sprint 4+.
- **D80** — LLM tool_use formel (Anthropic tool_use / OpenAI function_calling) → Story 4.x.
- **D81** — Syntax highlighting frontend code blocks (Shiki / Prism) → Sprint 4+.
- **D82** — Export Playground run as JSON / Markdown → Sprint 4+.

### Pattern audit-event bypass — placement TODO

Cohérent décision Story 2.5 P-15 + Story 2.6 P-24 : le TODO doit être sur 1 ligne **adjacente au `publish(...)`** call. `git grep "audit-event bypass cleanup"` doit retourner **9 hits** post-Story 2.7 (8 baseline + 1 nouveau dans `features/m7_playground/service.py` pour `m7.playground.run_completed`).

### Smoke Runtime — séquence prescrite (AC7)

```bash
TOKEN="change_me"
BASE="http://localhost:8000/api/v1"
SUFFIX=$$

# Setup
docker compose up -d
docker compose exec backend uv run alembic upgrade head

# Step 1: create template (Story 2.1)
TEMPLATE=$(curl -sS -X POST "$BASE/agents/templates" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "{\"archetype\":\"producteur\",\"name\":\"smoke-27-prod-$SUFFIX\"}")
TEMPLATE_ID=$(echo "$TEMPLATE" | python3 -c 'import sys,json; print(json.load(sys.stdin)["template_id"])')

# Step 2: register MCP server + assign 2 tools (Story 2.5/2.6)
SERVER=$(curl -sS -X POST "$BASE/tools/servers" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "{\"name\":\"smoke-27-mcp-$SUFFIX\",\"transport\":\"stdio\",\"connection_config\":{\"command\":\"python\",\"args\":[\"-m\",\"tests.fixtures.mcp_mock_server\"]}}")
SERVER_ID=$(echo "$SERVER" | python3 -c 'import sys,json; print(json.load(sys.stdin)["server_id"])')
TOOL_ECHO=$(echo "$SERVER" | python3 -c 'import sys,json; print([t["tool_id"] for t in json.load(sys.stdin)["tools"] if t["name"]=="echo"][0])')
TOOL_ADD=$(echo "$SERVER" | python3 -c 'import sys,json; print([t["tool_id"] for t in json.load(sys.stdin)["tools"] if t["name"]=="add"][0])')
curl -sS -X POST "$BASE/agents/templates/$TEMPLATE_ID/tools" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "{\"tool_ids\":[\"$TOOL_ECHO\",\"$TOOL_ADD\"]}"

# Step 3: Playground run avec subset 1/2 tools
RUN=$(curl -sS -X POST "$BASE/playground/agents/$TEMPLATE_ID/run" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "{\"arguments\":{\"topic\":\"smoke test\"},\"enabled_tool_ids\":[\"$TOOL_ECHO\"]}")
echo "RUN: $RUN" | python3 -m json.tool

# Step 4: re-run avec arguments modifiés
RUN2=$(curl -sS -X POST "$BASE/playground/agents/$TEMPLATE_ID/run" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "{\"arguments\":{\"topic\":\"different smoke\"},\"enabled_tool_ids\":[\"$TOOL_ECHO\"]}")
echo "RUN2 distinct: $(echo $RUN2 | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"raw_output\"])') vs $(echo $RUN | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"raw_output\"])')"

# Step 5: ghost template_id → 404
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$BASE/playground/agents/00000000-0000-0000-0000-000000000000/run" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "{\"arguments\":{}}"

# Step 6: DB isolation verification
docker compose exec -T db psql -U agentive_owner -d agentive -c "SELECT event_type, count(*) FROM outbox_events WHERE created_at > NOW() - INTERVAL '5 minutes' AND event_type IN ('m2.agent_instance.created', 'm5.tool.invoked', 'm7.playground.run_completed') GROUP BY event_type ORDER BY event_type;"
# Expected: m7.playground.run_completed | 2 (steps 3+4)
#           m2.agent_instance.created   | 0
#           m5.tool.invoked              | 0  (Playground bypass)
```

---

## File List (post-implementation, target)

**Backend NEW**
- `backend/src/agentive_backend/features/m7_playground/__init__.py` (T1)
- `backend/src/agentive_backend/features/m7_playground/router.py` (T5)
- `backend/src/agentive_backend/features/m7_playground/service.py` (T4)
- `backend/src/agentive_backend/features/m7_playground/schemas.py` (T2)
- `backend/src/agentive_backend/shared/contracts/events/playground_events.py` (T3, si AC5 confirmé)
- `backend/tests/unit/m7_playground/__init__.py`
- `backend/tests/unit/m7_playground/test_service.py` (T4.6)
- `backend/tests/unit/m7_playground/test_schemas.py` (T2.4)
- `backend/tests/integration/m7_playground/__init__.py`
- `backend/tests/integration/m7_playground/conftest.py` (T6.1)
- `backend/tests/integration/m7_playground/test_playground_e2e.py` (T6.2)
- `backend/tests/unit/shared/contracts/test_playground_events.py` (T3.3)

**Backend MODIFIED**
- `backend/src/agentive_backend/app/main.py` (T5.3) — `include_router(playground_router, prefix="/api/v1")`.
- `backend/src/agentive_backend/shared/contracts/events/__init__.py` (T3.2) — barrel export `PlaygroundRunCompletedEvent`.
- `backend/tests/integration/m2_agent_registry/conftest.py` (T6.1) — `make_e2e_app` étendu pour inclure m7 router (pattern B-01 Story 2.5 DRY).

**Frontend NEW**
- `frontend/src/features/playground/types.ts` (T7.2)
- `frontend/src/features/playground/api.ts` (T7.3)
- `frontend/src/features/playground/hooks.ts` (T7.4)
- `frontend/src/features/playground/index.ts` (T7.1 barrel)
- `frontend/src/features/playground/PlaygroundPage.tsx` (T7.5)
- `frontend/src/features/playground/AgentTesterForm.tsx` (T7.6)
- `frontend/src/features/playground/OutputInspector.tsx` (T7.7)
- `frontend/src/features/playground/PlaygroundPage.test.tsx` (T8.1)
- `frontend/src/features/playground/AgentTesterForm.test.tsx` (T8.2)
- `frontend/src/features/playground/OutputInspector.test.tsx` (T8.3)
- `frontend/src/app/routes/config/playground/$templateId.tsx` (T7.8)
- `frontend/src/app/routes/config/playground/index.tsx` (T7.8 placeholder list page)

**Frontend MODIFIED**
- `frontend/src/app/routes/config/agents/$templateId.tsx` (T9.1) — bouton "Tester dans Playground" lien vers `/config/playground/{templateId}`.
- `frontend/src/app/routes/config/index.tsx` (T9.1) — entrée sidebar "Playground" optionnelle.

**Story spec**
- `_bmad-output/implementation-artifacts/2-7-agent-playground.md` (cette story, Status: ready-for-dev).
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — bump 2-7 backlog → ready-for-dev + ligne récap.

## Dev Agent Record

### Agent Model Used

(à remplir lors de l'implémentation)

### Debug Log References

(à remplir lors de l'implémentation)

### Completion Notes List

(à remplir lors de l'implémentation — inclure capture smoke runtime AC7 exhaustive)

## Senior Developer Review

(à remplir post-implémentation via `/bmad-code-review`)

## Change Log

| Date | Author | Description |
|------|--------|-------------|
| 2026-05-11 | bmad-create-story (Claude Opus 4.7 1M ctx) | Initial story creation — context engine pass complet : Epic 2 + Story 2.7 AC + AR Playground architecture L1496/L1694 + FR48 + LLMRouter.complete signature + Story 2.4 snapshot pattern + Story 2.6 call_tool reuse + 15 décisions techniques + AC5 audit event tranchable + 8 nouveaux defer D75-D82. Ready for dev-story implementation. |
