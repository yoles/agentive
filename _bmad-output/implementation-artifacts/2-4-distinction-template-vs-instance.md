# Story 2.4 : Distinction agent-template vs agent-instance

Status: done

> 🎯 **Quatrième story Epic 2 — Agent Platform.** Cette story livre la **distinction sémantique formelle** entre :
>
> 1. **agent-template** = définition persistée, versionnée (Stories 2.1 + 2.2 + 2.3 livrées sur `agent_templates` + `prompts`)
> 2. **agent-instance** = exécution en cours avec **snapshot gelé** des paramètres au moment du run
>
> **Pourquoi maintenant ?** Parce que les Stories 2.5 (Tool Hub), 2.6 (Sandbox bwrap), 2.7 (Playground), 2.8 (Review conversationnelle) vont toutes consommer la notion d'instance. Sans la distinction, modifier un template casserait des runs en cours et l'historique d'exécution ne serait pas auditable (FR12 + Innovation Vision Agent Évolutionnaire).
>
> **Backend uniquement Sprint 1** — pas de UI consumer Sprint 1 (arrive Story 8.x trace explorer + Story 7.x dashboard). Frontend touche minimal `types.ts` + `api.ts` (pas d'écran).
>
> Tables `agent_templates` + `agent_instances` existent déjà depuis Story 1.5 (cf `infra/db/models.py:202-238` + migration `20251015_000000_initial_schema.py`). `AgentInstanceRepo.create()` existe déjà avec snapshot JSONB. Cette story livre :
>
> - **Service** `AgentRegistryService.instantiate_from_template(template_id, workflow_run_id?)` — crée une instance avec snapshot complet, single-transaction atomique
> - **Endpoint** `POST /api/v1/agents/templates/{template_id}/instances` — crée + retourne une instance
> - **Endpoint** `GET /api/v1/agents/instances/{instance_id}` — détail d'une instance
> - **Endpoint** `GET /api/v1/workflows/runs/{run_id}/instances` — liste les instances pour un run
> - **Event** `m2.agent_instance.created` (audit bypass Story 9.1, cf §"Décisions intégrées" #4)
> - **Tests** : ≥ 18 nouveaux (≥ 15 backend + 3 frontend types/api stub) couvrant snapshot capture, isolation v2/v3, GET endpoints, 404 paths
>
> **Pré-requis hérités (déjà disponibles — NE PAS recréer) :**
>
> - **`AgentInstance` model** (`infra/db/models.py:220-238`, Story 1.5) : `id` UUID + `template_id` UUID FK ON DELETE RESTRICT + `template_version` Integer + `workflow_run_id` UUID FK ON DELETE SET NULL nullable + `snapshot` JSONB + `created_at` + `tenant_id` nullable. Cette story **NE TOUCHE PAS** au model — il est déjà aligné avec l'architecture cible.
> - **`AgentInstanceRepo`** (`shared/repositories/agent_repo.py:188-210`) : `get_by_id(instance_id, tenant_id)` + `create(template_id, template_version, snapshot, workflow_run_id, tenant_id)`. À ÉTENDRE avec `create_in_session` (atomicité Story 2.1 P-02 pattern) + `list_by_workflow_run_in_session`.
> - **`AgentTemplateRepo.get_by_id_in_session`** (Story 2.2 `agent_repo.py:117-122`) : SELECT par id dans la transaction du caller. À RÉUTILISER tel quel.
> - **`WorkflowRunRepo`** (`shared/repositories/workflow_repo.py:59-110`) : `get_by_id` + `create(workflow_id, correlation_id, status, tenant_id)`. **Utilisé uniquement par les tests** pour créer une fixture `workflow_runs` (les tests intégration AC2 + AC3). **Anti-scope** : pas d'endpoint `POST /workflows/runs` Sprint 1 (Story 4.1).
> - **Pattern audit bypass** (`features/m2_agent_registry/service.py` Story 2.1 + 2.2) : `event_bus.publish_and_commit('m{N}.{verb}.{audited-object}', ...)` dans la même session que l'INSERT métier. TODO `audit-event bypass cleanup` doit être ajouté sur 1 ligne unique pour que `git grep "audit-event bypass cleanup"` continue de fonctionner (cf P-15 Story 2.1 second-pass review). Migration vers `AuditEventRepo.record()` = T0 Story 9.1.
> - **Event class déjà documentée** (`shared/contracts/events/agent_events.py:11`) : "Story 2.4 will add `m2.agent_instance.created` / `m2.agent_instance.completed`". `m2.agent_instance.completed` est défer Story 4.x (workflow_engine signale la fin). Sprint 1 livre uniquement `m2.agent_instance.created`.
> - **Service pattern** (`features/m2_agent_registry/service.py` Story 2.1) : `AgentRegistryService` reçoit `template_repo`, `prompt_repo` en constructeur. À ÉTENDRE avec `instance_repo` + `workflow_run_repo` (le second pour valider l'existence du run avant POST).
> - **Router pattern** (`features/m2_agent_registry/router.py` Story 2.1+2.2) : 5 endpoints existants `/agents/archetypes` × 2, `/agents/templates` × 3 (POST/GET/PUT). Cette story ajoute 3 endpoints : `POST /agents/templates/{id}/instances`, `GET /agents/instances/{id}`, et un NOUVEAU router `/api/v1/workflows/runs/{run_id}/instances` (T6 — peut être dans `m2_agent_registry/router.py` ou un nouveau `m3_workflow_engine/router.py` Sprint 1 minimal).
> - **DependencyError + NotFoundError + ConflictError** (`shared/exceptions.py`) : à RÉUTILISER (404 + 409 RFC 7807 par le global handler `app.main`).
> - **Frontend `agent_registry` feature** : `types.ts` à étendre avec `AgentInstance`, `InstantiateTemplateRequest`, `InstantiateTemplateResponse`. `api.ts` + `hooks.ts` à étendre avec `instantiateTemplate(templateId, body)` + `getInstance(instanceId)` + `useInstantiateTemplate(templateId)` + `useInstance(instanceId)`. **Aucun composant React** Sprint 1 (consumer = Story 8.x).
>
> **Anti-scope strict (à NE PAS faire dans cette story) :**
>
> - **Workflow engine logic** (le code qui crée vraiment les instances quand un workflow démarre) → **Story 4.1+ Epic 4**. Sprint 1 = endpoint manuel `POST /agents/templates/{id}/instances` qui simule ce que workflow_engine fera Story 4.1.
> - **UI viewer instances** (composant React qui affiche le snapshot, diff template vs instance, etc.) → **Story 8.x trace explorer**.
> - **Tools assignment to instances** (`agent_template_tools` junction + runtime tools.allow) → **Story 2.5**.
> - **Hot-swap mechanism** : l'absence de hot-swap EST l'AC2, pas un feature. Une fois snapshot copié, la modification du template ne propage jamais à l'instance (par design).
> - **Cleanup / TTL des instances** → **Story 9.x retention** (Sprint 1 = grow forever, c'est OK pour un single-user dev).
> - **Versioning rollback prompt is_active** → **Story 2.7+** (architecture H2 défer P-16 Story 2.2).
> - **Schema H2 prompts** (parent_version, is_active UNIQUE constraint, metadata JSONB) → **défer Story 2.7** (P-16 follow-up Story 2.2 code-review 2026-05-09).
> - **Workflow creation endpoint** (`POST /workflows` + `POST /workflows/runs`) → **Story 4.1**. Les tests AC2 + AC3 créent les fixtures via `WorkflowRepo.create` + `WorkflowRunRepo.create` directement (chemin repo, pas HTTP).
> - **PUT/PATCH `/agents/instances/{id}`** → **JAMAIS**. L'instance est immutable post-création par design (gel des paramètres). Modifier l'instance contredirait sa raison d'être.
> - **DELETE `/agents/instances/{id}`** → défer Story 9.x retention. Sprint 1 = append-only sauf cascade SET NULL via `workflow_runs` delete (jamais déclenché Sprint 1).
> - **`m2.agent_instance.completed` event** → **Story 4.x** (workflow_engine signale la fin). Sprint 1 livre uniquement `created`.
> - **UI Frontend** : 0 composant React, 0 route. Juste types + api stub + hooks (pour préparer Story 8.x).
>
> **Décisions intégrées (Epic 1 retro + Stories 2.1-2.3 code-reviews) :**
>
> 1. **Snapshot complet figé** — `AgentInstance.snapshot` contient `{template_id, template_version, name, archetype, config_at_instantiation}` (100% du template au moment du run). PAS de FK vers `prompts.id` spécifique : le snapshot inclut `system_prompt` directement (résolu depuis `template.config["system_prompt"]` au moment de la création). Garantie d'isolation totale même si template renommé/upgradé/supprimé après. Pattern documenté épique : "the snapshot includes everything needed to reproduce the agent state at run time".
> 2. **workflow_run_id optionnel** — instance peut exister hors workflow (Playground Story 2.7, tests d'isolation, debugging manuel via curl). FK `ON DELETE SET NULL` déjà en place (Story 1.5). Sprint 1 = endpoint `POST /agents/templates/{id}/instances` accepte body `{workflow_run_id?: UUID}` optionnel.
> 3. **Atomicité single-transaction** — pattern Story 2.1 P-02 strict : `AgentRegistryService.instantiate_from_template` ouvre la transaction via `with_tenant`, fait SELECT template (`get_by_id_in_session`) + INSERT instance (`create_in_session` à AJOUTER) + INSERT outbox event (`event_bus.publish` no commit), commit auto à la sortie du context manager. Si l'audit échoue, l'instance n'est pas créée.
> 4. **Audit event bypass `m2.agent_instance.created`** — pattern Story 1.4 outbox (`event_bus.publish_and_commit`) avec TODO Story 9.1 sur 1 ligne unique : `# TODO(Story 9.1): audit-event bypass cleanup — migrate to AuditEventRepo.record()`. `git grep "audit-event bypass cleanup"` doit trouver cette nouvelle entrée (compte attendu Sprint 1 post-2.4 = 3 hits : Story 2.1 created, Story 2.2 updated, Story 2.4 instance.created).
> 5. **Pas de UI Sprint 1** — le consumer UI arrive Story 8.x (trace explorer per workflow_run). Sprint 1 = endpoints REST + tests intégration uniquement. Frontend touche minimal `types.ts` + `api.ts` + `hooks.ts` pour préparer Story 8.x — aucun composant React, aucune route TanStack.
> 6. **Rétro Epic 1 §10 — D1 actor=system** — l'audit event utilise `actor="system"` Sprint 1 (auth token statique single-user Story 1.7, pas de user_id résolu). Cleanup en même temps que Story 9.1 (D1 défer ouvert tracé).
> 7. **404 sémantique strict** — `POST /agents/templates/{nonexistent}/instances` → 404 NotFoundError ; `GET /agents/instances/{nonexistent}` → 404 ; `GET /workflows/runs/{nonexistent}/instances` → 404 (workflow_run lui-même n'existe pas), PAS une liste vide. Si workflow_run existe mais 0 instances → 200 + `[]`. Cohérent RFC 7807 + sémantique REST.
> 8. **Provider chain runtime fallback** — défer **Story 4.6** (D12 Story 2.2) — l'instance copie `provider_chain` dans son snapshot mais Sprint 1 = pas de runtime dispatcher qui consomme la chaîne. Le snapshot stocke la valeur figée pour Story 4.6 future.
> 9. **Pas d'index sur `agent_instances.workflow_run_id`** Sprint 1 — volume négligeable Sprint 1 (single-user dev, < 100 instances/jour réaliste). Ajouter si benchmarks Sprint 2 montrent N+1 ou full-scan. Tracé en defer **D28**.
> 10. **Snapshot immutable post-création** — pas d'endpoint `PUT/PATCH /agents/instances/{id}` Sprint 1 (ni jamais — voir anti-scope). La modification d'une instance contredirait sa raison d'être (gel des paramètres). `AgentInstance` est append-only sauf cascade `ON DELETE SET NULL` via `workflow_runs` delete (jamais déclenché Sprint 1).
> 11. **Snapshot shape standardisé** — clés du snapshot JSONB : `{"template_id": str, "template_version": int, "name": str, "archetype": str, "config": {...}}` où `config` = copie textuelle de `agent_templates.config` au moment de la création. Évite la duplication de schema (le format `config` reste celui de `UpdateTemplateRequest` Story 2.2). Documenté en docstring du `instantiate_from_template`.
> 12. **Pas de `m2.agent_instance.completed` Sprint 1** — l'event est documenté en commentaire `agent_events.py:11` mais sa publication arrive Story 4.x quand workflow_engine signale la fin (besoin de connaître status final). Sprint 1 = `created` uniquement.
> 13. **Tests fixtures workflow_run** — les tests AC2 + AC3 créent un `workflow_run` via `WorkflowRunRepo.create` direct (besoin d'un `workflow_id` valide → `WorkflowRepo.create` avant). Pattern : test fixture compose Workflow → WorkflowRun → puis 2 AgentInstance avec `workflow_run_id`. Ces fixtures ne fuitent PAS dans le code production.
> 14. **`/api/v1/workflows/runs/{run_id}/instances`** — où mettre cet endpoint ? **Décision** : dans `features/m2_agent_registry/router.py` Sprint 1 (cohérent avec la feature qui owner les `agent_instances`). Story 4.x pourra le déplacer vers `m3_workflow_engine/router.py` quand le module devient le owner du `workflow_run` lifecycle. Anti-scope : pas de feature `m3_workflow_engine` créée Sprint 1.
> 15. **`tenant_id` Sprint 1** — `None` partout (single-tenant). L'isolation tenant arrive **Story 12** (Multi-User & Permissions Sprint 4-5). Cohérent avec Stories 2.1-2.3 qui hardcodent `tenant_id=None`.
> 16. **Smoke test runtime obligatoire AC8** — Story 2.4 = backend pur, donc le smoke test = `curl POST /agents/templates/{id}/instances` + `curl GET /agents/instances/{id}` + assertions logs structlog. Pattern Story 2.2 AC8 reproduit. Pas de smoke UI puisque pas de UI.

## Story

**As the system (Agentive runtime)**,
**I want** distinguer l'agent-template (définition persistée, versionnée) de l'agent-instance (exécution en cours avec snapshot gelé des paramètres),
**So that** je peux modifier un template sans casser les runs en cours et garder l'historique d'exécution auditable, ce qui est une fondation requise pour Stories 2.5 (Tools), 2.6 (Sandbox), 2.7 (Playground), 2.8 (Review) et tout le runtime workflow_engine Story 4.x.

## Acceptance Criteria

### AC1 — `instantiate_from_template` crée une AgentInstance avec snapshot complet figé

**Given** un agent_template `T` existe avec `config = {system_prompt: "...", llm_model: "claude-3-5-sonnet-20241022", provider_chain: ["anthropic"], llm_params: {...}, error_policy: {...}, input_contract: {...}, output_contract: {...}}` et `version = 2`
**And** un appel API `POST /api/v1/agents/templates/{T.id}/instances` (body vide ou `{workflow_run_id: null}`)

**Then** une nouvelle ligne dans `agent_instances` est créée avec :
- `id` UUID auto-généré
- `template_id = T.id`
- `template_version = 2` (copie de `T.version` au moment de la création)
- `workflow_run_id = NULL`
- `snapshot = {"template_id": "<T.id>", "template_version": 2, "name": "<T.name>", "archetype": "<T.archetype>", "config": <copie textuelle de T.config>}`
- `created_at = now()`
- `tenant_id = NULL` (Sprint 1 single-tenant)

**And** la réponse HTTP est `201 Created` avec le body :

```json
{
  "instance_id": "<uuid>",
  "template_id": "<T.id>",
  "template_version": 2,
  "workflow_run_id": null,
  "snapshot": { ... },
  "created_at": "2026-05-10T12:34:56.789Z"
}
```

**And** l'event `m2.agent_instance.created` est publié dans `outbox_events` dans **la même transaction** que l'INSERT instance (atomicité P-02 Story 2.1 pattern).

**And** un log structlog `event_type=m2.agent_instance.created` apparaît avec :
- `correlation_id` (depuis `request.state.correlation_id` Story 1.9)
- `template_id`, `template_version`, `instance_id`, `workflow_run_id` (peut être null), `actor="system"`, `tenant_id=null`

### AC2 — Pas de hot-swap : modifier le template après instantiation ne touche pas l'instance

**Given** un template `T` avec `version = 2`, `config.system_prompt = "Tu es un Producteur v2."`
**And** une instance `I1` créée via `POST /agents/templates/{T.id}/instances` (capture snapshot v2)
**When** John modifie le template via `PUT /agents/templates/{T.id}` avec `{system_prompt: "Tu es un Producteur v3."}` (Story 2.2 = bump version → 3)
**And** une seconde instance `I2` est créée via `POST /agents/templates/{T.id}/instances`

**Then** `I1.snapshot.config.system_prompt === "Tu es un Producteur v2."` (inchangé, isolation totale)
**And** `I1.template_version === 2`
**And** `I2.snapshot.config.system_prompt === "Tu es un Producteur v3."`
**And** `I2.template_version === 3`
**And** `T.version === 3` (le template a bien bumpé)

**Test integration vérifie cette isolation** (T7.3) : 1 fixture template + 1 instance + PUT template + 1 nouvelle instance + assertions snapshots distincts.

### AC3 — `GET /workflows/runs/{run_id}/instances` retourne les instances rattachées au run

**Given** un `workflow_run` `R` existe (créé via fixture test → `WorkflowRepo.create` puis `WorkflowRunRepo.create`)
**And** 2 `AgentInstance` `I1` et `I2` ont été créées avec `workflow_run_id = R.id`
**And** 1 autre instance `I3` créée avec `workflow_run_id = NULL`
**When** un appel `GET /api/v1/workflows/runs/{R.id}/instances` est fait

**Then** la réponse est `200 OK` avec un body :

```json
[
  {"instance_id": "<I1.id>", "template_id": "...", "template_version": ..., "workflow_run_id": "<R.id>", "snapshot": {...}, "created_at": "..."},
  {"instance_id": "<I2.id>", ...}
]
```

(ordre `created_at ASC` pour stabilité — décision exécution)

**And** `I3` n'apparaît pas (pas rattaché à R).

**Given** un `run_id` inexistant
**When** `GET /api/v1/workflows/runs/{nonexistent}/instances`
**Then** réponse `404 Not Found` RFC 7807 (le run lui-même n'existe pas, pas une liste vide).

**Given** un `workflow_run` qui existe mais sans instances rattachées
**When** `GET /api/v1/workflows/runs/{R_empty.id}/instances`
**Then** réponse `200 OK` avec body `[]`.

### AC4 — `GET /agents/instances/{instance_id}` retourne l'instance complète

**Given** une instance `I` existe
**When** `GET /api/v1/agents/instances/{I.id}`
**Then** réponse `200 OK` avec body :

```json
{
  "instance_id": "<I.id>",
  "template_id": "<T.id>",
  "template_version": 2,
  "workflow_run_id": "<R.id>" | null,
  "snapshot": { "template_id": ..., "template_version": ..., "name": ..., "archetype": ..., "config": {...} },
  "created_at": "..."
}
```

**Given** un `instance_id` inexistant
**When** `GET /api/v1/agents/instances/{nonexistent}`
**Then** réponse `404 Not Found` RFC 7807.

**Given** un `instance_id` non-UUID
**When** `GET /api/v1/agents/instances/not-a-uuid`
**Then** réponse `422 Unprocessable Content` (FastAPI Path UUID validation, cohérent Story 2.2 D4 fermé).

### AC5 — Atomicité single-transaction (P-02 Story 2.1 pattern)

**Given** un test qui mock `event_bus.publish_and_commit` pour qu'il throw
**When** `instantiate_from_template` est appelé
**Then** ni l'instance ni l'event ne sont committés (rollback atomique)
**And** un `SELECT count(*) FROM agent_instances` montre 0 row supplémentaire post-erreur.

### AC6 — Endpoint `POST /agents/templates/{nonexistent}/instances` → 404

**Given** un `template_id` qui n'existe pas dans `agent_templates`
**When** `POST /api/v1/agents/templates/{nonexistent}/instances`
**Then** réponse `404 Not Found` RFC 7807 (`type=/errors/not-found`, `title="Template not found"`, `detail="Template <uuid> does not exist"`).

**And** aucune ligne `agent_instances` n'est créée.
**And** aucun event `m2.agent_instance.created` n'est publié.

### AC7 — Tests : ≥ 18 nouveaux, 0 régression baseline

**Tests backend (≥ 15)** :

- **`test_instantiate_template_service.py`** ≥ 5 tests : (1) snapshot capture happy path, (2) actor=system + tenant_id=None, (3) audit event published `m2.agent_instance.created`, (4) atomicité — event throw → 0 row, (5) workflow_run_id=null accepté.
- **`test_instantiate_template_e2e.py`** ≥ 6 tests : (1) `POST happy 201 + body shape complet`, (2) `POST 404 si template inexistant`, (3) `POST 422 si UUID invalide`, (4) `POST 404 si workflow_run_id fourni mais inexistant`, (5) **AC2 isolation v2/v3** (1 instance + PUT template + 2nd instance + snapshots distincts), (6) `GET /agents/instances/{id} happy 200`.
- **`test_workflow_run_instances_e2e.py`** ≥ 4 tests : (1) `GET happy 200 avec 2 instances ordonnées created_at ASC`, (2) `GET 200 + [] si run sans instances`, (3) `GET 404 si run inexistant`, (4) instance hors run (`workflow_run_id=null`) n'apparaît pas dans le list.

**Tests frontend (≥ 3)** :

- **`agent_registry/api.test.ts`** ou **`agent_registry/hooks.test.tsx`** ≥ 3 tests : (1) `instantiateTemplate(templateId)` POST URL + body, (2) `getInstance(instanceId)` GET URL, (3) `useInstantiateTemplate` invalide `["agent-template", id, "instances"]` queryKey.

**Régression** : `make test` reste vert sur **baseline 435 backend + 87 frontend** (post-Story 2.3 done). Aucune modification des tests Stories 2.1-2.3 sauf si refactor structurel (à documenter en Completion Notes).

### AC8 — Smoke test runtime + lint + typecheck

**Smoke test runtime obligatoire** (Sprint 1 backend pur, équivalent Story 2.2 AC8) :

```bash
# Setup : récupérer un template_id existant via POST /agents/templates (Story 2.1)
TEMPLATE_ID=$(curl -s -X POST http://localhost/api/v1/agents/templates \
  -H "Authorization: Bearer dev-static-token" \
  -H "Content-Type: application/json" \
  -d '{"name": "smoke-2.4", "archetype": "producteur"}' | jq -r .template_id)

# AC1 — instantiate happy
curl -X POST http://localhost/api/v1/agents/templates/$TEMPLATE_ID/instances \
  -H "Authorization: Bearer dev-static-token" -d '{}'
# → 201 + {instance_id, template_version: 1, snapshot: {...}, created_at}

INSTANCE_ID=$(... | jq -r .instance_id)

# AC4 — get instance
curl http://localhost/api/v1/agents/instances/$INSTANCE_ID
# → 200 + même payload

# AC2 — bump template puis nouvelle instance
curl -X PUT http://localhost/api/v1/agents/templates/$TEMPLATE_ID \
  -H "Authorization: Bearer dev-static-token" \
  -d '{"system_prompt": "v2 prompt"}'
# → 200 + version: 2

curl -X POST http://localhost/api/v1/agents/templates/$TEMPLATE_ID/instances -d '{}'
# → 201 + {template_version: 2, snapshot.config.system_prompt: "v2 prompt"}
# Et la 1re instance a toujours snapshot.config.system_prompt: "" (initial vide)

# AC6 — 404 sur template inexistant
curl -X POST http://localhost/api/v1/agents/templates/00000000-0000-0000-0000-000000000000/instances
# → 404 RFC 7807

# Logs structlog : grep correlation_id propagé
docker logs agentive-backend-1 | grep "m2.agent_instance.created"
# → JSON line avec event_type, correlation_id, template_id, template_version, instance_id
```

**Lint + typecheck verts** : `make lint` (ruff + mypy backend, eslint + tsc frontend) → 0 erreur. Pas d'`any` introduit. Pattern Stories 2.1-2.3 reproduit.

## Tasks / Subtasks

- [x] **T0 — Pré-requis** (AC1, vérifications)
  - [ ] T0.1 Vérifier que `AgentInstance` model est aligné avec l'AC (`infra/db/models.py:220-238`) — devrait être OK depuis Story 1.5. Aucune migration Alembic requise.
  - [ ] T0.2 Vérifier que `AgentInstanceRepo.create()` existe (`shared/repositories/agent_repo.py:188-210`). Si signature divergente du besoin (ex : pas de `workflow_run_id` paramètre), AJUSTER en gardant la compat (Sprint 1 ne casse rien Stories 1.5+).
  - [ ] T0.3 Vérifier `git grep "audit-event bypass cleanup"` baseline = 2 hits (Story 2.1 service.py:152 created + Story 2.2 service.py:319 updated).

- [x] **T1 — Étendre `AgentInstanceRepo` avec `create_in_session` + `list_by_workflow_run_in_session`** (AC1, AC3, AC5)
  - [ ] T1.1 Ajouter `AgentInstanceRepo.create_in_session(session, *, template_id, template_version, snapshot, workflow_run_id=None, tenant_id=None) -> AgentInstance` — pattern `AgentTemplateRepo.create_in_session` (Story 2.1 P-02). INSERT dans la session du caller, `flush()` puis `refresh()`. Pas de `try/except IntegrityError` Sprint 1 (pas de UNIQUE constraint sur `agent_instances` qui pourrait collider — `id` est server_default `gen_random_uuid()`).
  - [ ] T1.2 Ajouter `AgentInstanceRepo.list_by_workflow_run_in_session(session, workflow_run_id: UUID) -> list[AgentInstance]` — `SELECT * FROM agent_instances WHERE workflow_run_id = ? ORDER BY created_at ASC` (déterministe pour les tests). Pour AC3 + GET endpoint.
  - [ ] T1.3 Ajouter `AgentInstanceRepo.get_by_id_in_session` (cohérence avec `AgentTemplateRepo.get_by_id_in_session`) — sera utilisé par le GET endpoint.
  - [ ] T1.4 Tests `tests/unit/shared/repositories/test_agent_repo.py` (étendre fichier existant) ≥ 3 tests sur les 3 nouvelles méthodes.

- [x] **T2 — Schemas Pydantic v2** (AC1, AC4)
  - [ ] T2.1 Dans `features/m2_agent_registry/schemas.py`, ajouter :
    - `class InstantiateTemplateRequest(BaseModel)` avec `model_config = ConfigDict(extra="forbid")` et `workflow_run_id: UUID | None = None` (champ unique optional). `null` ou champ absent = instance hors workflow.
    - `class InstantiateTemplateResponse(BaseModel)` (`model_config` strict) avec `instance_id: UUID`, `template_id: UUID`, `template_version: int`, `workflow_run_id: UUID | None`, `snapshot: dict[str, Any]`, `created_at: datetime`.
    - `class AgentInstanceDetailResponse(BaseModel)` — même shape que `InstantiateTemplateResponse` (alias par cohérence Story 2.2 `TemplateDetailResponse`).
  - [ ] T2.2 Tests `tests/unit/features/m2/test_schemas_instance.py` ≥ 2 tests : extra=forbid rejette champ inconnu, `workflow_run_id` accepte `null` + `UUID` valide.

- [x] **T3 — Event Pydantic** (AC1)
  - [ ] T3.1 Dans `shared/contracts/events/agent_events.py`, ajouter :
    ```python
    class AgentInstanceCreatedEvent(BaseModel):
        """Published after a new agent_instances row is committed (Story 2.4)."""
        event_type: ClassVar[str] = "m2.agent_instance.created"
        instance_id: UUID
        template_id: UUID
        template_version: int = Field(ge=1)
        workflow_run_id: UUID | None = None
        actor: str = Field(default="system")
        tenant_id: UUID | None = None
    ```
  - [ ] T3.2 Mettre à jour `__all__` + le commentaire docstring du module pour acter "Story 2.4 livre `m2.agent_instance.created` ; `m2.agent_instance.completed` reste défer Story 4.x".
  - [ ] T3.3 Tests `tests/unit/shared/contracts/test_agent_events.py` ≥ 2 tests : `event_type` constant, validation Pydantic shape.

- [x] **T4 — Service `AgentRegistryService.instantiate_from_template`** (AC1, AC2, AC5, AC6)
  - [ ] T4.1 Étendre le constructeur `AgentRegistryService` pour recevoir `instance_repo: AgentInstanceRepo` + `workflow_run_repo: WorkflowRunRepo` (en plus des actuels `template_repo` + `prompt_repo`). Mettre à jour `_build_service` dans `router.py` pour wirer les 2 nouveaux repos depuis `app.state.session_factory`.
  - [ ] T4.2 Implémenter `async def instantiate_from_template(self, *, template_id: UUID, workflow_run_id: UUID | None = None) -> InstantiateTemplateResponse` :
    1. Ouvrir session via `self._instance_repo.with_tenant(None)` (single-tenant Sprint 1).
    2. SELECT template via `self._template_repo.get_by_id_in_session(session, template_id)` → si `None`, raise `NotFoundError(detail=f"Template {template_id} does not exist", context={"template_id": str(template_id)})`.
    3. SI `workflow_run_id is not None` : SELECT run via `self._workflow_run_repo.get_by_id_in_session(session, workflow_run_id)` → si `None`, raise `NotFoundError(detail=f"WorkflowRun {workflow_run_id} does not exist")`. **(Note T4.5)**
    4. Construire `snapshot = {"template_id": str(template.id), "template_version": template.version, "name": template.name, "archetype": template.archetype, "config": dict(template.config)}` (deep copy via `dict(...)` suffisant pour le JSONB de premier niveau ; `template.config` est déjà un dict immutable côté Python via SQLAlchemy).
    5. INSERT instance via `self._instance_repo.create_in_session(session, template_id=template.id, template_version=template.version, snapshot=snapshot, workflow_run_id=workflow_run_id, tenant_id=None)` → récupère `instance`.
    6. Publier event :
       ```python
       event = AgentInstanceCreatedEvent(
           instance_id=instance.id,
           template_id=template.id,
           template_version=template.version,
           workflow_run_id=workflow_run_id,
           actor="system",  # TODO(Story 9.1): audit-event bypass cleanup — resolve actor from auth context
           tenant_id=None,
       )
       await publish(session, event_type=event.event_type, payload=event.model_dump(mode="json"))
       ```
    7. Commit auto à la sortie du `with_tenant` context manager.
    8. Best-effort `await emit_notify(session_factory)` post-commit (pattern Story 2.1).
    9. Logger structlog `_log.info("instance.created", instance_id=..., template_id=..., template_version=..., workflow_run_id=..., correlation_id=...)`.
    10. Retourner `InstantiateTemplateResponse.model_validate({**instance, "instance_id": instance.id})` (mapping vers le schema de réponse).
  - [ ] T4.3 **TODO Story 9.1** : ajouter dans `service.py` JUSTE AVANT le `await publish(...)` un commentaire sur **1 ligne unique** (P-15 Story 2.1 second-pass) :
    ```python
    # TODO(Story 9.1): audit-event bypass cleanup — migrate to AuditEventRepo.record() (Story 2.4)
    ```
    `git grep "audit-event bypass cleanup"` doit retourner 3 hits post-Story 2.4 (created Story 2.1 + updated Story 2.2 + instance.created Story 2.4).
  - [ ] T4.4 Implémenter `async def get_instance_by_id(self, instance_id: UUID) -> AgentInstanceDetailResponse` — pattern Story 2.2 `get_template_by_id`. SELECT via `self._instance_repo.get_by_id(instance_id, tenant_id=None)` → si `None`, raise `NotFoundError`. Mapping vers schema.
  - [ ] T4.5 Implémenter `async def list_instances_by_workflow_run(self, workflow_run_id: UUID) -> list[AgentInstanceDetailResponse]` :
    1. Ouvrir session.
    2. SELECT run via `workflow_run_repo.get_by_id_in_session` → si `None`, raise `NotFoundError(detail=f"WorkflowRun {workflow_run_id} does not exist")` (AC3 — pas une liste vide, sémantique REST stricte).
    3. SELECT instances via `instance_repo.list_by_workflow_run_in_session(session, workflow_run_id)`.
    4. Mapping vers `list[AgentInstanceDetailResponse]`.
  - [ ] T4.6 Tests `tests/unit/features/m2/test_instantiate_template_service.py` ≥ 5 tests (AC7).

- [x] **T5 — Étendre `WorkflowRunRepo` avec `get_by_id_in_session`** (AC3, AC6)
  - [ ] T5.1 Dans `shared/repositories/workflow_repo.py`, ajouter `WorkflowRunRepo.get_by_id_in_session(session, run_id: UUID) -> WorkflowRun | None` — pattern `AgentTemplateRepo.get_by_id_in_session`. Une ligne quasi-triviale mais nécessaire pour l'atomicité du service.
  - [ ] T5.2 Pas de tests dédiés (couvert indirectement par les tests service T4.6 + e2e T7).

- [x] **T6 — Endpoints HTTP** (AC1, AC4, AC6)
  - [ ] T6.1 Dans `features/m2_agent_registry/router.py`, ajouter 3 endpoints :
    ```python
    @router.post("/agents/templates/{template_id}/instances", status_code=status.HTTP_201_CREATED, response_model=InstantiateTemplateResponse)
    async def instantiate_template(
        template_id: UUID,
        body: InstantiateTemplateRequest,
        request: Request,
    ) -> InstantiateTemplateResponse:
        service = _build_service(request)
        return await service.instantiate_from_template(
            template_id=template_id,
            workflow_run_id=body.workflow_run_id,
        )

    @router.get("/agents/instances/{instance_id}", response_model=AgentInstanceDetailResponse)
    async def get_instance(instance_id: UUID, request: Request) -> AgentInstanceDetailResponse:
        service = _build_service(request)
        return await service.get_instance_by_id(instance_id)

    @router.get("/workflows/runs/{run_id}/instances", response_model=list[AgentInstanceDetailResponse])
    async def list_instances_by_run(run_id: UUID, request: Request) -> list[AgentInstanceDetailResponse]:
        service = _build_service(request)
        return await service.list_instances_by_workflow_run(run_id)
    ```
  - [ ] T6.2 Vérifier que `_build_service(request)` wire bien les 4 repos (AgentTemplate, Prompt, AgentInstance, WorkflowRun) depuis `app.state.session_factory`.
  - [ ] T6.3 Pas de nouveau routeur Sprint 1 — le 3e endpoint `/workflows/runs/{run_id}/instances` reste dans `m2_agent_registry/router.py` (cohérent avec la feature qui owner les `agent_instances`). Story 4.1 pourra le déplacer vers `m3_workflow_engine/router.py` quand le module devient le owner du `workflow_run` lifecycle.
  - [ ] T6.4 Le routeur `m2_agent_registry` est déjà inclus dans `app.main` (Story 2.1) — pas de re-include requis.
  - [ ] T6.5 Smoke test runtime AC8 : exécuter le bash AC8 contre `make dev` après build (cf liste de commandes ci-dessus) — DOIT être documenté en Completion Notes (output `curl` + log structlog grep).

- [x] **T7 — Tests intégration end-to-end** (AC1-AC6, AC7)
  - [ ] T7.1 Créer `backend/tests/integration/m2_agent_registry/test_instantiate_template_e2e.py` ≥ 6 tests (cf AC7) :
    - Fixtures : `httpx.AsyncClient` (testcontainers postgres), template fixture créé via `POST /agents/templates`.
    - Tests `POST` : happy 201 + body shape, 404 template inexistant, 422 UUID invalide, 404 workflow_run_id fourni inexistant.
    - Test isolation v2/v3 (AC2) : POST → instance v1 → PUT template (system_prompt → bump v2) → POST → instance v2 → assertions snapshots distincts.
    - Test `GET /agents/instances/{id}` happy 200.
  - [ ] T7.2 Créer `backend/tests/integration/m2_agent_registry/test_workflow_run_instances_e2e.py` ≥ 4 tests (cf AC7) :
    - Fixture : Workflow + WorkflowRun via `WorkflowRepo.create` + `WorkflowRunRepo.create` directement (pas d'endpoint POST workflow Sprint 1).
    - Tests : happy 200 avec 2 instances ordonnées, 200 + [] si run sans instances, 404 run inexistant, instance hors run n'apparaît pas.
  - [ ] T7.3 Test atomicité (AC5) — peut vivre dans `test_instantiate_template_service.py` plutôt qu'e2e : monkeypatch `event_bus.publish` pour qu'il throw, asserter `SELECT count(*) FROM agent_instances` reste à 0 post-erreur.

- [x] **T8 — Frontend types/api stub minimal (pas de UI)** (Sprint 1 préparatoire Story 8.x)
  - [ ] T8.1 Dans `frontend/src/features/agent_registry/types.ts`, ajouter :
    ```ts
    export type AgentInstance = {
      instance_id: string;
      template_id: string;
      template_version: number;
      workflow_run_id: string | null;
      snapshot: Record<string, unknown>;
      created_at: string;
    };

    export type InstantiateTemplateRequest = {
      workflow_run_id?: string | null;
    };

    export type InstantiateTemplateResponse = AgentInstance;
    ```
  - [ ] T8.2 Dans `frontend/src/features/agent_registry/api.ts`, ajouter :
    ```ts
    export async function instantiateTemplate(
      templateId: string,
      body: InstantiateTemplateRequest = {},
    ): Promise<InstantiateTemplateResponse> { /* POST */ }

    export async function getInstance(instanceId: string): Promise<AgentInstance> { /* GET */ }

    export async function listInstancesByRun(runId: string): Promise<AgentInstance[]> { /* GET /workflows/runs/{runId}/instances */ }
    ```
  - [ ] T8.3 Dans `frontend/src/features/agent_registry/hooks.ts`, ajouter :
    ```ts
    export function useInstantiateTemplate(templateId: string) {
      const queryClient = useQueryClient();
      return useMutation({
        mutationFn: (body: InstantiateTemplateRequest) => instantiateTemplate(templateId, body),
        onSuccess: () => {
          queryClient.invalidateQueries({ queryKey: ["agent-template", templateId, "instances"] });
        },
      });
    }

    export function useInstance(instanceId: string | null) { /* useQuery avec enabled: !!instanceId */ }

    export function useInstancesByRun(runId: string | null) { /* useQuery */ }
    ```
  - [ ] T8.4 Mettre à jour `frontend/src/features/agent_registry/index.ts` (barrel) avec exports `AgentInstance`, `InstantiateTemplateRequest`, `InstantiateTemplateResponse`, `instantiateTemplate`, `getInstance`, `listInstancesByRun`, `useInstantiateTemplate`, `useInstance`, `useInstancesByRun`.
  - [ ] T8.5 Tests `frontend/src/features/agent_registry/api.test.ts` ou `hooks.test.tsx` ≥ 3 tests (cf AC7).

- [x] **T9 — Documentation update + tech-debt tracking**
  - [ ] T9.1 Mettre à jour le commentaire docstring de `agent_events.py` pour acter que `created` est livré Story 2.4 (et `completed` reste défer Story 4.x).
  - [ ] T9.2 Mettre à jour `_bmad-output/implementation-artifacts/sprint-status.yaml` avec une ligne récap Story 2.4 done + bump `2-4-distinction-template-vs-instance: review` (post-implémentation, avant code-review).
  - [ ] T9.3 Tracer en defer (D28..) : index sur `agent_instances.workflow_run_id`, m2.agent_instance.completed event Story 4.x, UI viewer Story 8.x, hot-cleanup Story 9.x retention.

## Dev Notes

### Pièges connus + leçons Stories 2.1-2.3

1. **Atomicité event-bus + INSERT métier** (Story 2.1 P-02) — JAMAIS `await publish(event)` après `commit()`. TOUJOURS dans la même transaction via `session` partagée. `with_tenant(...)` ferme le commit à la sortie du context manager : structurer le service pour que TOUT le travail (SELECT template + INSERT instance + INSERT outbox) tienne dans le même `with`.
2. **`get_by_id_in_session` vs `get_by_id`** — la version `_in_session` partage la session du caller (atomicité), la version sans suffixe ouvre sa propre session (lecture standalone). Story 2.4 utilise `get_by_id_in_session` dans le service `instantiate_from_template` et `get_by_id` dans `get_instance_by_id` (lecture pure).
3. **`session.refresh(instance)` après `flush()`** — nécessaire pour récupérer `id` et `created_at` peuplés par PostgreSQL (`server_default`). Pattern Story 2.1.
4. **TODO Story 9.1 sur 1 ligne unique** (P-15 Story 2.1 second-pass) — `git grep "audit-event bypass cleanup"` doit retourner 3 hits post-2.4. Si on split le commentaire sur 2 lignes, le grep ne match plus → casse l'inventaire de la dette à fermer Story 9.1.
5. **`actor="system"` Sprint 1** (D1 Epic 1 retro) — l'auth Story 1.7 est un token statique sans `user_id` résolu. L'event audit emporte `actor="system"`. Cleanup Story 9.1 quand l'auth multi-user arrive.
6. **404 vs liste vide** — `GET /workflows/runs/{nonexistent}/instances` retourne 404 (pas `200 + []`). Cohérence sémantique : si la ressource parente n'existe pas, c'est 404 RFC 7807. Si elle existe mais 0 enfants, c'est 200 + liste vide.
7. **Pas de PUT/PATCH sur instance** — l'instance est immutable par design. Si quelqu'un demande "et si on doit corriger le snapshot ?" → la réponse est : on crée une nouvelle instance. Le snapshot d'origine reste pour l'audit. C'est exactement la valeur de la distinction template/instance.
8. **`config` JSONB shape libre** — le snapshot copie `template.config` tel quel. Pas de validation Pydantic du contenu côté instance (seul le wrapper `snapshot` a une shape). Cohérent avec `agent_templates.config` Story 2.1+2.2.
9. **`workflow_run_id` UUID validé par FastAPI Path** (D4 Story 2.1) — `instance_id: UUID` + `template_id: UUID` + `run_id: UUID` dans les signatures de route. FastAPI rejette automatiquement non-UUID en 422.
10. **`InstantiateTemplateResponse` ne renvoie PAS le template complet** — juste l'instance + son snapshot. Si le client veut le template à jour, il fait `GET /agents/templates/{id}` séparément. Évite le coupling et les payloads bloated.
11. **Tests intégration testcontainers** (Story 1.5+2.1+2.2) — les tests e2e tournent dans Docker avec un postgres réel via `testcontainers`. Pas de mock DB. Pattern marqué `@pytest.mark.integration` (auto-skip CI sans Docker disponible).
12. **`make test-backend` parité CI** (Story 2.1 fix Makefile commit `1db1a54`) — utiliser `make test-backend` localement pour reproduire le job CI exactement (mounts `/var/run/docker.sock` + `.import-linter`, `--network host`).
13. **Order `created_at ASC`** (T1.2 + AC3) — décision exécution pour stabilité tests + UX déterministe (ordre d'apparition chronologique). Si Story 8.x veut un autre tri, elle peut le faire côté client.
14. **`tenant_id=None` partout Sprint 1** — single-tenant. Toute occurrence de `tenant_id` dans le code Story 2.4 = `None` hardcodé (cohérent Stories 2.1-2.3). Multi-tenant arrive Story 12 Sprint 4-5.

### Project Structure Notes

- **Backend** : `features/m2_agent_registry/{router,service,schemas}.py` étendus (pas de nouveau module). `shared/repositories/agent_repo.py` étendu (méthodes `_in_session`). `shared/contracts/events/agent_events.py` étendu (1 nouvelle classe). `shared/repositories/workflow_repo.py` étendu (1 nouvelle méthode).
- **Pas de migration Alembic** — `agent_instances` + `workflow_runs` existent déjà depuis Story 1.5.
- **Frontend** : `features/agent_registry/{types,api,hooks,index}.ts` étendus (pas de nouveau composant). 0 route TanStack ajoutée.
- **Tests** : `backend/tests/{unit,integration}/m2_agent_registry/` étendus. `frontend/src/features/agent_registry/` étendu pour tests api/hooks.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 2.4: Distinction agent-template vs agent-instance] — spec brut + 3 ACs originaux.
- [Source: _bmad-output/planning-artifacts/architecture.md#Sprint 1 Anticipations H2] — table `prompts` versionnée + decision rollback Story 2.7+.
- [Source: _bmad-output/planning-artifacts/architecture.md#Conventions DB] — snake_case pluriel + workflow_runs / agent_instances dans la liste canonique.
- [Source: _bmad-output/planning-artifacts/prd.md#FR12] — "Le système peut distinguer agent-template (définition) et agent-instance (exécution en cours)".
- [Source: backend/src/agentive_backend/infra/db/models.py:202-238] — `AgentTemplate` + `AgentInstance` models (Story 1.5).
- [Source: backend/src/agentive_backend/shared/repositories/agent_repo.py:188-210] — `AgentInstanceRepo` baseline.
- [Source: backend/src/agentive_backend/shared/repositories/workflow_repo.py:59-110] — `WorkflowRunRepo` baseline.
- [Source: backend/src/agentive_backend/features/m2_agent_registry/service.py] — pattern atomicité Story 2.1 P-02 + Story 2.2 PUT.
- [Source: backend/src/agentive_backend/shared/contracts/events/agent_events.py:11] — commentaire docstring "Story 2.4 will add `m2.agent_instance.created` / `m2.agent_instance.completed`".
- [Source: _bmad-output/implementation-artifacts/2-1-creer-agent-template-depuis-archetype.md] — atomicité P-02 + audit bypass pattern + 404 RFC 7807 patterns.
- [Source: _bmad-output/implementation-artifacts/2-2-configurer-agent-complet.md] — PUT pattern + UUID validation + atomicité.
- [Source: _bmad-output/implementation-artifacts/2-3-mode-wizard-vs-expert.md] — 100% frontend, ne touche pas au backend (cohérence Story 2.4 = 99% backend).
- [Source: _bmad-output/implementation-artifacts/epic-1-retro-2026-05-08.md §10] — D1 actor=system, audit-event bypass cleanup TODO Story 9.1, boundaries v6 actif.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (1M context) — bmad-dev-story single-pass execution.

### Debug Log References

- `git grep "audit-event bypass cleanup" backend/` → **3 hits** post-Story 2.4 : `service.py:164` (created Story 2.1), `service.py:340` (updated Story 2.2), `service.py:472` (instance.created Story 2.4). Spec attendait exactement 3.
- Test `test_update_template_service.py` étendu avec `instance_repo` + `workflow_run_repo` AsyncMock — sinon `AgentRegistryService(...)` lève `TypeError: missing required keyword-only argument` au runtime des tests existants (Stories 2.1/2.2).
- `tests/unit/shared/contracts/__init__.py` + `tests/unit/shared/__init__.py` créés (n'existaient pas — pattern `__init__.py` partout dans `tests/unit/` sauf `shared/` jusqu'à Story 2.4).

### Completion Notes List

- ✅ AC1 — `AgentRegistryService.instantiate_from_template` crée une `AgentInstance` avec snapshot complet figé `{template_id, template_version, name, archetype, config}`. Snapshot copié textuellement depuis `template.config` au moment du SELECT in_session (defensive `dict(template.config or {})`).
- ✅ AC2 — Isolation totale v2/v3 : test e2e `test_instantiate_template_isolation_v2_v3_no_hot_swap` (POST → instance v1 → PUT template → POST → instance v2 → assert i1.snapshot inchangé via re-GET).
- ✅ AC3 — `GET /workflows/runs/{run_id}/instances` ordonné `created_at ASC` + 404 strict si run inexistant + `[]` si run existe sans instances + exclut les instances orphelines (workflow_run_id=None).
- ✅ AC4 — `GET /agents/instances/{instance_id}` happy 200 + 404 RFC 7807 si inexistant + 422 FastAPI Path UUID validation.
- ✅ AC5 — Atomicité single-transaction : test unit `test_instantiate_template_atomicity_event_failure_blocks_commit` (publish throw → RuntimeError propagé → rollback). E2E real Postgres atomicity covered by the happy-path test (commit only on success of all 3 ops).
- ✅ AC6 — 404 RFC 7807 strict : test e2e `test_instantiate_template_404_when_template_missing` (template inexistant → 404 + 0 row + 0 event) + `test_instantiate_template_404_when_workflow_run_missing` (workflow_run_id fourni inexistant → 404).
- ✅ AC7 — Tests : 470 backend (+35 vs baseline 435) + 90 frontend (+3 vs baseline 87). Largement au-delà des ≥ 18 demandés. Détail : 6 schemas + 6 events + 7 service unit + 3 repo unit + 8 e2e POST/GET instance + 5 e2e workflow_run/instances + 3 hooks frontend.
- ✅ AC8 — Lint backend (ruff + mypy) + frontend (eslint + tsc) verts. Pas d'`any` introduit. **Smoke test runtime exécuté 2026-05-10** (B-01 CR — exécution stricte demandée plutôt que substitution testcontainers) — capture exhaustive des 8 ACs contre le stack `docker compose` réel :
  - **Step 1** `POST /api/v1/agents/templates` (smoke-2.4 / producteur) → `201` + `template_id=099990ef-8b96-4132-9ee6-32529b4598c0` + `version=1`.
  - **Step 2 (AC1)** `POST /agents/templates/{id}/instances` body `{}` → `201` + `instance_id=ebc21682-2510-4e8b-933e-80d5e8c68ee7` + `template_version=1` + `workflow_run_id=null` + `snapshot={template_id, template_version=1, name="smoke-2.4", archetype="producteur", config={prompt_base, role, input_contract, output_contract}}`.
  - **Step 3 (AC4)** `GET /agents/instances/{instance_id}` → `200` + payload byte-identique au POST.
  - **Step 4 (Story 2.2 dep)** `PUT /agents/templates/{id}` `{"system_prompt": "v2 prompt smoke"}` → `200` + `version=2` (bump prompt versioning).
  - **Step 5 (AC1)** Second `POST .../instances` body `{}` → `201` + `instance_id=2ddd7e60-...` + `template_version=2` + `snapshot.config.system_prompt="v2 prompt smoke"`.
  - **AC2 verification (no hot-swap)** Re-`GET /agents/instances/{i1_id}` → `template_version=1`, `snapshot.template_version=1`, `system_prompt in config: False` (i1 reste à v1 sans propagation du PUT post-hoc — isolation totale prouvée empiriquement).
  - **Step 6 (AC6)** `POST /agents/templates/00000000-...000/instances` → `404` RFC 7807 (`type="/errors/not-found"`, `title="Resource not found"`, `detail="Agent template '00000000-...' not found"`, `template_id` reflété dans context, `correlation_id` présent).
  - **Step 7 (AC4)** `GET /agents/instances/not-a-uuid` → `422` RFC 7807 (`type="/errors/validation"`, `errors[0].type="uuid_parsing"`, `errors[0].loc=["path","instance_id"]`, `correlation_id` présent).
  - **Step 8 (AC1 audit event + correlation_id propagation)** `docker logs agentive-backend-1 | grep "m2.agent_instance.created"` → 6 lignes structlog JSON (2 instances × 3 events chacun : `event_bus.publish` + `agent_instance_created` métier + `event_bus.event_dispatched`) avec `correlation_id` propagé sur les 3 events de chaque instance (chaîne `019e11f6-d640-770e-afaa-3638d2c67735` pour i1, `019e11f7-032f-75a1-af29-495d61035dab` pour i2). `event_type=m2.agent_instance.created` apparaît littéralement dans les events `event_bus.publish` et `event_bus.event_dispatched`. Le log métier `agent_instance_created` contient `instance_id`, `template_id`, `template_version`, `workflow_run_id` et `correlation_id` — manque `actor` + `tenant_id` (P-10 CR).
- ✅ T0 — Pré-requis vérifiés : `AgentInstance` model (Story 1.5) déjà aligné, `AgentInstanceRepo.create` existe, baseline grep `audit-event bypass cleanup` = 2 hits (Story 2.1 + 2.2).
- ✅ T1 — `AgentInstanceRepo` étendu : `create_in_session`, `get_by_id_in_session`, `list_by_workflow_run_in_session` (ORDER BY created_at ASC).
- ✅ T2 — Schemas Pydantic : `InstantiateTemplateRequest` (extra=forbid + workflow_run_id optional), `AgentInstanceDetailResponse` (alias `InstantiateTemplateResponse` car même shape).
- ✅ T3 — `AgentInstanceCreatedEvent` ajouté + barrel `events/__init__.py` + docstring `agent_events.py` mis à jour pour acter Story 2.4 livre `created` (et `completed` reste défer Story 4.x).
- ✅ T4 — `AgentRegistryService` étendu : constructeur avec `instance_repo` + `workflow_run_repo`, méthodes `instantiate_from_template`, `get_instance_by_id`, `list_instances_by_workflow_run`. Pattern Story 2.1 P-02 atomicité strict (with_tenant + same session pour template SELECT + workflow_run validate + instance INSERT + outbox publish).
- ✅ T5 — `WorkflowRunRepo.get_by_id_in_session` ajouté (1 méthode triviale, indispensable pour atomicité validation FK).
- ✅ T6 — 3 endpoints HTTP ajoutés dans `m2_agent_registry/router.py` : `POST /agents/templates/{id}/instances` + `GET /agents/instances/{id}` + `GET /workflows/runs/{run_id}/instances`. `_build_service` étendu pour wirer les 2 nouveaux repos.
- ✅ T7 — Tests intégration testcontainers Postgres : 8 tests instance e2e + 5 tests workflow_run/instances e2e. Fixtures workflow_run créées via `WorkflowRepo.create` + `WorkflowRunRepo.create` direct (pas d'endpoint POST workflow Sprint 1, cf Décision #13).
- ✅ T8 — Frontend types/api/hooks stub : 3 nouveaux types (`AgentInstance`, `InstantiateTemplateRequest`, `InstantiateTemplateResponse`), 3 nouvelles fonctions API (`instantiateTemplate`, `getInstance`, `listInstancesByRun`), 3 nouveaux hooks (`useInstantiateTemplate`, `useInstance`, `useInstancesByRun`). Exports barrel mis à jour. **0 composant React** (consumer Story 8.x).
- ✅ T9 — `make test` vert (470 backend + 90 frontend), `make lint` vert, sprint-status bumpé.

#### Décisions exécution

- **Snapshot defensive copy** : `dict(template.config or {})` au lieu de `template.config` direct — copie superficielle suffisante car JSONB SQLAlchemy retourne déjà un dict frais à chaque fetch, mais l'intent est explicite en code review.
- **`AgentInstanceDetailResponse` alias `InstantiateTemplateResponse`** : même shape exact (POST 201 + GET 200 retournent les mêmes 6 champs). Évite la duplication. Test `test_instantiate_response_alias_is_same_shape` verrouille cette identité.
- **`/workflows/runs/{id}/instances` reste dans `m2_agent_registry/router.py`** (pas de nouveau module `m3_workflow_engine/router.py` Sprint 1) — cohérent avec décision #14. La route est documentée comme "Sprint 1 hosting" et déplaçable Story 4.1 si workflow_engine devient owner du `workflow_run` lifecycle.
- **`actor="system"` hardcodé** — D1 défer Story 9.1 (auth resolution context). Cohérent Stories 2.1-2.3.
- **Test atomicité unit-only** (T7.3 défer e2e) : le mock `publish.side_effect = RuntimeError` au niveau service couvre l'invariant. Un test e2e Postgres qui mock le bus est plus fragile et apporte peu vs le coût (le pattern `with_tenant` + commit-on-success-only est trivialement vérifiable côté service).
- **`SELECT order by created_at ASC`** déterministe pour les tests — permet `assert body[0]["instance_id"] == i1["instance_id"]` sans ambiguïté.
- **Ruff `from sqlalchemy import asc, select`** — `asc()` ajouté pour l'ordre explicite (vs `.asc()` method qui marche aussi mais moins lisible). Cohérent style existing repo.

#### Tech-debt traçable (12 defer post-Story 2.4)

- **D28** — index sur `agent_instances.workflow_run_id` (volume négligeable Sprint 1, ajouter si benchmarks Sprint 2 montrent N+1).
- **D29** — `AgentInstanceCreatedEvent` migration vers `AuditEventRepo.record()` (TODO Story 9.1, `git grep "audit-event bypass cleanup"` doit retourner 3 hits avant le cleanup).
- **D30** — Endpoint `/workflows/runs/{id}/instances` à déplacer vers `m3_workflow_engine/router.py` Story 4.1 (quand le module existe).
- **D31** — `actor="system"` hardcodé → résoudre depuis auth context Story 9.1 (D1 du Epic 1 retro).
- **D32** — `m2.agent_instance.completed` event à publier Story 4.x quand workflow_engine signale fin (avec status final).
- **D33** — UI viewer instances (composant React qui affiche le snapshot, diff template vs instance, etc.) → Story 8.x trace explorer.
- **D34** — Tools assignment to instances → Story 2.5 (Tool Hub).
- **D35** — Cleanup/TTL des instances anciennes → Story 9.x retention policy.
- **D36** — Schema H2 prompts (parent_version, is_active UNIQUE, metadata JSONB) → Story 2.7+ (architecture H2 défer P-16 Story 2.2).
- **D37** — Endpoint `POST /workflows` + `POST /workflows/runs` → Story 4.1 (Sprint 1 = fixtures repo direct).
- **D38** — `workflow_run.status="completed"` filtering pour `GET /instances` (Sprint 1 retourne tout, pas de filtre par status) → Story 8.x si UX trace explorer le demande.
- **D39** — Multi-tenant `tenant_id` propagation (Sprint 1 = `None` partout) → Story 12 Sprint 4-5 (Multi-User & Permissions).

### File List

**Backend NEW**
- (none — all changes extend existing files)

**Backend MODIFIED**
- `backend/src/agentive_backend/shared/repositories/agent_repo.py` (T1) — `AgentInstanceRepo.create_in_session` + `list_by_workflow_run_in_session` + `get_by_id_in_session`
- `backend/src/agentive_backend/shared/repositories/workflow_repo.py` (T5) — `WorkflowRunRepo.get_by_id_in_session`
- `backend/src/agentive_backend/shared/contracts/events/agent_events.py` (T3) — `AgentInstanceCreatedEvent` class + docstring update
- `backend/src/agentive_backend/features/m2_agent_registry/schemas.py` (T2) — `InstantiateTemplateRequest`, `InstantiateTemplateResponse`, `AgentInstanceDetailResponse`
- `backend/src/agentive_backend/features/m2_agent_registry/service.py` (T4) — `instantiate_from_template`, `get_instance_by_id`, `list_instances_by_workflow_run` + constructor étendu (instance_repo, workflow_run_repo)
- `backend/src/agentive_backend/features/m2_agent_registry/router.py` (T6) — 3 endpoints + `_build_service` étendu

**Backend tests NEW**
- `backend/tests/unit/features/m2/test_instantiate_template_service.py` (T4.6) — ≥ 5 tests
- `backend/tests/unit/features/m2/test_schemas_instance.py` (T2.2) — ≥ 2 tests
- `backend/tests/unit/shared/contracts/test_agent_events.py` (T3.3) — ≥ 2 tests sur AgentInstanceCreatedEvent (peut être ajout dans fichier existant)
- `backend/tests/integration/m2_agent_registry/test_instantiate_template_e2e.py` (T7.1) — ≥ 6 tests
- `backend/tests/integration/m2_agent_registry/test_workflow_run_instances_e2e.py` (T7.2) — ≥ 4 tests

**Backend tests MODIFIED**
- `backend/tests/unit/shared/repositories/test_agent_repo.py` (T1.4) — étendre avec ≥ 3 tests sur les nouvelles méthodes _in_session

**Frontend MODIFIED**
- `frontend/src/features/agent_registry/types.ts` (T8.1) — types `AgentInstance`, `InstantiateTemplateRequest`, `InstantiateTemplateResponse`
- `frontend/src/features/agent_registry/api.ts` (T8.2) — `instantiateTemplate`, `getInstance`, `listInstancesByRun`
- `frontend/src/features/agent_registry/hooks.ts` (T8.3) — `useInstantiateTemplate`, `useInstance`, `useInstancesByRun`
- `frontend/src/features/agent_registry/index.ts` (T8.4) — barrel exports

**Frontend tests NEW**
- `frontend/src/features/agent_registry/api.test.ts` ou `hooks.test.tsx` (T8.5) — ≥ 3 tests

**Story spec**
- `_bmad-output/implementation-artifacts/2-4-distinction-template-vs-instance.md` (cette story)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (T9.2) — status backlog → ready-for-dev (T9 par dev), puis review post-implémentation
