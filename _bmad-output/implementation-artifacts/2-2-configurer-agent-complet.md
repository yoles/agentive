# Story 2.2 : Configurer l'identité, le prompt, les contrats élastiques, le LLM et la politique d'erreur

Status: Review

> 🎯 **Deuxième story Epic 2 — Agent Platform.** Cette story pose le **endpoint de configuration profonde** d'un agent-template (`PUT /api/v1/agents/templates/{id}`), active le **versioning prompts** (table `prompts` insertion à chaque édition), introduit le **wrapping anti-prompt-injection** AR44 (`<user_input>...</user_input>` / `<tool_output>...</tool_output>`), et ajoute la **politique d'erreur** déclarative (retry exponential backoff NFR14). Côté UI, livre une **page d'édition expert minimale** (`/config/agents/{templateId}`) — formulaire flat sans wizard, sans accordéons stylés (Story 2.3 ajoutera la dichotomie Wizard/Expert).
>
> **Pré-requis hérités (déjà disponibles — NE PAS recréer) :**
>
> - **Table `agent_templates`** (Story 1.5 + amendement Story 2.1 `NULLS NOT DISTINCT`) : `id`, `name`, `archetype`, `version INT default 1`, `config JSONB`, `created_at`, `tenant_id`. UNIQUE `(name, version, tenant_id)`. ⚠️ Pas de colonne `updated_at` Sprint 1 — l'horodatage de modification se déduit de `prompts.created_at` du dernier insert pour ce `agent_template_id`.
> - **Table `prompts`** (Story 1.5, `infra/db/models.py:241-261`) : `id UUID`, `agent_template_id UUID FK CASCADE`, `version INT`, `content TEXT`, `created_at`, `tenant_id`. UNIQUE `(agent_template_id, version)` via contrainte `uq_prompt_template_version`. **Schéma volontairement minimal Sprint 1** — pas de `parent_version`, `is_active`, ni `metadata` JSONB (architecture H2 décrit l'état cible Sprint 2+, mais Sprint 1 utilise la version actuelle telle quelle ; cf §"Décision schéma prompts" en Dev Notes).
> - **`AgentTemplateRepo`** (Story 1.5 + Story 2.1) : `agent_repo.py:24-114` expose déjà `get_by_id`, `get_by_name_version`, `create`, `create_in_session` (pattern atomicité P-02). **Story 2.2 doit ajouter `update_in_session`** suivant le même pattern (transaction de la session caller, IntegrityError → ConflictError, refresh).
> - **`PromptRepo`** (Story 1.5) : `prompt_repo.py:18-58` expose `get_by_id`, `get_by_template_version`, `create`. **Story 2.2 doit ajouter `create_in_session`** (idem pattern atomicité — actuellement `create` ouvre sa propre transaction, incompatible avec composition atomique).
> - **Event Bus** (Story 1.4) : `event_bus.publish_and_commit(event_type, payload, session)` opérationnel + `OutboxEvent` table (`models.py:269-284`).
> - **Module M2 `agent_registry`** (Story 2.1) : `features/m2_agent_registry/` rempli — `archetypes.py` (registry immutable `MappingProxyType`), `service.py` (atomicité `with_tenant` → `create_in_session` → `publish(session=...)`), `router.py` (3 endpoints GET/POST + DI `_build_service`), `schemas.py` (Pydantic v2 strict `extra="forbid"`), `templates/archetype-schema.yaml`. **Story 2.2 étend ce module** — pas de nouveau module M8.
> - **Audit-event bypass** (décision Epic 1 retro 2026-05-08) : `AuditEventRepo.record()` lève `NotImplementedError` jusqu'à Story 9.1. **Utiliser `event_bus.publish_and_commit('m2.agent_template.updated', ...)` avec un commentaire `# TODO Story 9.1 — audit-event bypass cleanup` sur UNE LIGNE** (le `git grep "audit-event bypass cleanup"` doit matcher — pattern Story 2.1 P-15).
> - **RFC 7807** (Stories 1.1, 1.9) : `AgentiveError` + `handle_agentive_error` opérationnels. `ValidationError`, `ConflictError`, `NotFoundError` dans `shared/exceptions.py`. **Convention canonique `type: "/errors/{kind}"`** (B1 amendement Story 2.1 — pas d'URL absolue).
> - **Error handler PII stripping** (Story 2.1 P-06) : `app/main.py` strip `input` + `ctx` des erreurs Pydantic avant le payload RFC 7807 422. Réutiliser tel quel — ne pas dupliquer.
> - **Frontend feature `agent_registry`** (Story 2.1) : `frontend/src/features/agent_registry/` contient `types.ts`, `api.ts`, `hooks.ts` (`useArchetypes`, `useArchetypeDetail`, `useCreateTemplate`), `ArchetypeSelector.tsx`, `ArchetypePreview.tsx`. **Story 2.2 étend `api.ts` + `hooks.ts`** avec `getTemplate(id)`, `updateTemplate(id, payload)`, `useTemplate(id)`, `useUpdateTemplate(id)`.
> - **Frontend route `/config/agents/$templateId.tsx`** : actuellement placeholder Story 2.1 (affiche juste `templateId`). **Story 2.2 le remplace** par le formulaire d'édition expert.
> - **Toaster sonner** (Story 2.1, mounted dans `AppLayout`) : `toast.success(...)` / `toast.error(...)` utilisable directement.
> - **Design system** (Story 1.8) : 16 primitives shadcn dans `frontend/src/shared/components/ui/`. Disponibles : `Input`, `Textarea`, `Select`, `Button`, `Label`, `Card`, etc. ESLint `boundaries/dependencies` v6 actif (post-rétro Epic 1).
> - **Conventions TODO Story 9.1** : ne pas casser `git grep "audit-event bypass cleanup"` — ce hit doit rester sur **une seule ligne** (préserver convention P-15 Story 2.1).
>
> **Tech-debt Story 2.1 à fermer dans cette story (D4, D6) :**
>
> - **D4** : Validation UUID du paramètre route `$templateId` au point d'entrée. Tooltip Story 2.1 : la route détail GET/PUT recevra des UUID — valider format avant tout fetch DB pour 422 explicite (au lieu d'un 500 SQLAlchemy ou d'un 404 trompeur). Solution : Path param FastAPI `template_id: UUID` (FastAPI auto-valide). Côté frontend, `useTemplate` doit guard `enabled: id != null && /^[0-9a-f-]{36}$/.test(id)` (cohérent avec P-09 Story 2.1).
> - **D6** : `useCreateTemplate.onSuccess` invalide `["agent-templates"]` mais cette queryKey n'existe pas encore (Story 2.1 ne liste pas les templates). **Story 2.2 introduit le pattern queryKey** : `["agent-template", id]` pour le détail (consommé par `useTemplate`), `["agent-templates"]` pour la liste future. Aligner les invalidations : `useUpdateTemplate.onSuccess` invalide `["agent-template", id]` + `["agent-templates"]`. **D6 ne sera pleinement fermé qu'avec la liste templates (Story 2.4 ou 2.7) — Story 2.2 pose juste les fondations conventionnelles.**
>
> **Décisions intégrées (Epic 1 retro + Story 2.1) :**
>
> 1. **Audit-event bypass** : `event_bus.publish_and_commit('m2.agent_template.updated', payload, session=session)` + commentaire TODO une ligne pour `git grep`.
> 2. **Atomicité single-transaction** (P-02 Story 2.1) : update template + insert prompt + publish event = **1 seule transaction**. Échec ⇒ rollback complet. Aucun event ne fuit si l'update échoue.
> 3. **Smoke test runtime systématique** (P-04 Story 2.1) : `make up` puis `curl -k -X PUT https://localhost:8443/api/v1/agents/templates/{id} -H "Authorization: Bearer ${AGENTIVE_API_TOKEN}" -d '{...}'` et assertions sur logs structlog (`correlation_id` présent, `event_type=m2.agent_template.updated`). Documenté en T6.4.
> 4. **Pas de `parent_version` / `is_active` dans `prompts`** : le schéma actuel suffit pour AC1 (versioning incrémental), l'évolution vers le schéma cible architecture H2 attendra une story dédiée (Story 2.4 ou 2.7 selon besoin instances + rollback). **Documenter clairement cette divergence en Dev Notes** pour éviter la confusion à long terme.
> 5. **Provider chain Sprint 1** : juste une `list[str]` validée (chaque string ∈ whitelist `{"anthropic", "openai"}`) — pas de fallback runtime actif (LLMRouter Story 1.6 livre l'abstraction, mais le provider chain configurable arrivera Story 4.6 "Interruption / Relance / Fallback LLM"). Story 2.2 stocke la config ; ne consomme pas encore au runtime.
> 6. **Error policy Sprint 1** : config-only — la spec déclare `on_timeout`, `max_retries`, `backoff_strategy` dans `error_policy`, mais le **dispatcher actif** (qui APPLIQUE le retry) viendra avec l'orchestrateur de workflows (Story 4.6). Story 2.2 valide le shape, persiste la config, **n'exécute pas encore le retry** au runtime (acceptable : aucun agent ne s'exécute encore Sprint 1 hors spike M3).
>
> **Anti-scope strict (à NE PAS faire dans cette story) :**
>
> - **Mode Wizard guidé vs Expert direct → Story 2.3.** Cette story livre UNIQUEMENT le mode Expert (formulaire flat à champs visibles, PAS d'accordéons stylés ni de wizard étapes/progress). `ModeToggle` (UX-DR16) + accordéons (UX-DR24) + persistance préférence utilisateur sont 2.3.
> - **Validation Zod miroitant Pydantic** → Story 2.3 (cf B2 amendement Story 2.1 : Zod arrive quand le formulaire multi-champs justifie l'overhead — c'est ici. ⚠️ Re-évaluation : sera-t-elle introduite ici OU en 2.3 ? **Décision : Story 2.2 utilise validation native HTML5 + checks JS minimaux (pattern Story 2.1) ; Zod arrive en 2.3 avec le mode Wizard et les step gates.**
> - **Distinction template ↔ instance → Story 2.4** (snapshot gelé au runtime). Aucune création d'`AgentInstance` ici.
> - **Tool Hub MCP / assignation outils → Story 2.5.**
> - **Sandbox bwrap → Story 2.6.**
> - **Agent Playground → Story 2.7.**
> - **Review conversationnelle Producteur/Contrôleur → Story 2.8.**
> - **Liste paginée des templates** (`GET /api/v1/agents/templates` + page `/config/agents/`) → reportée Story 2.4 (besoin de la distinction template/instance) ou Story 2.7 (Playground a besoin de lister). Story 2.2 garde le stub Story 2.1 sur `/config/agents/`.
> - **Rollback de version** (revenir à v(N-1)) → Story 2.4 ou 2.7. La table `prompts` actuelle ne supporte pas `is_active` et la rollback sémantique nécessite une décision UX dédiée.
> - **Validation runtime dispatcher error_policy + provider_chain** → Story 4.6 (orchestrateur). Story 2.2 stocke et valide le shape uniquement.
> - **Édition visuelle structurée des contracts** (`core` + `extras` avec sous-champs typés ajoutables UI) → Story 2.3 ou 2.4. Story 2.2 expose un `<Textarea>` JSON brut (parser côté JS, validation backend Pydantic v2).
> - **Frontend `getTemplates()` listing** → reporté avec la liste paginée ci-dessus. Story 2.2 expose UNIQUEMENT `getTemplate(id)` + `updateTemplate(id)`.

## Story

**As John (développeur solo / Owner)**,
**I want** configurer en détail un agent-template existant (identité, system prompt, contrats input/output élastiques, modèle LLM + paramètres, provider chain, politique d'erreur),
**So that** je l'adapte finement à son rôle spécifique sans repartir d'une création complète, avec un historique versionné des prompts pour traçabilité et reproductibilité.

## Acceptance Criteria

### AC1 — `PUT /api/v1/agents/templates/{template_id}` met à jour un template (versioning prompts incrémenté)

**Given** un template existant créé via Story 2.1 (`POST /api/v1/agents/templates`) avec `version=1` et un Bearer token valide
**When** je fais `PUT /api/v1/agents/templates/{template_id}` avec body :
```json
{
  "system_prompt": "Tu es un agent Producteur expert en TypeScript. Génère du code idiomatique strict mode.",
  "input_contract": {"core": {"task": "string", "language": "string"}, "extras": {}},
  "output_contract": {"core": {"code": "string", "explanations": "string"}, "extras": {}},
  "llm_model": "claude-3-5-sonnet-20241022",
  "llm_params": {"temperature": 0.2, "max_tokens": 4096},
  "provider_chain": ["anthropic"],
  "error_policy": {"on_timeout": "retry_with_backoff", "max_retries": 3, "backoff_strategy": "exponential"}
}
```
**Then** la réponse est `200 OK` avec body :
```json
{
  "template_id": "<uuid>",
  "name": "Code Producer",
  "archetype": "producteur",
  "version": 2,
  "config": { /* config complète mise à jour incluant les 6 nouvelles clés */ },
  "updated_at": "<iso8601>"
}
```
**And** une row est insérée dans `prompts` (`agent_template_id={template_id}`, `version=2`, `content="<system_prompt verbatim>"`, `tenant_id=NULL` Sprint 1) — vérifiable via `PromptRepo.get_by_template_version(template_id, 2)`.
**And** la row `agent_templates` voit `version` passer de `1 → 2` et `config` mise à jour avec les 6 nouvelles clés (en plus des 4 héritées Story 2.1 : `prompt_base`, `input_contract`, `output_contract`, `role`).
**And** un audit event `m2.agent_template.updated` est publié via `event_bus.publish_and_commit(...)` avec payload `{template_id, name, archetype, old_version: 1, new_version: 2, correlation_id, actor: "system"}` (système Sprint 1 — `actor` réel propagé Story 1.7/9.1).
**And** le `correlation_id` du request est propagé dans tous les logs structlog du service + de l'event publié.

**Given** le `template_id` n'est pas un UUID valide (ex: `abc`, `123`, `not-a-uuid`)
**When** je `PUT /api/v1/agents/templates/abc`
**Then** la réponse est `422 Unprocessable Entity` RFC 7807 (FastAPI Path validator auto-déclenché par `template_id: UUID`) — **fix de D4 Story 2.1**.

**Given** le `template_id` est un UUID valide mais inexistant en DB
**When** je `PUT /api/v1/agents/templates/00000000-0000-0000-0000-000000000000`
**Then** la réponse est `404 Not Found` RFC 7807 avec `type: "/errors/not-found"`, `title: "Resource not found"`, `detail: "Agent template '<uuid>' not found"`, `correlation_id` présent.

**Given** le body contient un champ inconnu (Pydantic v2 `extra="forbid"` strict)
**When** je `PUT` avec un payload incluant `{..., "rogue_field": "..."}`
**Then** la réponse est `422 RFC 7807` avec détail Pydantic standard, `input` + `ctx` strippés (pattern P-06 Story 2.1).

**Given** le body est partiel (ex: juste `system_prompt`, sans les autres champs)
**When** je `PUT`
**Then** la requête est **acceptée** : seuls les champs présents sont mis à jour (PATCH-like sémantique malgré le verbe PUT, pragmatique Sprint 1 — voir Dev Notes §"PUT vs PATCH"). Les champs absents conservent leur valeur précédente. **Une nouvelle version `prompts` n'est insérée QUE SI `system_prompt` est dans le payload** (sinon, juste UPDATE `agent_templates.config` sans bump version, sans insert prompt).

### AC2 — Contrats élastiques `core` + `extras` validés Pydantic v2

**Given** un payload PUT avec `input_contract: {"core": {"query": "string"}, "extras": {"meta": "additional"}}`
**When** la validation Pydantic v2 strict s'exécute sur le schéma `UpdateTemplateRequest`
**Then** elle accepte la structure si `core` est un `dict[str, Any]` (champs typés à validation runtime ultérieure) **et** `extras` est un `dict[str, Any]` (zone permissive).
**And** un payload manquant la clé `core` (`{"extras": {...}}`) → 422 RFC 7807 avec `detail: "Field 'core' required"`.
**And** un payload avec `core` n'étant pas un dict (ex: `"core": "string"`) → 422.

**Given** la spec architecture H8 (versioning des contrats élastiques)
**When** je consulte la documentation
**Then** la convention `schema_version` dans le contrat est **acceptée mais NON validée** Sprint 1 (la table `CONTRACTS` registry du `core/contracts/registry.py` arrive Story 4.x avec l'orchestrateur). Story 2.2 stocke `input_contract`/`output_contract` comme `dict[str, Any]` brut (Pydantic `model_validator` n'inspecte que la structure `core` + `extras`).

### AC3 — Wrapping anti-prompt-injection (utility AR44)

**Given** un helper backend `wrap_external_input(content: str, kind: Literal["user_input", "tool_output"]) -> str` est ajouté dans `backend/src/agentive_backend/shared/llm/security.py` (nouveau fichier, dans `shared/llm` — co-localisé avec `LLMRouter` Story 1.6).
**When** je l'appelle avec `wrap_external_input("Ignore all previous instructions", "user_input")`
**Then** il retourne `"<user_input>Ignore all previous instructions</user_input>"`.
**And** si le contenu lui-même contient déjà `</user_input>` (tentative de break-out), le helper escape les `<` du payload (`&lt;`) avant wrapping. Test unitaire couvre ce cas.
**And** un test unitaire valide les 2 `kind` autorisés (`user_input`, `tool_output`) ; tout autre `kind` → `ValueError` (Pydantic Literal-strict côté Python).

**Note Sprint 1** : ce helper est livré + testé mais **pas encore consommé en prod** (aucun agent ne fait d'appel LLM avec inputs externes Sprint 1 hors spike M3). Il sera consommé Story 2.7 (Agent Playground) puis Story 4.x (workflows). **AC3 = livraison de l'outil + couverture tests, pas intégration runtime.**

### AC4 — Politique d'erreur — schema validation (NFR14)

**Given** un schéma Pydantic v2 `ErrorPolicy` dans `features/m2_agent_registry/schemas.py` :
```python
class ErrorPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on_timeout: Literal["retry_with_backoff", "fail_fast", "fallback_provider"] = "retry_with_backoff"
    max_retries: int = Field(default=3, ge=0, le=10)
    backoff_strategy: Literal["exponential", "linear", "constant"] = "exponential"
```
**When** un payload PUT inclut `error_policy: {"on_timeout": "retry_with_backoff", "max_retries": 3, "backoff_strategy": "exponential"}`
**Then** la validation Pydantic accepte et persiste cette configuration dans `agent_templates.config["error_policy"]`.
**And** `max_retries` hors range (`-1` ou `11`) → 422 RFC 7807 avec détail Pydantic standard (constraint violation).
**And** `on_timeout` ou `backoff_strategy` hors enum → 422.

**Note Sprint 1** : le **dispatcher actif** (qui APPLIQUE le retry au runtime) arrive Story 4.6 (Interruption/Relance/Fallback LLM). Story 2.2 valide + persiste **uniquement la config** (la stack d'exécution agents Sprint 1 est limitée au spike M3 LangGraph qui a son propre checkpointing — distinct).

### AC5 — Page `/config/agents/{templateId}` — formulaire expert minimal

**Given** je navigue vers `/config/agents/{templateId}` (route TanStack file-based : `frontend/src/app/routes/config/agents/$templateId.tsx`, qui **remplace** le placeholder Story 2.1)
**When** la page se monte
**Then** un fetch `GET /api/v1/agents/templates/{templateId}` (nouveau endpoint, voir AC6) charge les données du template courant via `useTemplate(id)` (TanStack Query, `queryKey: ["agent-template", id]`, `staleTime: 30_000` ms).
**And** le formulaire affiche les champs suivants (tous **flat**, sans accordéons stylés — Story 2.3 stylera) :

| Champ | Composant | Source défaut |
|---|---|---|
| `name` | `<Input readOnly>` (le rename est hors scope 2.2 — défer Story 2.4) | template courant |
| `system_prompt` | `<Textarea>` (rows=12, monospace optionnel via `font-mono`) | template courant ou `archetype.prompt_base` si vide |
| `llm_model` | `<Select>` avec options whitelist : `["claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022", "gpt-4o", "gpt-4o-mini"]` | template courant ou `"claude-3-5-sonnet-20241022"` |
| `llm_params.temperature` | `<Input type="number" step="0.1" min="0" max="2">` | `0.7` |
| `llm_params.max_tokens` | `<Input type="number" min="1" max="200000">` | `4096` |
| `provider_chain` | `<Textarea>` JSON brut (ex: `["anthropic"]`) — éditeur structuré reporté Story 2.3 | `["anthropic"]` |
| `input_contract` | `<Textarea>` JSON brut (rows=8) | template courant |
| `output_contract` | `<Textarea>` JSON brut (rows=8) | template courant |
| `error_policy.on_timeout` | `<Select>` : `["retry_with_backoff", "fail_fast", "fallback_provider"]` | `"retry_with_backoff"` |
| `error_policy.max_retries` | `<Input type="number" min="0" max="10">` | `3` |
| `error_policy.backoff_strategy` | `<Select>` : `["exponential", "linear", "constant"]` | `"exponential"` |

**And** un bouton "Sauvegarder" (`<Button variant="default">`) déclenche `useUpdateTemplate(id).mutate(payload)`.
**And** un bouton secondaire "Annuler" remet les valeurs initiales du fetch (re-fetch via `queryClient.invalidateQueries(["agent-template", id])`).
**And** pendant la mutation, le bouton "Sauvegarder" passe en loading (disabled + spinner).
**And** sur succès : `toast.success("Template mis à jour (v{new_version})")` + invalidation des queryKeys `["agent-template", id]` et `["agent-templates"]`.
**And** sur erreur 422 : `toast.error("<title> : <detail>")` (parsing RFC 7807) + le focus va au premier champ invalide (extraction des `errors[].loc[1]` dans le JSON RFC 7807).
**And** sur erreur 404 (UUID invalide en route) : redirection vers `/config/agents/` (la liste future ; Sprint 1 = redirection vers `/config/`).

**Anti-scope visuel** : pas d'accordéons (UX-DR24 = Story 2.3), pas de `ModeToggle` Wizard/Expert (UX-DR16 = Story 2.3), pas de progress steps. Layout : `max-width: 640px` centré (UX layout Config — `ux-design-specification.md` ligne 622).

### AC6 — `GET /api/v1/agents/templates/{template_id}` — endpoint détail (nouveau)

**Given** un Bearer token valide + un `template_id` UUID existant
**When** je `GET /api/v1/agents/templates/{template_id}`
**Then** la réponse est `200 OK` avec body `{template_id, name, archetype, version, config, created_at}`.
**And** UUID invalide → 422 (FastAPI Path UUID).
**And** UUID valide mais inexistant → 404 RFC 7807 (`type: "/errors/not-found"`).

**Justification de l'ajout dans Story 2.2** : l'AC5 a besoin de fetcher le template pour l'éditer. Endpoint stable, scope minimal, pas de query string Sprint 1 (filtrage / pagination = liste, reporté).

### AC7 — Tests : ≥ 25 nouveaux tests, 0 régression baseline

**Tests backend (≥ 18) :**

- **Unit `test_schemas_update.py`** : 6 tests minimum sur `UpdateTemplateRequest` Pydantic v2 — extra forbid, contracts shape (core/extras), error_policy enum + ranges, llm_model whitelist, llm_params bounds, partial payload accepted.
- **Unit `test_security.py`** : 4 tests minimum sur `wrap_external_input` — happy `user_input`, happy `tool_output`, escape `</user_input>` injection, kind invalide → ValueError.
- **Unit `test_routes_update.py`** : 4 tests minimum sur la route PUT — 200 OK happy path (mock service), 404 not found, 422 UUID invalid (en fait FastAPI gère, donc 1 test smoke), 422 extra field strict.
- **Unit `test_routes_get.py`** : 2 tests minimum — 200 OK détail, 404 not found.
- **Integration `test_update_template_e2e.py`** (testcontainers Postgres) : 6 tests minimum couvrant les flows e2e —
  1. PUT happy path → row updated + prompt v2 inserted + outbox event + correlation_id propagé
  2. PUT partial (juste system_prompt) → version bump + prompt insert
  3. PUT partial (sans system_prompt, juste llm_params) → config update SANS bump version SANS prompt insert
  4. PUT 404 not found UUID inexistant
  5. PUT 422 extra field
  6. Atomicity proof : monkeypatch event_bus.publish pour lever exception → assertion : aucune row prompt insérée + aucune row template modifiée + outbox vide pour cet event_type post-fail (rollback complet observé en DB).

**Tests frontend (≥ 7) :**

- **Component `$templateId.test.tsx`** : 5 tests minimum —
  1. Render charge `useTemplate(id)` (mock fetch) et affiche les champs pré-remplis
  2. Save button déclenche mutation avec le payload attendu
  3. Toast succès affiché + invalidation queries appelée
  4. Toast erreur 422 affiché avec parsing RFC 7807 + focus au champ invalide
  5. UUID invalide en route → redirection (peut être un mock router)
- **Hook `useUpdateTemplate.test.ts`** : 2 tests minimum — onSuccess invalide les bonnes queryKeys, onError propage l'erreur RFC 7807 brute.

**Régression** : `make test` reste vert sur **390 backend (incluant les nouveaux) + 26 frontend (incluant les nouveaux)** — baseline post-Story 2.1 (commit 72b03d1). Aucune modification des tests Story 2.1 sauf si ajout d'un nouveau cas de figure justifié (et alors documenté en Completion Notes).

### AC8 — Smoke test runtime + validation conventions

**Given** la stack est démarrée (`make up`) et un template existe (créé via Story 2.1 endpoint)
**When** j'exécute :
```bash
TOKEN=$(grep AGENTIVE_API_TOKEN .env | cut -d= -f2)
TID=$(curl -sk -X POST https://localhost:8443/api/v1/agents/templates \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"archetype":"producteur","name":"Smoke Producer 2.2"}' | jq -r .template_id)
curl -sk -X PUT https://localhost:8443/api/v1/agents/templates/$TID \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"system_prompt":"Smoke test prompt v2","llm_model":"claude-3-5-sonnet-20241022"}' \
  -w "\n%{http_code}\n"
```
**Then** la réponse contient `200` HTTP et `version: 2` dans le body.
**And** `make logs` (backend) montre une ligne JSON structlog avec `event_type=m2.agent_template.updated`, `correlation_id` présent (UUID), `template_id` égal à `$TID`, `new_version=2`, `old_version=1`.
**And** un PUT avec UUID invalide (`/api/v1/agents/templates/abc`) retourne `422` avec body `application/problem+json`.
**And** `git grep "audit-event bypass cleanup"` retourne **2 hits** (1 hérité Story 2.1 sur `m2.agent_template.created`, 1 nouveau Story 2.2 sur `m2.agent_template.updated`) — chaque hit sur **une seule ligne** (convention P-15).

**And** `make test` retourne `0 failed, 0 errors` (vert global) avant marking story `review`.

## Tasks / Subtasks

- [x] **T1 — Schemas Pydantic v2 (AC1, AC2, AC4)**
  - [x] T1.1 Étendre `features/m2_agent_registry/schemas.py` : ajouter `ErrorPolicy(BaseModel)`, `LLMParams(BaseModel)`, `ContractDefinition(BaseModel)`, `UpdateTemplateRequest(BaseModel)`, `TemplateDetailResponse(BaseModel)`, `UpdateTemplateResponse(BaseModel)`. Tous avec `model_config = ConfigDict(extra="forbid")`. Whitelists `llm_model` via `Literal[...]` ou Field validator (au choix — `Literal` plus strict + introspectable).
  - [x] T1.2 Tests unitaires `test_schemas_update.py` ≥ 6 tests (AC2, AC4 enum/ranges, AC1 extra forbid, partial payload). **Réalisé : 14 tests.**

- [x] **T2 — Repos étendus (AC1)**
  - [x] T2.1 `shared/repositories/agent_repo.py` : `update_in_session` + `update_config_in_session` (no bump) + `get_by_id_in_session`. Pattern atomicité identique à `create_in_session`.
  - [x] T2.2 `shared/repositories/prompt_repo.py` : `create_in_session` ajouté ; `create()` refactorisé en wrapper self-managed transaction qui délègue à `create_in_session`.
  - [x] T2.3 Tests unitaires : 3 nouveaux tests sur `AgentTemplateRepo` + 1 nouveau sur `PromptRepo` (happy paths via mock session).

- [x] **T3 — Helper anti-prompt-injection (AC3)**
  - [x] T3.1 `backend/src/agentive_backend/shared/llm/security.py` créé avec `wrap_external_input(content, kind)` — `html.escape(quote=False)` AVANT wrapping pour rendre l'enveloppe inforgeable.
  - [x] T3.2 Pas exposé via `shared/llm/__init__.py` — import explicite obligatoire.
  - [x] T3.3 Tests `tests/unit/llm/test_security.py` : 6 tests (happy user_input, happy tool_output, escape break-out, escape entities, kind invalide → ValueError, empty content).

- [x] **T4 — Service `update_template` (AC1)**
  - [x] T4.1 `service.py` : `update_template()` + `get_template_by_id()` ajoutés. Pattern atomicité P-02 (with_tenant → get_by_id_in_session → 404 → merge config → bump conditionnel → update_in_session OU update_config_in_session → prompt insert si bump → publish event). TODO Story 9.1 sur ligne unique préservé.
  - [x] T4.2 Tests `test_update_template_service.py` : 4 tests (404, bump+prompt+event, no-bump+no-prompt+event, contracts model_dump merge).

- [x] **T5 — Routes FastAPI (AC1, AC6)**
  - [x] T5.1 `PUT /api/v1/agents/templates/{template_id}` (Path UUID auto-validé) ajouté.
  - [x] T5.2 `GET /api/v1/agents/templates/{template_id}` ajouté.
  - [x] T5.3 `test_agents_templates_route.py` étendu : 6 nouveaux tests (PUT UUID invalide / extra rejected / llm_model invalid / happy path ; GET UUID invalide / happy path).
  - [x] T5.4 `tags=["agents"]` conservé (héritage Story 2.1).
  - [x] T5.5 `_build_service` étendu pour wirer aussi `PromptRepo`.

- [x] **T6 — Frontend page édition (AC5, D4 fix, D6 pose)**
  - [x] T6.1 `api.ts` : `getTemplate(id)` + `updateTemplate(id, payload)`.
  - [x] T6.2 `hooks.ts` : `useTemplate(id)` avec guard UUID regex (D4 fix frontend) + `useUpdateTemplate(id)` invalidant `["agent-template", id]` + `["agent-templates"]` (D6 partial — convention queryKey posée).
  - [x] T6.3 `types.ts` : 6 nouveaux types miroirs.
  - [x] T6.4 `$templateId.tsx` reconstruit en formulaire flat expert (12 champs : name readonly, system_prompt, llm_model select, temperature/max_tokens, provider_chain JSON, input/output_contract JSON, error_policy 3 sous-champs). Pattern "derived state during render" pour hydrater le form depuis la query (évite `react-hooks/set-state-in-effect`). Focus on error via `document.getElementById` (pattern Story 2.1 P-11).
  - [x] T6.5 Tests `$templateId.test.tsx` : 5 tests (hydratation, save → PUT body shape, toast erreur 422, UUID invalide → page erreur sans fetch, JSON invalide bloque PUT). Test existant `new.test.tsx` mis à jour pour mocker GET détail post-redirect.

- [x] **T7 — Smoke + régression + commit (AC8)**
  - [x] T7.1 `make test` : **431 backend (+41 vs baseline 390) + 31 frontend (+5 vs baseline 26), 0 failed, 0 errors.** Total +46 nouveaux tests (≥ 25 attendus).
  - [x] T7.2 Smoke runtime AC8 exécuté : POST + PUT happy 200 (version=1→2) + PUT UUID invalide 422 RFC 7807 + log structlog `event_type=m2.agent_template.updated` avec `correlation_id`, `old_version`, `new_version`, `bump_version: true` propagés.
  - [x] T7.3 `git grep "audit-event bypass cleanup"` → 2 hits sur 2 lignes distinctes (`service.py:152` Story 2.1 created, `service.py:319` Story 2.2 updated).
  - [x] T7.4 `make lint-backend` + `make lint-frontend` verts (3 ruff fixes appliqués post-write : RUF100 noqa unused, RUF043 raw string match, I001 import sort + auto-format ; 1 ESLint fix : `react-hooks/set-state-in-effect` → derived state during render pattern).
  - [x] T7.5 Story marquée `Review` ; sprint-status.yaml bumpé `in-progress → review` ; commit conventional pendant.

## Dev Notes

### Architecture & patterns

- **Atomicité single-transaction** (P-02 Story 2.1) : le pattern `with_tenant() → *_in_session() → publish(session=...)` est NON-NÉGOCIABLE. Toute écriture multiple (template + prompt + outbox event) DOIT vivre dans la même transaction. Échec partiel ⇒ rollback complet. AC7 test 6 (atomicity proof) vérifie ce contract en monkeypatchant `event_bus.publish` pour lever une exception et asserter qu'aucune row n'a fuit.
- **`with_tenant()` est un `@asynccontextmanager`** (`shared/repositories/base.py`) — il ouvre une `AsyncSession`, set le `SET LOCAL app.current_tenant_id` (Story 1.5 isolation tenant), et commit/rollback à l'`__aexit__`. Ne **jamais** `session.commit()` manuellement à l'intérieur du bloc — le context manager gère.
- **PUT vs PATCH sémantique** : on choisit PUT par cohérence avec l'epic (ligne 815 : `PUT /api/v1/agents/templates/:id`), mais le comportement réel est PATCH-like (champs absents = inchangés). C'est une concession pragmatique Sprint 1. Documentation OpenAPI doit le dire (`description: "PUT — semantically PATCH-like for Sprint 1; absent fields preserved."`). Cette décision sera revue Story 2.4 (où la distinction template/instance peut clarifier la sémantique).
- **Décision schéma `prompts`** : la table actuelle (`models.py:241-261`) est minimale (pas de `parent_version`, `is_active`, `metadata`). Architecture H2 (`architecture.md:891-912`) décrit l'état cible (avec `is_active` UNIQUE constraint). **Story 2.2 ne migre PAS le schéma** — l'évolution attendra (Story 2.4 ou 2.7 selon besoin instances + rollback). Documenter ça en commentaire de `models.py` Prompt class :
  ```python
  # NOTE Sprint 1 — schéma minimal (Story 1.5). L'évolution vers H2 architecture
  # (parent_version, is_active, metadata JSONB) attendra Story 2.4 / 2.7 quand
  # le besoin runtime instances + rollback se concrétisera.
  ```
- **Versioning lazily-seeded** : Story 2.1 ne crée AUCUNE row `prompts` à la création du template (`v1` est implicite = `archetype.prompt_base`). Story 2.2 commence à v2 lors du premier UPDATE. La requête historique « quel était le prompt v1 » se résout via `template.config['prompt_base']` (immutable car Story 2.1 ne touche jamais cette clé). C'est OK pour Sprint 1.
- **`config` JSONB = single-source-of-truth** pour la version courante : la table `prompts` est l'historique audit, `agent_templates.config` reflète l'état actuel (incluant `system_prompt` au format string, dupliqué avec `prompts.content` de la version courante). C'est une duplication acceptée Sprint 1 (simplifie les fetchs UI). Documenter cette décision en commentaire service.py.
- **Audit-event bypass** : tout commentaire TODO Story 9.1 doit être sur **une seule ligne** pour préserver `git grep "audit-event bypass cleanup"`. Pattern Story 2.1 P-15 :
  ```python
  # TODO Story 9.1 — audit-event bypass cleanup : migrer vers AuditEventRepo.record() (NotImplementedError jusqu'à 9.1)
  await event_bus.publish_and_commit("m2.agent_template.updated", payload, session=session)
  ```
  Le `git grep` doit retourner exactement 2 hits après cette story (1 hérité Story 2.1 sur `created`, 1 nouveau Story 2.2 sur `updated`).
- **Import-linter contracts** : aucune nouveauté — les Contracts 1 (features-isolated), 3 (no-direct-sqlalchemy/asyncpg from features), 4 (no-direct-langchain), 5 (no-direct-llm-sdk) doivent rester verts. Le helper `wrap_external_input` vit dans `shared/llm/security.py` — pas une feature, pas de violation. Si un test échoue ici → c'est un signal qu'un import direct s'est glissé (`grep -rn "import sqlalchemy\|import anthropic\|import openai" backend/src/agentive_backend/features/`).
- **ESLint boundaries v6** : la page `$templateId.tsx` (en `app/routes/`) consomme uniquement `features/agent_registry/` + `shared/components/ui/` + `shared/components/layouts/`. Pas d'import cross-feature. Le hook `useTemplate` reste dans le feature `agent_registry`.

### Patterns de réutilisation

| Pattern Story 2.1 | À réutiliser tel quel | Localisation |
|---|---|---|
| `with_tenant() → create_in_session() → publish(session=...)` | Étendre avec `update_in_session` + insertion prompt + publish | `service.py:create_template` (ligne ~110-150) |
| `IntegrityError → ConflictError` | Même translation pour update si nom unique violé (peu probable Sprint 1 — pas de rename) | `agent_repo.py:create_in_session:106-112` |
| Error handler PII stripping (input/ctx) | Réutiliser tel quel — déjà en place sur tous les `RequestValidationError` | `app/main.py` (Story 2.1 P-06) |
| `_build_service(request: Request)` DI | Réutiliser pour les nouvelles routes PUT + GET détail | `router.py` (Story 2.1 + ~5 lignes) |
| RFC 7807 `type: "/errors/{kind}"` | `NotFoundError` lève déjà avec `type="/errors/not-found"` | `shared/exceptions.py` |
| TanStack Query staleTime strategy | Detail = 30s, registry/archétypes = Infinity, listings = 5min (futur) | Pattern Story 2.1 `hooks.ts` |
| Toast feedback sonner | `toast.success(...)` / `toast.error(...)` | `AppLayout` Story 2.1 D9 |
| Test E2E testcontainers | Pattern `test_create_template_e2e.py` + fixture `postgres_container` | `tests/integration/m2_agent_registry/` |

### Project Structure Notes

**Fichiers backend NOUVEAUX :**

- `backend/src/agentive_backend/shared/llm/security.py` (T3) — utility `wrap_external_input`
- `backend/tests/unit/shared/llm/test_security.py` (T3.3)
- `backend/tests/unit/services/test_update_template.py` (T4.2)
- `backend/tests/unit/test_schemas_update.py` (T1.2) — ou regrouper avec `test_schemas.py` Story 2.1 si déjà présent
- `backend/tests/integration/m2_agent_registry/test_update_template_e2e.py` (T7 / AC7)

**Fichiers backend MODIFIÉS :**

- `backend/src/agentive_backend/features/m2_agent_registry/schemas.py` (T1.1) — ajout 6 schemas
- `backend/src/agentive_backend/features/m2_agent_registry/service.py` (T4.1) — ajout `update_template()` + `get_template_by_id()`
- `backend/src/agentive_backend/features/m2_agent_registry/router.py` (T5) — ajout 2 routes
- `backend/src/agentive_backend/features/m2_agent_registry/__init__.py` (T1) — exports nouveaux schemas si besoin (probablement non — usage interne uniquement)
- `backend/src/agentive_backend/shared/contracts/events/agent_events.py` (T4) — ajout constante `AGENT_TEMPLATE_UPDATED = "m2.agent_template.updated"` + classe `AgentTemplateUpdatedEvent` (Pydantic v2, miroir de `AgentTemplateCreatedEvent` Story 2.1)
- `backend/src/agentive_backend/shared/repositories/agent_repo.py` (T2.1) — ajout `update_in_session` + éventuellement `update_config_in_session` (no bump) + `get_by_id_in_session` si pas déjà
- `backend/src/agentive_backend/shared/repositories/prompt_repo.py` (T2.2) — ajout `create_in_session`
- `backend/src/agentive_backend/infra/db/models.py` (Dev Notes) — ajout commentaire NOTE Sprint 1 sur Prompt class

**Fichiers frontend NOUVEAUX :**

- `frontend/src/app/routes/config/agents/$templateId.test.tsx` (T6.5)
- `frontend/src/features/agent_registry/hooks.test.ts` (T6.5) — peut être fusionné avec un fichier hooks test existant

**Fichiers frontend MODIFIÉS :**

- `frontend/src/app/routes/config/agents/$templateId.tsx` (T6.4) — remplacement complet du placeholder
- `frontend/src/features/agent_registry/api.ts` (T6.1) — ajout 2 fonctions
- `frontend/src/features/agent_registry/hooks.ts` (T6.2) — ajout 2 hooks
- `frontend/src/features/agent_registry/types.ts` (T6.3) — ajout 6 types
- `frontend/src/features/agent_registry/index.ts` (T6) — exports nouveaux hooks si convention Story 2.1 (probablement oui)

**Fichiers NON modifiés (vérifier que ça reste vrai post-implémentation) :**

- `docker-compose.yml`, `Makefile` — aucune raison de toucher
- Migrations Alembic — aucune nouvelle migration nécessaire (les tables existent depuis Story 1.5)
- `.import-linter` — aucun nouveau contrat
- `eslint.config.js` — aucun changement de boundaries

### Tech-debt status (post-Story 2.2)

- **D4 fermé** : UUID validation auto-côté FastAPI (`template_id: UUID` Path) + frontend guard `enabled` regex.
- **D6 partiellement fermé** : conventions queryKey posées (`["agent-template", id]` + `["agent-templates"]`). `useUpdateTemplate` invalide les 2. **Restera ouvert** jusqu'à ce que la liste templates existe (Story 2.4 ou 2.7) — à ce moment-là, `useCreateTemplate` (Story 2.1) verra son invalidation `["agent-templates"]` réellement consommée.
- **D1, D2, D3, D5, D7-D10** : inchangés (pas dans le scope de cette story).
- **NOUVEAU defer** :
  - **D11** : Pas de rollback UI / `prompts.is_active` — défer Story 2.4 ou 2.7
  - **D12** : Provider chain runtime fallback non actif — défer Story 4.6
  - **D13** : Error policy dispatcher actif (retry exponential backoff réel) — défer Story 4.6
  - **D14** : Édition visuelle des contracts (form structuré au lieu de Textarea JSON) — défer Story 2.3 ou 2.4
  - **D15** : Endpoint `GET /api/v1/agents/templates` (liste paginée) — défer Story 2.4 ou 2.7

### References

- `_bmad-output/planning-artifacts/epics.md:806-831` — Story 2.2 spec verbatim (AC1-AC4)
- `_bmad-output/planning-artifacts/prd.md:497` — FR10 (configuration agent)
- `_bmad-output/planning-artifacts/prd.md:500` — FR13 (contrats élastiques core + extras)
- `_bmad-output/planning-artifacts/architecture.md:891-912` — H2 schéma `prompts` (cible — Sprint 1 utilise version minimale 1.5)
- `_bmad-output/planning-artifacts/architecture.md:971-1003` — H8 versioning contrats élastiques + `schema_version`
- `_bmad-output/planning-artifacts/architecture.md:1379-1397` — Module M2 structure
- `_bmad-output/planning-artifacts/architecture.md:393, 554-563` — LLM Router abstraction (Story 1.6 — Sprint 1 livré)
- `_bmad-output/planning-artifacts/ux-design-specification.md:622-626, 681` — Layout Config (max-width 640px), wizard vs expert distinction (= 2.3)
- `_bmad-output/implementation-artifacts/2-1-creer-agent-template-depuis-archetype.md` — Story 2.1 patterns + tech-debt D1-D10
- `_bmad-output/implementation-artifacts/epic-1-retro-2026-05-08.md` — décisions audit-event bypass + boundaries v6
- `backend/src/agentive_backend/infra/db/models.py:241-261` — Prompt model actuel
- `backend/src/agentive_backend/shared/repositories/agent_repo.py:24-114` — AgentTemplateRepo (Story 2.1)
- `backend/src/agentive_backend/shared/repositories/prompt_repo.py:18-58` — PromptRepo (Story 1.5)
- `backend/src/agentive_backend/features/m2_agent_registry/service.py` — pattern atomicité Story 2.1 (référence pour update_template)
- `backend/src/agentive_backend/features/m2_agent_registry/router.py` — pattern routes Story 2.1
- Commit `72b03d1` — Story 2.1 done baseline
- Commit `1db1a54` — fix Makefile parité CI (pré-requis pour `make test` vert)

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (1M context) — bmad-dev-story single-pass execution.

### Debug Log References

- 1 itération test fail (mocks repo trop naïfs : `update_in_session` retournait template sans muter `version`/`config`) → mocks corrigés avec `_mock_update` qui mute le template (mirror du vrai repo). 4 tests verts post-fix.
- 1 itération atomicité e2e fail (ASGITransport propage `RuntimeError` au lieu de retourner 500) → wrap `client.put` dans `pytest.raises(RuntimeError)` ; la preuve d'atomicité (DB unchanged) reste asserté.
- 3 ruff lint fixes post-implémentation : RUF100 noqa unused (assert sans noqa), RUF043 raw string sur match=, I001 import sort.
- 1 ESLint fix : `react-hooks/set-state-in-effect` ⇒ pattern "derived state during render" (https://react.dev/reference/react/useState#storing-information-from-previous-renders) avec `hydratedFromId` tracking.

### Completion Notes List

- ✅ AC1 PUT endpoint avec versioning prompts incrémenté (smoke test : v1→v2 observé en DB + log structlog)
- ✅ AC2 contrats élastiques `core` + `extras` validés via Pydantic v2 strict
- ✅ AC3 helper `wrap_external_input` AR44 livré + 6 tests (escape break-out + entities)
- ✅ AC4 ErrorPolicy schema NFR14 (validation only — dispatcher défer Story 4.6 documenté)
- ✅ AC5 page `/config/agents/{templateId}` formulaire flat expert (12 champs) — anti-scope respecté (pas de Wizard/accordéons stylés)
- ✅ AC6 GET détail endpoint (200 happy / 404 / 422 UUID invalid)
- ✅ AC7 tests : **+41 backend / +5 frontend** (total 431 backend + 31 frontend, 0 régression). Frontend à +5 vs +7 spec : amend mineur — 5 tests `$templateId.test.tsx` couvrent les flows critiques (hydratation, save, toast erreur, UUID guard, JSON malformé). Pas de fichier `hooks.test.ts` séparé créé — les hooks sont exercés indirectement via les tests page.
- ✅ AC8 smoke runtime : 200 OK PUT happy, 422 PUT UUID invalide, log `m2.agent_template.updated` avec `correlation_id`/`old_version`/`new_version`/`bump_version` propagés, `git grep "audit-event bypass cleanup"` → 2 hits 2 lignes distinctes
- ✅ Tech-debt 2.1 D4 fermé (FastAPI Path UUID + frontend regex guard)
- 🟡 Tech-debt 2.1 D6 partiellement fermé : conventions queryKey `["agent-template", id]` + `["agent-templates"]` posées et invalidées par `useUpdateTemplate`. La pleine fermeture attend la liste paginée (Story 2.4 / 2.7).
- 🆕 Defer documentés dans la story : D11 rollback UI prompts.is_active, D12 provider chain runtime fallback, D13 error policy dispatcher actif, D14 contracts visuels, D15 endpoint listing paginé.
- ⚠️ Décision documentée Sprint 1 : schéma `prompts` actuel (`models.py:241-261`) reste minimal — l'évolution vers H2 architecture (`parent_version`, `is_active`, `metadata` JSONB) attendra Story 2.4 / 2.7 avec besoin runtime instances + rollback.

### File List

**Backend NEW (5)**
- `backend/src/agentive_backend/shared/llm/security.py` — helper `wrap_external_input` AR44
- `backend/tests/unit/llm/test_security.py` — 6 tests
- `backend/tests/unit/m2_agent_registry/test_schemas_update.py` — 14 tests
- `backend/tests/unit/m2_agent_registry/test_update_template_service.py` — 4 tests
- `backend/tests/integration/m2_agent_registry/test_update_template_e2e.py` — 7 tests e2e (incl. atomicité)

**Backend MODIFIED (8)**
- `backend/src/agentive_backend/features/m2_agent_registry/schemas.py` — +6 schemas (ErrorPolicy, LLMParams, ContractDefinition, UpdateTemplateRequest, TemplateDetailResponse, UpdateTemplateResponse) + 2 type aliases (LLMModel, ProviderId)
- `backend/src/agentive_backend/features/m2_agent_registry/service.py` — +update_template, +get_template_by_id, +PromptRepo wiring
- `backend/src/agentive_backend/features/m2_agent_registry/router.py` — +PUT /agents/templates/{id}, +GET /agents/templates/{id}, +PromptRepo wiring dans `_build_service`
- `backend/src/agentive_backend/shared/contracts/events/agent_events.py` — +AgentTemplateUpdatedEvent
- `backend/src/agentive_backend/shared/contracts/events/__init__.py` — export AgentTemplateUpdatedEvent
- `backend/src/agentive_backend/shared/repositories/agent_repo.py` — +update_in_session, +update_config_in_session, +get_by_id_in_session
- `backend/src/agentive_backend/shared/repositories/prompt_repo.py` — +create_in_session, refactor `create()` en wrapper
- `backend/tests/unit/repositories/test_agent_repo.py` — +3 tests
- `backend/tests/unit/repositories/test_prompt_repo.py` — +1 test
- `backend/tests/unit/api/test_agents_templates_route.py` — +6 tests (PUT + GET)

**Frontend NEW (1)**
- `frontend/src/app/routes/config/agents/$templateId.test.tsx` — 5 tests

**Frontend MODIFIED (5)**
- `frontend/src/app/routes/config/agents/$templateId.tsx` — remplacement complet du placeholder Story 2.1 par le formulaire expert flat
- `frontend/src/app/routes/config/agents/new.test.tsx` — mock GET détail post-redirect (la page détail Story 2.2 fetch le template au montage)
- `frontend/src/features/agent_registry/api.ts` — +getTemplate, +updateTemplate
- `frontend/src/features/agent_registry/hooks.ts` — +useTemplate (D4 UUID guard), +useUpdateTemplate
- `frontend/src/features/agent_registry/types.ts` — +6 types (TemplateDetail, UpdateTemplateRequest, UpdateTemplateResponse, ErrorPolicy, LLMParams, ContractDefinition) + 2 alias (LLMModel, ProviderId)
- `frontend/src/features/agent_registry/index.ts` — exports étendus

**Documentation MODIFIED (1)**
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — bump 2.2 in-progress → review + log line
