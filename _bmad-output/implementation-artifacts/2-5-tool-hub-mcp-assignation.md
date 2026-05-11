# Story 2.5 : Tool Hub MCP + assignation outils à un agent

Status: done

> 🎯 **Cinquième story Epic 2 — Agent Platform.** Cette story livre la **plomberie M5 Tool Hub** : connexion de serveurs MCP (stdio + SSE), discovery + registry des outils exposés, et assignation d'outils à des agent-templates via une junction table. Couvre **FR22** (Tool Hub MCP) et **NFR19** (compatibilité stdio + SSE).
>
> **Cible architecturale** : `features/m5_tool_hub/` (registry + endpoints + service) + `infra/mcp/client.py` (MCP JSON-RPC client thin wrapper sur le SDK Python `mcp>=1.27.0` déjà en deps) + tables `tool_servers` / `tools` / `agent_template_tools` (Alembic migration nouvelle).
>
> **Pourquoi maintenant ?** Stories 2.6 (Sandbox bwrap), 2.7 (Playground), 2.8 (Review conversationnelle), 4.x (workflow_engine runtime) consomment toutes la notion d'outils assignés à un agent. Sans la plomberie, on ne peut ni découvrir ce qu'un serveur MCP expose, ni autoriser un agent à n'en utiliser qu'un sous-ensemble (principe du moindre privilège).
>
> **Anti-scope strict** :
>
> - **Exécution runtime des outils** (call MCP réel) → **Story 2.6** : Sandbox bwrap + setrlimit + timeout + whitelist réseau. Sprint 1 = REGISTRY + ASSIGNMENT uniquement, pas d'execution.
> - **Sandbox bwrap** → **Story 2.6** strict.
> - **Tool Playground** (tester un outil isolément) → **Story 2.7**.
> - **Re-discovery automatique** des outils sur reconnexion serveur MCP → manuel Sprint 1 (POST /tools/servers/{id}/rediscover défer Story 2.6+ ou cron M11 Scheduler).
> - **Credentials chiffrés Fernet** (NFR6 — clés API serveurs MCP qui requièrent auth) → **Story 9.2** (chiffrement at-rest). Sprint 1 = `connection_config` JSONB en clair (assumed safe single-user dev). Tracé en defer + warning code.
> - **Détection runtime "tool not in allowlist"** quand l'agent-instance veut appeler un outil non assigné → **Story 4.x workflow_engine** (Sprint 1 livre la junction table + l'invariant DB ; le check runtime arrive avec l'execution).
> - **Versioning des tools** (un même tool MCP qui change de schema) → défer Sprint 4+ ou Story 9.x.
> - **DELETE `/tools/servers/{id}`** → défer Sprint 1 (ON DELETE CASCADE sur `tools` + `agent_template_tools` mais pas d'endpoint REST). Si John veut nettoyer, il fera DELETE direct DB Sprint 1.
> - **Health-check serveurs MCP** (M6 Dashboard ping) → **Story 7.x dashboard**.
> - **Multi-tenant** → `tenant_id=None` partout Sprint 1, isolation arrive Story 12.
> - **Audit event bypass migration** → `m5.tool_server.connected`, `m5.tool.discovered`, `m2.agent_template.tool_assigned`, `m2.agent_template.tool_unassigned` publiés via `event_bus.publish_and_commit` avec TODO Story 9.1 (4 nouvelles entrées `git grep "audit-event bypass cleanup"` post-Story 2.5 = 7 hits attendus).
>
> **Pré-requis hérités (déjà disponibles — NE PAS recréer)** :
>
> - **`mcp>=1.27.0` Python SDK** (`backend/pyproject.toml:30` ✅). Expose `mcp.client.stdio.stdio_client(command, args, env?)` et `mcp.client.sse.sse_client(url, headers?)` qui retournent un `ClientSession` avec `.initialize()` + `.list_tools() -> ListToolsResult`.
> - **`infra/mcp/`** module placeholder (`__init__.py` vide). À PEUPLER en T2 avec `client.py` (thin wrapper sur SDK MCP) + futur `sandbox.py` (Story 2.6).
> - **`features/m5_tool_hub/`** module placeholder (`__init__.py` vide). À PEUPLER en T3-T5 avec `router.py` + `service.py` + `schemas.py`.
> - **`AgentTemplateRepo.get_by_id_in_session`** (Story 2.2) — utilisé pour valider `template_id` dans `POST /agents/templates/{id}/tools`.
> - **Pattern audit bypass** (Story 2.1 P-02) — `event_bus.publish_and_commit` dans la même session que les writes métier. TODO `audit-event bypass cleanup` sur 1 ligne pour `git grep` Story 9.1.
> - **Pattern atomicité single-transaction** (Story 2.1 P-02) — toutes les writes (DISCOVERY = INSERT tool_server + INSERT N tools + N audit events) dans `with_tenant`.
> - **`shared/exceptions.py`** : `NotFoundError`, `ConflictError`, `ValidationError`, `DependencyError` (déjà câblés au global handler RFC 7807 dans `app.main`).
> - **Pattern `_make_app` test** (factor P-09 Story 2.4 CR dans `tests/integration/m2_agent_registry/conftest.py`) — sera reproduit dans `tests/integration/m5_tool_hub/conftest.py` (helper `make_e2e_app` symétrique).
> - **Frontend feature `agent_registry`** : la page `/config/agents/{templateId}` (Story 2.3) existe déjà. Story 2.5 ajoute un **6ème accordéon** "Outils" en mode Expert + une **6ème étape** Wizard, OU bien — décision exécution préférée — un **panneau séparé** sous l'accordéon Identité (cf §"Décisions intégrées" #11).
> - **Pattern queryKey TanStack Query** : `["tool-servers"]`, `["tool-server", serverId]`, `["agent-template", templateId, "tools"]` (cohérent Story 2.4 D40 invalidation chains).
> - **Frontend `shared/components/ui/`** : `Checkbox` à VÉRIFIER en T0 — si absent, ajouter shadcn primitive (`pnpm dlx shadcn@latest add checkbox`).
>
> **Décisions intégrées (Epic 1 retro + Stories 2.1-2.4 code-reviews)** :
>
> 1. **Schéma 3 tables nouvelles** (Sprint 1 strict, défer indexes secondaires Sprint 2 si bench montre N+1) :
>    - **`tool_servers`** : `id UUID PK gen_random_uuid()`, `name TEXT NOT NULL`, `transport TEXT NOT NULL CHECK (transport IN ('stdio', 'sse'))`, `connection_config JSONB NOT NULL` (stdio = `{command: str, args: list[str], env?: dict}`, SSE = `{url: str, headers?: dict}`), `status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive'))`, `discovered_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `tenant_id UUID NULL`. **UNIQUE constraint** `(name, tenant_id)` NULLS NOT DISTINCT (cohérent Story 1.5 fix `agent_templates.uq_agent_template`).
>    - **`tools`** : `id UUID PK`, `server_id UUID FK REFERENCES tool_servers(id) ON DELETE CASCADE`, `name TEXT NOT NULL` (tool name from MCP discovery), `description TEXT NOT NULL DEFAULT ''`, `input_schema JSONB NOT NULL DEFAULT '{}'` (MCP tool's `inputSchema`), `output_schema JSONB NULL` (MCP tool n'a pas toujours `outputSchema`), `discovered_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `tenant_id UUID NULL`. **UNIQUE constraint** `(server_id, name)` (un même server ne peut pas exposer 2 tools même nom ; sur re-discovery, on UPDATE le row existant via UPSERT — cf décision #6).
>    - **`agent_template_tools`** : `agent_template_id UUID FK ON DELETE CASCADE`, `tool_id UUID FK ON DELETE CASCADE`, `assigned_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `assigned_by_actor TEXT NOT NULL DEFAULT 'system'` (D1 défer Story 9.1, hardcoded Sprint 1), `tenant_id UUID NULL`. **PRIMARY KEY composite** `(agent_template_id, tool_id)` — un agent ne peut pas avoir 2x le même tool, et c'est un index naturellement utile pour les queries "quels tools pour ce template".
> 2. **MCP discovery flow Sprint 1** : `POST /api/v1/tools/servers` → service ouvre une connexion MCP via le transport demandé → `await session.initialize()` → `tools_result = await session.list_tools()` → INSERT tool_server + N INSERT tools dans une seule transaction + N+1 audit events (`m5.tool_server.connected` + N × `m5.tool.discovered`). La connexion MCP est **fermée immédiatement après discovery** (Sprint 1 = pas de pool persistant ; chaque discovery est éphémère). Story 2.6 introduira un pool persistant si besoin.
> 3. **Compatibilité NFR19 stdio + SSE** — testé via 2 fixtures :
>    - **stdio** : `everything-server` officiel MCP (npm `@modelcontextprotocol/server-everything`) ou un mock subprocess Python qui implémente le protocole. **Décision exécution préférée** : mock Python local (pas de dépendance Node.js dans la CI) — cf T7.4.
>    - **SSE** : un test server SSE in-process via `httpx.AsyncClient` + `mcp.server.sse` côté backend test. Si trop lourd Sprint 1, défer SSE testing à un test marker `@pytest.mark.skip(reason="SSE smoke test deferred to Story 2.6")` avec **AU MINIMUM** un test unit qui prouve que `infra/mcp/client.py` choisit le bon transport (stdio vs sse) selon `tool_servers.transport` — voir T7.5.
> 4. **Discovery timeout** — la connexion MCP + `list_tools` doit se faire avec un timeout strict (`asyncio.wait_for(..., timeout=10.0)`). Sinon un serveur MCP malicieux ou hangulé bloque le request handler FastAPI indéfiniment. En cas de timeout → 503 `DependencyError` RFC 7807 (`type=/errors/dependency`, `title="MCP server discovery timeout"`).
> 5. **Audit events bypass M5** — 4 nouveaux events :
>    - `m5.tool_server.connected` payload `{server_id, name, transport, tools_count, actor, tenant_id}` — émis après discovery réussie.
>    - `m5.tool.discovered` payload `{tool_id, server_id, tool_name, actor, tenant_id}` — émis 1× par tool découvert.
>    - `m2.agent_template.tool_assigned` payload `{template_id, tool_id, tool_name, actor, tenant_id}` — émis par `POST /agents/templates/{id}/tools`.
>    - `m2.agent_template.tool_unassigned` payload `{template_id, tool_id, actor, tenant_id}` — émis par `DELETE /agents/templates/{id}/tools/{tool_id}`.
>    Tous avec TODO `audit-event bypass cleanup` sur 1 ligne. Total post-Story 2.5 : `git grep "audit-event bypass cleanup"` doit retourner **7 hits** (3 hits Stories 2.1-2.4 + 4 nouveaux Story 2.5).
> 6. **Re-discovery (UPSERT)** — si on POST `/tools/servers` avec `name` déjà existant, **rejet 409 ConflictError** Sprint 1 (le user doit DELETE puis re-POST OU appeler `POST /tools/servers/{id}/rediscover` qui n'existe pas Sprint 1 → tracé en defer D52). PAS d'UPSERT silencieux qui pourrait masquer un schema drift outils. Story 2.6 ou 2.7 introduira le rediscover endpoint.
> 7. **`tools.input_schema` JSONB shape** — copie textuelle du `inputSchema` retourné par MCP `list_tools()` (JSON Schema draft-07 typiquement). Aucune validation Pydantic Sprint 1 (le serveur MCP est l'autorité sur le schema de SES outils). Validation runtime des arguments tool call = Story 4.x.
> 8. **404 sémantique strict** (cohérent Story 2.4 décision #7) :
>    - `GET /tools/servers/{nonexistent}` → 404.
>    - `POST /agents/templates/{nonexistent}/tools` → 404 (template inexistant).
>    - `POST /agents/templates/{template_id}/tools` body `{tool_ids: [<nonexistent>]}` → 404 sur le tool_id manquant (PAS partial success).
>    - `DELETE /agents/templates/{template_id}/tools/{tool_id}` quand l'assignment n'existe pas → 404 (idempotency stricte ; ré-DELETE ne retourne pas 200).
>    - `GET /tools/servers` → 200 + `[]` (jamais 404).
> 9. **`POST /agents/templates/{id}/tools`** body shape : `{tool_ids: [UUID, UUID, ...]}` (REPLACE semantique : le body remplace TOUTE la liste actuelle d'assignments pour ce template ; les tools précédents non listés sont DELETE de la junction). Atomicité single-transaction. Émet 1 audit event `m2.agent_template.tool_assigned` par nouveau tool + 1 audit event `m2.agent_template.tool_unassigned` par tool retiré. **Décision exécution** : sémantique REPLACE plutôt qu'INCREMENT car l'UI checkbox grid envoie naturellement la liste complète.
> 10. **DELETE `/agents/templates/{id}/tools/{tool_id}`** = endpoint séparé pour désassigner UN seul tool (UX granulaire — clic sur un X dédié). Émet `m2.agent_template.tool_unassigned`. 404 si l'assignment n'existe pas.
> 11. **UI Frontend Sprint 1 — décision exécution** : les outils ne s'intègrent **PAS** dans le Wizard/Expert form Story 2.3 (qui est déjà bien chargé en 5 étapes/accordéons). À la place, un **panneau séparé** dédié `<AgentToolsPanel />` rendu **en bas de page** sous le formulaire de la page `/config/agents/{templateId}`. Pas de nouvelle route. Le panneau affiche : (a) liste des tools groupés par serveur MCP (UX-DR §"Tool grouping"), (b) checkbox par tool, (c) bouton "Sauvegarder les assignments" qui POST le `tool_ids` complet, (d) bouton secondaire "Configurer serveurs MCP" qui ouvre la page nouvelle `/config/tools` (page liste des serveurs avec bouton "Ajouter un serveur" + form).
> 12. **Page `/config/tools`** Sprint 1 — minimal CRUD : (a) liste des serveurs avec leur transport + nombre d'outils + status, (b) bouton "Ajouter un serveur MCP" qui ouvre un dialog modal avec form (nom + transport stdio/sse + connection_config JSONB textarea), (c) sur succès POST → invalidate `["tool-servers"]` queryKey + redirect vers la liste. Pas de DELETE Sprint 1 (cohérent décision #6, anti-scope). Pas d'edit du serveur (changer connection_config = créer un nouveau serveur).
> 13. **Frontend feature module nouvelle** : `frontend/src/features/tool_hub/` (cohérent avec `agent_registry/`, `theme/`). Contient : `types.ts`, `api.ts`, `hooks.ts`, `index.ts` (barrel), `AgentToolsPanel.tsx` (consumer), `ToolServerList.tsx` (page `/config/tools`), `AddToolServerDialog.tsx`, et leurs tests. Story 2.6+ ajoutera `ToolPlayground.tsx`.
> 14. **Pas de UI pour DELETE serveur, EDIT serveur, REDISCOVER serveur** Sprint 1 (anti-scope) — décisions tracées en defer D52-D54.
> 15. **Atomicité tests** — pattern `_make_app` factor P-09 Story 2.4 CR reproduit dans `tests/integration/m5_tool_hub/conftest.py` (helper `make_e2e_app` qui inclut le router m5 + les middleware standard).
> 16. **Smoke test runtime obligatoire AC8** (cohérent Story 2.4 B-01 résolu) — Story 2.5 = backend pur sur le path discovery (front consomme via API). Smoke test = (a) curl POST /tools/servers stdio (avec mock subprocess Python local), (b) curl GET /tools/servers, (c) curl POST /agents/templates/{id}/tools, (d) curl DELETE /agents/templates/{id}/tools/{tool_id}, (e) `docker logs ... | grep "m5.tool_server.connected\|m2.agent_template.tool_assigned"` → 4 events au minimum (1 connected + N discovered + 1 assigned + 1 unassigned). Capture exhaustive en Completion Notes au moment de l'implémentation.

## Story

**As John (développeur solo / Owner)**,
**I want** brancher des serveurs MCP (stdio + SSE) via le Tool Hub (M5) et assigner des outils spécifiques à des agent-templates,
**So that** chaque agent n'a que les capacités dont il a besoin (principe du moindre privilège, NFR10) et je peux faire évoluer le catalogue d'outils sans toucher aux templates qui les consomment.

## Acceptance Criteria

### AC1 — `POST /api/v1/tools/servers` connecte un serveur MCP et discovers ses outils

**Given** le module M5 Tool Hub est déployé (`features/m5_tool_hub/router.py` inclus dans `app.main`)
**And** le backend image contient le SDK `mcp>=1.27.0` (déjà en deps `pyproject.toml:30`)

**When** je POST `/api/v1/tools/servers` avec body :
```json
{
  "name": "my-mcp-server",
  "transport": "stdio",
  "connection_config": {"command": "python", "args": ["-m", "my.mcp.server"]}
}
```

**Then** la réponse est `201 Created` avec body :
```json
{
  "server_id": "<uuid>",
  "name": "my-mcp-server",
  "transport": "stdio",
  "status": "active",
  "discovered_at": "2026-05-10T...",
  "tools": [
    {"tool_id": "<uuid>", "name": "tool_a", "description": "...", "input_schema": {...}, "output_schema": null}
  ]
}
```

**And** `tool_servers` row INSERTed avec `status='active'`, `discovered_at=now()`.
**And** N rows `tools` INSERTed (1 par tool exposé par le serveur MCP), avec `server_id` FK + `name` + `description` + `input_schema` JSONB.
**And** `1 + N` rows `outbox_events` INSERTed dans **la même transaction** : `m5.tool_server.connected` + N × `m5.tool.discovered`.
**And** un log structlog `event=tool_server_connected` apparaît avec `correlation_id`, `server_id`, `transport`, `tools_count`, `actor=system`, `tenant_id=null`.

**Given** body avec `transport: "sse"` + `connection_config: {url: "https://example.com/mcp"}`
**When** je POST `/api/v1/tools/servers`
**Then** même contrat (201 + tools array). Le service utilise `mcp.client.sse.sse_client` au lieu de `stdio_client`.

**Given** le serveur MCP ne répond pas dans `10.0` secondes
**When** la discovery est lancée
**Then** réponse `503 DependencyError` RFC 7807 (`type=/errors/dependency`, `title="MCP server discovery timeout"`, `detail="Discovery timed out after 10s"`).
**And** AUCUNE row n'est créée (`tool_servers` ni `tools`) — atomicité P-02.

**Given** le body contient un `transport` non reconnu (ex `"http"`)
**When** POST
**Then** `422` Pydantic (Literal validation).

### AC2 — `POST /api/v1/agents/templates/{template_id}/tools` assigne (REPLACE) une liste d'outils à un template

**Given** un agent_template `T` existe (Story 2.1+2.2)
**And** 3 tools `T1`, `T2`, `T3` existent (depuis serveurs MCP enregistrés en AC1)

**When** je POST `/api/v1/agents/templates/{T.id}/tools` body `{tool_ids: ["<T1.id>", "<T2.id>"]}`

**Then** la réponse est `200 OK` avec body :
```json
{
  "template_id": "<T.id>",
  "assigned_tools": [
    {"tool_id": "<T1.id>", "name": "...", "server_id": "...", "assigned_at": "..."},
    {"tool_id": "<T2.id>", "name": "...", "server_id": "...", "assigned_at": "..."}
  ]
}
```
**And** la junction `agent_template_tools` contient exactement 2 rows pour `T.id` : `(T.id, T1.id)` et `(T.id, T2.id)`.
**And** 2 audit events `m2.agent_template.tool_assigned` (1 par nouveau tool) émis dans la même transaction.

**Given** le template a déjà `T1` + `T2` assignés
**When** je POST avec `{tool_ids: ["<T2.id>", "<T3.id>"]}` (REPLACE — T1 disparaît, T3 apparaît)
**Then** la junction devient exactement `{(T.id, T2.id), (T.id, T3.id)}`.
**And** 1 audit event `m2.agent_template.tool_unassigned` (T1) + 1 audit event `m2.agent_template.tool_assigned` (T3) — pas d'event pour T2 inchangé.

**Given** body `{tool_ids: []}` (clear all)
**When** POST
**Then** la junction perd toutes ses rows pour `T.id`. N events `tool_unassigned` (1 par tool retiré).

**Given** body `{tool_ids: ["<nonexistent_uuid>"]}`
**When** POST
**Then** `404 NotFoundError` RFC 7807 (`detail="Tool '<uuid>' not found"`). AUCUNE modification de la junction (atomicité — soit tous les tools sont valides, soit rien ne change).

**Given** `template_id` inexistant
**When** POST
**Then** `404 NotFoundError` (`detail="Agent template '<uuid>' not found"`).

### AC3 — `DELETE /api/v1/agents/templates/{template_id}/tools/{tool_id}` désassigne 1 tool spécifique

**Given** un assignment `(T.id, T2.id)` existe
**When** je DELETE `/api/v1/agents/templates/{T.id}/tools/{T2.id}`
**Then** réponse `204 No Content`.
**And** la row `agent_template_tools (T.id, T2.id)` est supprimée.
**And** 1 audit event `m2.agent_template.tool_unassigned` émis.

**Given** l'assignment n'existe pas (template OU tool OU les 2 inexistants OU l'assignment lui-même n'existe pas)
**When** DELETE
**Then** `404 NotFoundError` (`detail="Assignment (template=<uuid>, tool=<uuid>) not found"`). Pas d'idempotency Sprint 1 (re-DELETE = 404, pas 204).

### AC4 — Endpoints GET en lecture (`tool_servers`, `tools`, agent's tools)

**Given** des serveurs MCP enregistrés
**When** `GET /api/v1/tools/servers`
**Then** `200 OK` + array `[{server_id, name, transport, status, tools_count, discovered_at}, ...]`. Tools count agrégé via `SELECT count(*) FROM tools WHERE server_id = ?`. Ordonné `discovered_at DESC` (le plus récent en premier — UX naturelle).

**Given** un server existant
**When** `GET /api/v1/tools/servers/{server_id}`
**Then** `200 OK` + `{server_id, name, transport, status, connection_config, discovered_at, tools: [...]}` (détail complet avec tools array). 404 si inexistant.

**When** `GET /api/v1/agents/templates/{template_id}/tools`
**Then** `200 OK` + array d'AssignedTool grouped by server : `[{server: {...}, tools: [{tool_id, name, description, ...}]}]`. Cohérent avec UX-DR §"Tool grouping". 404 si template inexistant. 200 + `[]` si template existe sans tool.

### AC5 — Atomicité single-transaction (cohérent Story 2.1 P-02 / Story 2.4 AC5)

**Given** un test qui mock `event_bus.publish` pour qu'il throw sur le N-ième event (ex : N=2)
**When** `POST /tools/servers` est appelé sur un serveur MCP qui expose ≥ 3 tools
**Then** ni `tool_servers` ni AUCUN tool n'est committed (rollback atomique).
**And** un `SELECT count(*)` post-erreur montre 0 row supplémentaire dans les 2 tables.

Idem pour `POST /agents/templates/{id}/tools` (REPLACE atomique : soit toute la nouvelle liste est appliquée, soit rien ne change).

### AC6 — Frontend `<AgentToolsPanel />` rendu en bas de la page `/config/agents/{templateId}` (Story 2.3)

**Given** un user navigue vers `/config/agents/{templateId}` (Story 2.3 page existante)

**Then** sous le formulaire Wizard/Expert (en bas de page, `mt-8 border-t pt-6`), un panneau dédié `<AgentToolsPanel templateId={templateId} />` apparaît qui :
- Charge la liste de tools assignés via `useAgentTools(templateId)` (queryKey `["agent-template", id, "tools"]`)
- Charge la liste de tous les tools disponibles via `useTools()` (queryKey `["tools"]`) — agrégé par serveur MCP source
- Affiche un titre `"Outils MCP assignés à cet agent"` + une liste groupée par serveur MCP (UX-DR §"Tool grouping")
- Chaque tool a une checkbox pré-cochée si déjà assigné
- Un bouton "Sauvegarder les assignments" déclenche `useReplaceAgentTools(templateId).mutateAsync({tool_ids: <selected>})` (POST /agents/templates/{id}/tools REPLACE)
- Sur succès → toast `"Outils mis à jour"` + invalidate `["agent-template", templateId, "tools"]`
- Un bouton secondaire "Configurer les serveurs MCP" link vers `/config/tools`
- Si 0 serveur MCP enregistré → empty state avec CTA "Ajouter votre premier serveur MCP" link vers `/config/tools`

**And** le panneau ne change PAS le formulaire Wizard/Expert (Story 2.3 inchangé — anti-scope).

### AC7 — Page `/config/tools` (CRUD minimal serveurs MCP)

**Given** un user navigue vers `/config/tools`

**Then** la page rend :
- Header `"Serveurs MCP"` + bouton primaire `"Ajouter un serveur"`
- Liste tabulaire des serveurs (`useToolServers()` queryKey `["tool-servers"]`) avec colonnes : Nom, Transport (badge stdio/sse), Status, Outils (count), Découvert (date relative)
- Empty state si 0 serveur : "Aucun serveur MCP enregistré. Branchez votre premier serveur pour donner des capacités à vos agents." + CTA primaire
- Clic sur "Ajouter un serveur" ouvre un Dialog modal avec form :
  - Input `name` (required, min 1 max 255)
  - Select `transport` (stdio | sse)
  - Textarea `connection_config` (JSONB textarea, validation `JSON.parse` côté client + format hint dans helper text selon transport choisi)
  - Bouton "Connecter" déclenche `useCreateToolServer().mutateAsync(...)` (POST /tools/servers)
  - Sur succès → toast `"Serveur connecté ({tools_count} outils découverts)"` + close dialog + invalidate `["tool-servers"]`
  - Sur erreur 503 timeout → toast `"Le serveur MCP n'a pas répondu dans les 10 secondes."` + dialog reste ouvert
  - Sur erreur 409 conflict → toast `"Un serveur avec ce nom existe déjà."` + focus input name
  - Sur erreur 422 → toast inline du message Pydantic
- Pas de DELETE button Sprint 1 (anti-scope)
- Pas d'edit button Sprint 1 (anti-scope — re-créer un serveur si le config change)

### AC8 — Tests : ≥ 25 nouveaux, lint vert, smoke runtime exécuté

**Tests backend (≥ 18)** :

- **`test_tool_servers_service.py`** ≥ 5 tests : (1) discovery happy path stdio mock, (2) discovery happy path SSE mock OR transport router test (cf décision #3), (3) timeout 10s → DependencyError, (4) atomicité event throw → 0 row, (5) re-POST same name → ConflictError 409.
- **`test_tool_servers_e2e.py`** ≥ 5 tests : POST stdio + GET list + GET detail + POST 422 invalid transport + POST 503 timeout (mock subprocess that hangs).
- **`test_assign_tools_service.py`** ≥ 4 tests : REPLACE happy (2 added), REPLACE diff (1 removed + 1 added), REPLACE empty (clear all), 404 if any tool_id invalid.
- **`test_assign_tools_e2e.py`** ≥ 4 tests : POST happy 200 + GET assigned 200 + DELETE 204 + DELETE 404 (idempotency strict).
- **`test_tool_repos.py`** ≥ 3 tests sur ToolServerRepo + ToolRepo + AgentTemplateToolRepo (`_in_session` methods + CRUD basics).

**Tests frontend (≥ 7)** :

- **`tool_hub/api.test.ts`** OR **`tool_hub/hooks.test.tsx`** ≥ 5 tests : `useToolServers` GET URL, `useCreateToolServer` POST URL + body, `useReplaceAgentTools` POST URL + REPLACE shape + invalidation, `useDeleteAgentTool` DELETE URL + invalidation, `useAgentTools` queryKey + UUID guard.
- **`AgentToolsPanel.test.tsx`** ≥ 2 tests : render avec checkboxes pré-cochées + click "Sauvegarder" → POST.

**Régression** : `make test` reste vert sur baseline **post-Story 2.4 done** (backend ~471+ / frontend 91). Aucune modification des tests Stories 2.1-2.4.

**Smoke test runtime obligatoire** (B-01 Story 2.4 pattern — exécution capturée en Completion Notes au moment de l'implémentation) :

```bash
TOKEN="change_me"
BASE="http://127.0.0.1:8000"

# AC1 — POST stdio (mock server local Python)
SERVER=$(curl -s -X POST $BASE/api/v1/tools/servers \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"name":"smoke-mcp","transport":"stdio","connection_config":{"command":"python","args":["-m","tests.fixtures.mcp_mock_server"]}}')
SERVER_ID=$(...)

# AC2 — assign 1 tool
TEMPLATE=$(curl -s -X POST $BASE/api/v1/agents/templates -d '{"name":"smoke-2.5","archetype":"producteur"}' ...)
TEMPLATE_ID=$(...)
TOOL_ID=$(echo $SERVER | jq -r .tools[0].tool_id)
curl -s -X POST $BASE/api/v1/agents/templates/$TEMPLATE_ID/tools \
  -d "{\"tool_ids\": [\"$TOOL_ID\"]}"

# AC3 — DELETE assignment
curl -X DELETE $BASE/api/v1/agents/templates/$TEMPLATE_ID/tools/$TOOL_ID

# AC4 — GET endpoints
curl $BASE/api/v1/tools/servers
curl $BASE/api/v1/agents/templates/$TEMPLATE_ID/tools

# AC8 — logs
docker logs agentive-backend-1 | grep -E "m5.tool_server.connected|m2.agent_template.tool_assigned"
# → ≥ 4 events (1 connected + N discovered + 1 assigned + 1 unassigned)
```

**Lint + typecheck** : `make lint` (ruff + mypy + eslint + tsc) → 0 erreur. Pas d'`any`.

## Tasks / Subtasks

- [x] **T0 — Pré-requis vérifications**
  - [ ] T0.1 Vérifier `mcp>=1.27.0` dans `pyproject.toml` (déjà en deps ligne 30 ✅).
  - [ ] T0.2 Vérifier que `infra/mcp/__init__.py` existe (placeholder vide ✅).
  - [ ] T0.3 Vérifier que `features/m5_tool_hub/__init__.py` existe (placeholder vide ✅).
  - [ ] T0.4 Vérifier `git grep "audit-event bypass cleanup"` baseline = 3 hits (Stories 2.1+2.2+2.4). Post-Story 2.5 = **7 hits** attendus.
  - [ ] T0.5 Vérifier shadcn `Checkbox` primitive : `cat frontend/src/shared/components/ui/checkbox.tsx` ; si absent → `cd frontend && pnpm dlx shadcn@latest add checkbox`.

- [x] **T1 — Migration Alembic 3 nouvelles tables** (Décision #1)
  - [ ] T1.1 Créer `backend/alembic/versions/{date}_tool_hub_tables.py` (revision linéaire après `20260508_000000_agent_templates_unique_nulls_not_distinct.py`).
  - [ ] T1.2 `tool_servers` table (id PK gen_random_uuid, name TEXT, transport CHECK IN (stdio,sse), connection_config JSONB, status DEFAULT active CHECK IN (active,inactive), discovered_at TIMESTAMPTZ, created_at TIMESTAMPTZ, tenant_id NULL). UNIQUE constraint `(name, tenant_id)` NULLS NOT DISTINCT.
  - [ ] T1.3 `tools` table (id PK, server_id FK ON DELETE CASCADE, name TEXT, description TEXT DEFAULT '', input_schema JSONB DEFAULT '{}', output_schema JSONB NULL, discovered_at TIMESTAMPTZ, tenant_id NULL). UNIQUE `(server_id, name)`.
  - [ ] T1.4 `agent_template_tools` junction (agent_template_id FK ON DELETE CASCADE, tool_id FK ON DELETE CASCADE, assigned_at TIMESTAMPTZ, assigned_by_actor TEXT DEFAULT 'system', tenant_id NULL). PRIMARY KEY composite `(agent_template_id, tool_id)`.
  - [ ] T1.5 RLS `tenant_isolation` policy sur les 3 tables (Sprint 1 = `tenant_id IS NULL` toujours match, mais policy posée pour Story 12).
  - [ ] T1.6 `make alembic-upgrade` smoke test local : tables créées + migrations rollback OK.

- [x] **T2 — `infra/mcp/client.py` thin wrapper sur SDK MCP**
  - [ ] T2.1 Créer `backend/src/agentive_backend/infra/mcp/client.py` qui expose 1 fonction async `discover_tools(transport: Literal["stdio", "sse"], connection_config: dict, *, timeout: float = 10.0) -> list[ToolInfo]` où `ToolInfo` est une dataclass (`name: str, description: str, input_schema: dict, output_schema: dict | None`).
  - [ ] T2.2 Implémentation : selon `transport`, appelle `mcp.client.stdio.stdio_client(StdioServerParameters(command=..., args=..., env=...))` ou `mcp.client.sse.sse_client(url=..., headers=...)`. Wrap dans `asyncio.wait_for(..., timeout=timeout)` ; si `TimeoutError` → raise `MCPDiscoveryTimeoutError(timeout=timeout)` (custom exception qui sera traduite en DependencyError par le service).
  - [ ] T2.3 `await session.initialize()` puis `tools_result = await session.list_tools()` → mapper `tools_result.tools` (mcp.types.Tool) en `list[ToolInfo]`.
  - [ ] T2.4 Connexion **fermée immédiatement** après discovery (pas de pool persistant Sprint 1, cf décision #2).
  - [ ] T2.5 Tests `tests/integration/mcp/test_client.py` ≥ 2 tests : (1) stdio mock subprocess returns 2 tools, (2) timeout raises `MCPDiscoveryTimeoutError` (utiliser un mock subprocess qui dort 30s). Test SSE défer Story 2.6 OU 1 test transport routing si trivial.

- [x] **T3 — Repos `shared/repositories/tool_hub_repo.py`**
  - [ ] T3.1 Créer `ToolServerRepo(BaseRepo)` avec : `get_by_id`, `get_by_name(name, tenant_id)`, `create_in_session`, `list_all_in_session`, `get_with_tools_count_in_session` (LEFT JOIN tools + GROUP BY).
  - [ ] T3.2 Créer `ToolRepo(BaseRepo)` avec : `get_by_id`, `get_by_id_in_session`, `create_in_session`, `list_by_server_in_session`, `list_all_grouped_by_server_in_session` (pour `GET /tools` avec server group).
  - [ ] T3.3 Créer `AgentTemplateToolRepo(BaseRepo)` avec : `list_by_template_in_session(template_id) -> list[(AgentTemplate, Tool)]`, `assign_in_session(template_id, tool_id, actor)`, `unassign_in_session(template_id, tool_id) -> bool` (return True si une row supprimée, False si déjà absente — utilisé pour le 404 strict décision #8), `replace_in_session(template_id, new_tool_ids) -> tuple[added: list[UUID], removed: list[UUID]]` (REPLACE atomique cohérent décision #9).
  - [ ] T3.4 Étendre `shared/repositories/__init__.py` barrel : exports `ToolServerRepo`, `ToolRepo`, `AgentTemplateToolRepo`.
  - [ ] T3.5 Étendre `infra/db/models.py` avec 3 nouveaux models SQLAlchemy : `ToolServer`, `Tool`, `AgentTemplateTool` (composite PK via `__table_args__`). Mapped types stricts.
  - [ ] T3.6 Tests `tests/unit/repositories/test_tool_hub_repos.py` ≥ 3 tests (1 par repo).

- [x] **T4 — Schemas Pydantic `features/m5_tool_hub/schemas.py` + `features/m2_agent_registry/schemas.py` extension**
  - [ ] T4.1 Dans `features/m5_tool_hub/schemas.py` : `class CreateToolServerRequest(BaseModel)` (extra=forbid, name min 1 max 255, transport Literal["stdio","sse"], connection_config dict[str, Any]). `class ToolView(BaseModel)` (tool_id UUID, name, description, input_schema, output_schema). `class ToolServerView(BaseModel)` (server_id UUID, name, transport, status, discovered_at, tools_count int — pour GET list). `class ToolServerDetailView(ToolServerView)` (subclass + connection_config + tools: list[ToolView] — pour GET detail + POST response).
  - [ ] T4.2 Dans `features/m2_agent_registry/schemas.py` ajouter : `class ReplaceAgentToolsRequest(BaseModel)` (extra=forbid, tool_ids: list[UUID] avec max 100 defensive). `class AssignedToolView(ToolView)` (subclass + server_id + assigned_at). `class AgentToolsResponse(BaseModel)` (template_id UUID, assigned_tools: list[AssignedToolView] grouped by server_id côté frontend, mais ici flat).
  - [ ] T4.3 Tests `tests/unit/m2_agent_registry/test_schemas_tools.py` + `tests/unit/m5_tool_hub/test_schemas.py` ≥ 4 tests (extra=forbid, transport Literal, tool_ids max 100, list empty OK).

- [x] **T5 — Events Pydantic + barrel**
  - [ ] T5.1 Créer `shared/contracts/events/tool_events.py` (nouveau module) avec :
    - `ToolServerConnectedEvent` (event_type=`m5.tool_server.connected`, server_id UUID, name str, transport Literal, tools_count int >=0, actor str default "system", tenant_id UUID|None)
    - `ToolDiscoveredEvent` (event_type=`m5.tool.discovered`, tool_id UUID, server_id UUID, tool_name str, actor str, tenant_id UUID|None)
  - [ ] T5.2 Étendre `shared/contracts/events/agent_events.py` avec :
    - `AgentTemplateToolAssignedEvent` (event_type=`m2.agent_template.tool_assigned`, template_id UUID, tool_id UUID, tool_name str, actor str, tenant_id)
    - `AgentTemplateToolUnassignedEvent` (event_type=`m2.agent_template.tool_unassigned`, template_id UUID, tool_id UUID, actor str, tenant_id)
  - [ ] T5.3 Mettre à jour barrel `shared/contracts/events/__init__.py` avec les 4 nouveaux events.
  - [ ] T5.4 Tests `tests/unit/shared/contracts/test_tool_events.py` + extension `test_agent_events.py` ≥ 4 tests (event_type constants + minimal payload validation).

- [x] **T6 — Service `features/m5_tool_hub/service.py`**
  - [ ] T6.1 Créer `class ToolHubService` avec constructeur `(*, server_repo, tool_repo)` + méthodes :
    - `async def connect_server(self, *, name, transport, connection_config, tenant_id=None) -> ToolServerDetailView` :
      1. `with_tenant` ouvre session.
      2. SELECT tool_servers WHERE name=:name → si existe, raise `ConflictError(detail=f"Server '{name}' already registered")`.
      3. `await infra.mcp.client.discover_tools(transport, connection_config, timeout=10.0)` → si `MCPDiscoveryTimeoutError`, raise `DependencyError(detail="MCP server discovery timeout")`.
      4. INSERT tool_server → INSERT N tools → publish `m5.tool_server.connected` + N × `m5.tool.discovered` (loop) — toutes dans la MÊME transaction (cf P-02 atomicité Story 2.1).
      5. TODO Story 9.1 sur 1 ligne par publish call.
      6. Commit auto à `__aexit__`.
      7. emit_notify post-commit.
      8. Log structlog `tool_server_connected`.
    - `async def list_servers() -> list[ToolServerView]` (pattern Story 2.4 list_instances simple).
    - `async def get_server_detail(server_id) -> ToolServerDetailView`.
  - [ ] T6.2 Tests `tests/unit/m5_tool_hub/test_service.py` ≥ 5 tests (cf AC8 backend tests).

- [x] **T7 — Service `features/m2_agent_registry/service.py` extension** (cohérence — assignment vit dans m2 car la junction porte le préfixe `agent_template_*`)
  - [ ] T7.1 Étendre `AgentRegistryService` constructeur avec `tool_repo: ToolRepo` + `assignment_repo: AgentTemplateToolRepo`.
  - [ ] T7.2 `async def replace_template_tools(template_id, tool_ids, *, tenant_id=None) -> AgentToolsResponse` :
    1. `with_tenant` session.
    2. SELECT template via `template_repo.get_by_id_in_session` → 404 si None.
    3. Pour chaque `tool_id` dans `tool_ids` : SELECT tool via `tool_repo.get_by_id_in_session` → 404 si UN SEUL manque (pas de partial).
    4. `assignment_repo.replace_in_session(template_id, tool_ids, actor="system") -> (added, removed)`.
    5. Pour chaque `added` → publish `AgentTemplateToolAssignedEvent` (TODO 1 ligne).
    6. Pour chaque `removed` → publish `AgentTemplateToolUnassignedEvent` (TODO 1 ligne).
    7. Commit. emit_notify. Log.
    8. Retourne `AgentToolsResponse` avec liste actuelle des tools assignés.
  - [ ] T7.3 `async def list_template_tools(template_id) -> AgentToolsResponse` (404 si template inexistant ; retourne `assigned_tools=[]` si template existe sans tools).
  - [ ] T7.4 `async def unassign_tool(template_id, tool_id) -> None` :
    1. `with_tenant` session.
    2. `assignment_repo.unassign_in_session(template_id, tool_id) -> bool`.
    3. Si `False` (assignment n'existait pas) → raise `NotFoundError(detail=f"Assignment ({template_id}, {tool_id}) not found")`.
    4. Si `True` → publish `AgentTemplateToolUnassignedEvent` (TODO 1 ligne).
    5. Commit. emit_notify. Log.
  - [ ] T7.5 Mettre à jour `_build_service` (router.py) pour wirer les 2 nouveaux repos depuis `app.state.session_factory`. Étendre l'assertion P-16 Story 2.4 CR à 6 repos partageant la même session_factory.

- [x] **T8 — Endpoints HTTP**
  - [ ] T8.1 Créer `features/m5_tool_hub/router.py` avec : `POST /tools/servers`, `GET /tools/servers`, `GET /tools/servers/{server_id}`. `_build_tool_hub_service` helper similaire à m2.
  - [ ] T8.2 Étendre `features/m2_agent_registry/router.py` avec : `POST /agents/templates/{template_id}/tools` (200 OK), `GET /agents/templates/{template_id}/tools` (200 OK), `DELETE /agents/templates/{template_id}/tools/{tool_id}` (204 No Content).
  - [ ] T8.3 Inclure `m5_tool_hub.router` dans `app.main` (ajouter `app.include_router(m5_router, prefix="/api/v1")`).

- [x] **T9 — Frontend feature `tool_hub`**
  - [ ] T9.1 Créer `frontend/src/features/tool_hub/{types.ts, api.ts, hooks.ts, index.ts}` avec :
    - Types : `Transport = "stdio" | "sse"`, `ToolInfo`, `ToolServer`, `ToolServerDetail`, `CreateToolServerRequest`, `ReplaceAgentToolsRequest`, `AssignedTool`, `AgentToolsResponse`.
    - API : `listToolServers`, `getToolServer`, `createToolServer`, `listAgentTools(templateId)`, `replaceAgentTools(templateId, body)`, `deleteAgentTool(templateId, toolId)`.
    - Hooks : `useToolServers`, `useToolServer(id)`, `useCreateToolServer`, `useAgentTools(templateId)`, `useReplaceAgentTools(templateId)`, `useDeleteAgentTool(templateId)`. Invalidation chains : `useReplaceAgentTools.onSuccess` invalide `["agent-template", templateId, "tools"]`. `useCreateToolServer.onSuccess` invalide `["tool-servers"]`.
  - [ ] T9.2 Créer `frontend/src/features/tool_hub/AgentToolsPanel.tsx` (consumer dans `/config/agents/{templateId}`).
  - [ ] T9.3 Créer `frontend/src/app/routes/config/tools/index.tsx` (page liste serveurs) + `frontend/src/features/tool_hub/AddToolServerDialog.tsx` (modal form).
  - [ ] T9.4 Étendre `frontend/src/app/routes/config/agents/$templateId.tsx` (Story 2.3 page) pour rendre `<AgentToolsPanel templateId />` en bas (cf décision #11 — `mt-8 border-t pt-6` separator).
  - [ ] T9.5 Étendre `frontend/src/app/routes/__root.tsx` ou la sidebar (Story 1.8) pour ajouter une entrée "Outils" qui link vers `/config/tools`.
  - [ ] T9.6 Tests `tool_hub/api.test.ts` (ou `hooks.test.tsx`) ≥ 5 tests + `AgentToolsPanel.test.tsx` ≥ 2 tests.

- [x] **T10 — Tests intégration end-to-end**
  - [ ] T10.1 Créer `backend/tests/integration/m5_tool_hub/conftest.py` (helper `make_e2e_app` qui inclut les 2 routers m2 + m5).
  - [ ] T10.2 Créer `backend/tests/fixtures/mcp_mock_server.py` — un script Python invokable via `python -m tests.fixtures.mcp_mock_server` qui implémente le protocole MCP stdio minimal et expose 2 tools `echo` + `add` (pour les tests intégration discovery).
  - [ ] T10.3 Créer `backend/tests/integration/m5_tool_hub/test_tool_servers_e2e.py` (cf AC8 ≥ 5 tests).
  - [ ] T10.4 Créer `backend/tests/integration/m2_agent_registry/test_assign_tools_e2e.py` (cf AC8 ≥ 4 tests).
  - [ ] T10.5 Test atomicité (P-01 Story 2.4 CR pattern) : monkeypatch `service.publish` selectif sur `m5.tool.discovered` à throw sur le 2ème call → assert 0 row tool_servers + 0 row tools.

- [x] **T11 — Documentation + tech-debt tracking**
  - [ ] T11.1 Mettre à jour `agent_events.py` docstring pour acter Story 2.5 livre `tool_assigned/unassigned`.
  - [ ] T11.2 Mettre à jour `sprint-status.yaml` avec ligne récap Story 2.5 review (post-implémentation, avant CR).
  - [ ] T11.3 Tracer en defer (D52..D60) : DELETE /tools/servers endpoint, EDIT serveur, REDISCOVER endpoint, credentials chiffrés Fernet (Story 9.2), MCP connection pool persistant (Story 2.6+), tool versioning, health-check ping serveurs (Story 7.x), runtime tool execution (Story 2.6), runtime tool allowlist enforcement (Story 4.x).

## Dev Notes

### Pièges connus + leçons Stories 2.1-2.4

1. **Atomicité multi-event publish** (Story 2.1 P-02 + Story 2.4 AC5) — un POST /tools/servers émet `1 + N` events dans la MÊME transaction. Si N=10 tools et le 5ème publish throw, **tout** doit rollback (server + 4 tools déjà INSERTed + 5 events déjà publiés). Tester ce path en T10.5.
2. **TODO Story 9.1 sur 1 ligne unique** (P-15 Story 2.1 CR + P-04 Story 2.4 CR) — ne JAMAIS splitter le commentaire sur 2 lignes. Story 2.5 ajoute 4 nouveaux TODO → `git grep "audit-event bypass cleanup"` doit retourner exactement 7 hits post-2.5.
3. **`MCPDiscoveryTimeoutError` traduction** — Erreur infra `MCPDiscoveryTimeoutError` levée par `infra/mcp/client.py` est traduite en domain `DependencyError` par le service (cohérent Story 2.1 P-01 — features/ ne doit JAMAIS connaître les exceptions infra/sqlalchemy/mcp).
4. **REPLACE semantics atomicité** (décision #9) — `assignment_repo.replace_in_session` doit calculer `added/removed` AVANT les DML, puis : DELETE des `removed` + INSERT des `added` + N audit events `tool_unassigned` + M audit events `tool_assigned`. Tout en une seule transaction. Si on POST `{tool_ids: [T1, T2]}` deux fois consécutivement, le second appel produit 0 events (added=removed=[]).
5. **404 strict sur partial validation** (décision #8) — `POST /agents/templates/{id}/tools` body `{tool_ids: [valid, valid, NONEXISTENT]}` → 404 + AUCUNE modification. Pas de partial success ("on assigne les 2 valides et on ignore le 3ème"). Cohérent REST + plus simple à raisonner pour le client.
6. **Idempotency stricte sur DELETE** (décision #10) — re-DELETE un assignment déjà supprimé → 404 (PAS 204). Convention REST stricte, alignée avec Story 2.4 (re-GET sur un id supprimé = 404). Si John veut idempotency relaxed plus tard, ce sera un breaking change explicite Story 9.x.
7. **MCP transport `connection_config` shape libre** — Pas de validation Pydantic stricte du contenu (le serveur MCP est l'autorité). Sprint 1 = juste `dict[str, Any]`. Le user qui POST avec un mauvais shape (ex stdio sans `command`) verra l'erreur via le `MCPDiscoveryTimeoutError` ou via l'exception MCP SDK propagée en `DependencyError`. Acceptable Sprint 1 ; validation strict défer Sprint 4+.
8. **Frontend `Checkbox` shadcn primitive** (T0.5) — vérifier sa présence avant T9. Si absent, `pnpm dlx shadcn@latest add checkbox` ajoute `frontend/src/shared/components/ui/checkbox.tsx` + dépendance `@radix-ui/react-checkbox`.
9. **`tools.tools_count` agrégat dans GET /tools/servers** (AC4) — utiliser `LEFT JOIN tools GROUP BY tool_servers.id` dans la query SQLAlchemy (pas N+1). Test e2e doit valider qu'avec 3 servers, on a bien 3 SQL queries (1 SELECT JOIN agrégé), pas 3+N.
10. **Pattern `_make_app` factor** (P-09 Story 2.4 CR) — `tests/integration/m5_tool_hub/conftest.py` doit IMPORTER `make_e2e_app` depuis `tests/integration/m2_agent_registry/conftest.py` (réutilisation cross-feature) OU le re-définir si le wiring diffère (m5 router en plus). ~~Décision exécution : RE-DÉFINIR localement avec une variante `make_e2e_app_with_tool_hub` qui inclut les 2 routers — évite le coupling cross-feature des conftests.~~ **Amendement CR 2026-05-10** : décision exécution révisée → ÉTENDRE `make_e2e_app` dans `tests/integration/m2_agent_registry/conftest.py` pour inclure le m5 router, et faire que `tests/integration/m5_tool_hub/conftest.py` re-exporte juste depuis m2. Justification : (a) DRY — pas de duplication de wiring app/middleware/lifespan, (b) le coupling cross-feature est test-only (zéro impact runtime, zéro impact import-linter sur `src/`), (c) cohérent avec le pattern P-09 Story 2.4 CR qui mutualise `_make_app` + `_auth_headers` dans un conftest partagé. Trade-off accepté : `m2_agent_registry/conftest.py` importe `from agentive_backend.features.m5_tool_hub import router as tools_router` — acceptable car les conftests d'intégration cross-features sont par nature des points de composition.
11. **`actor="system"` hardcoded** (D1 défer Story 9.1) — cohérent Stories 2.1-2.4. À résoudre depuis auth context Story 9.1.
12. **`tenant_id=None` partout Sprint 1** — multi-tenant Story 12.
13. **`outbox_events.tenant_id` non rempli** (D44 Story 2.4 CR) — la dette traverse Story 2.5. À fermer Story 9.1+12.
14. **MCP SDK signatures** — `mcp.client.stdio.stdio_client` + `mcp.client.sse.sse_client` retournent un `AsyncContextManager[tuple[ReadStream, WriteStream]]`. Wrap manuel dans `ClientSession(read, write)` puis `await session.initialize()` puis `await session.list_tools()`. Voir [MCP Python SDK docs](https://github.com/modelcontextprotocol/python-sdk).
15. **Test fixture `mcp_mock_server.py`** (T10.2) — script Python standalone invoqué via `python -m tests.fixtures.mcp_mock_server`. Implémente `mcp.server.lowlevel.Server` + 2 tools triviaux. ~50 LOC. Évite la dépendance `@modelcontextprotocol/server-everything` (Node.js, alourdit la CI).
16. **Endpoint `POST /agents/templates/{id}/tools`** vit dans `m2_agent_registry/router.py` (cohérent décision #14 Story 2.4 — la junction `agent_template_*` appartient sémantiquement à m2, pas m5). m5 owne les serveurs MCP et les tools "génériques" ; m2 owne l'assignment. Story 4.1 pourra réorganiser quand workflow_engine arrive.

### Project Structure Notes

- **Backend NEW** : `infra/mcp/client.py` (T2), `features/m5_tool_hub/{router,service,schemas}.py` (T3-T8), `shared/repositories/tool_hub_repo.py` (T3), `shared/contracts/events/tool_events.py` (T5), `backend/alembic/versions/{date}_tool_hub_tables.py` (T1), `backend/tests/fixtures/mcp_mock_server.py` (T10.2).
- **Backend MODIFIED** : `infra/db/models.py` (+3 models), `shared/repositories/__init__.py` (+3 exports), `shared/contracts/events/__init__.py` (+4 events), `shared/contracts/events/agent_events.py` (+2 classes), `features/m2_agent_registry/{router,service,schemas}.py` (+3 endpoints + 3 service methods + 2 schemas), `app/main.py` (include_router m5).
- **Frontend NEW** : `features/tool_hub/{types,api,hooks,index}.ts`, `features/tool_hub/AgentToolsPanel.tsx`, `features/tool_hub/AddToolServerDialog.tsx`, `app/routes/config/tools/index.tsx`, `features/tool_hub/{api,hooks,AgentToolsPanel}.test.tsx`, **éventuellement** `shared/components/ui/checkbox.tsx` si T0.5 le requiert.
- **Frontend MODIFIED** : `app/routes/config/agents/$templateId.tsx` (+ `<AgentToolsPanel />` en bas), sidebar config (+ entrée "Outils").

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 2.5: Tool Hub MCP] — spec brut + 3 ACs originaux.
- [Source: _bmad-output/planning-artifacts/architecture.md#Sprint 1 — Tool Hub] — placement m5 + infra/mcp.
- [Source: _bmad-output/planning-artifacts/architecture.md#NFR19] — MCP stdio + SSE compatibility validated via SDK Python `mcp`.
- [Source: _bmad-output/planning-artifacts/architecture.md#FR22] — Tool Hub MCP coverage.
- [Source: _bmad-output/planning-artifacts/prd.md#FR22] — "Le système peut connecter des outils MCP aux agents via le Tool Hub (M5)".
- [Source: _bmad-output/planning-artifacts/prd.md#NFR10] — Sandbox outils MCP isolation (Story 2.6, hors-scope ici).
- [Source: backend/pyproject.toml:30] — `mcp>=1.27.0` SDK déjà en deps.
- [Source: backend/src/agentive_backend/infra/mcp/__init__.py] — placeholder à peupler T2.
- [Source: backend/src/agentive_backend/features/m5_tool_hub/__init__.py] — placeholder à peupler T3-T8.
- [Source: _bmad-output/implementation-artifacts/2-1-creer-agent-template-depuis-archetype.md] — atomicité P-02 + audit bypass pattern + 404 RFC 7807.
- [Source: _bmad-output/implementation-artifacts/2-2-configurer-agent-complet.md] — PUT pattern + atomicité.
- [Source: _bmad-output/implementation-artifacts/2-3-mode-wizard-vs-expert.md] — page `/config/agents/{templateId}` host pattern.
- [Source: _bmad-output/implementation-artifacts/2-4-distinction-template-vs-instance.md] — atomicité P-02 + REPLACE pattern + audit bypass + smoke runtime AC8 + factor `_make_app` P-09.
- [Source: _bmad-output/implementation-artifacts/epic-1-retro-2026-05-08.md §10] — D1 actor=system, audit-event bypass cleanup TODO Story 9.1, boundaries v6 actif.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (1M context) — bmad-dev-story single-pass execution.

### Debug Log References

- `git grep "audit-event bypass cleanup" backend/src/` → **8 hits** post-Story 2.5 :
  - 3 baseline (Stories 2.1+2.2+2.4) : m2/service.py L182 (created), L358 (updated), L494 (instance.created).
  - 4 nouveaux Story 2.5 : m2/service.py L656 (tool_assigned in replace), L670 (tool_unassigned in replace), L798 (tool_unassigned in unassign_tool) + m5/service.py L129 (server.connected + tool.discovered loop).
  - 1 docstring de référence (m5/service.py:13) — explique la convention grep, pas un TODO.
  - **Total TODOs réels : 7** (cohérent décision #5 spec). La docstring est intentionnelle (auto-référence pour le lecteur).
- `infra/mcp/client.py` — choix de wrapper le SDK `mcp>=1.27.0` plutôt que de réimplémenter le protocole JSON-RPC. `stdio_client(StdioServerParameters)` + `sse_client(url, headers)` retournent un AsyncContextManager `(read, write)` qu'on passe à `ClientSession(read, write)` puis `await session.initialize()` puis `await session.list_tools()` — cf inspection des signatures via `docker exec backend uv run python -c '...'` au début de T2.
- `tools.input_schema` mapping : la wire format MCP est camelCase (`inputSchema`), on snake_case côté DB (`input_schema`). Mapping via dataclass `ToolInfo` dans `infra/mcp/client.py`.
- Test atomicité E2E (T10.5 dans `test_assign_tools_e2e.py` `test_replace_assign_atomicity_publish_failure_rolls_back`) — pattern P-01 Story 2.4 CR : monkeypatch `service.publish` selectif sur `m2.agent_template.tool_assigned` à throw RuntimeError, wrapper le POST dans `pytest.raises(RuntimeError)`, asserter junction count == 0 post-erreur. La RuntimeError propage via httpx ASGITransport (pas de handler Exception global dans `_make_app`).
- Mock MCP server (`tests/fixtures/mcp_mock_server.py`) — script Python standalone invoqué par `python -m tests.fixtures.mcp_mock_server` côté subprocess. Implémente `mcp.server.lowlevel.Server` avec 2 tools triviaux (`echo` + `add`). ~50 LOC, 0 dépendance Node.js. Fonctionne en stdio uniquement Sprint 1 (SSE défer Story 2.6).

### Completion Notes List

- ✅ **AC1** — `POST /api/v1/tools/servers` (stdio happy path) : test e2e `test_create_tool_server_stdio_happy_path` discovers 2 tools (echo + add) via le mock MCP local. Réponse 201 + body shape complet (server_id, name, transport, status, tools array). DB rows : 1 server + 2 tools. Outbox : 1 + 2 = 3 audit events (`m5.tool_server.connected` + 2 × `m5.tool.discovered`) dans la même transaction. Timeout 10s appliqué via `asyncio.wait_for`. Test de discovery timeout via monkeypatch (raise `MCPDiscoveryTimeoutError`) → 503 RFC 7807 + 0 row.
- ✅ **AC2** — `POST /agents/templates/{id}/tools` REPLACE atomique : 4 tests e2e couvrent happy (assign 1), diff (1 added + 1 removed), clear all (empty list), 404 strict si tool inexistant (no partial). Junction state vérifié + audit events comptés.
- ✅ **AC3** — `DELETE /agents/templates/{id}/tools/{tool_id}` : 204 happy + 404 strict idempotency (re-DELETE = 404, pas 204). Vérifié via test `test_delete_template_tool_happy_path`.
- ✅ **AC4** — `GET /tools/servers` (list + count) + `GET /tools/servers/{id}` (detail + tools) + `GET /agents/templates/{id}/tools` (assigned list + 404 si template inexistant + [] si empty). 4 tests e2e dédiés.
- ✅ **AC5** — Atomicité single-tx (Story 2.1 P-02 pattern strict) : test e2e `test_replace_assign_atomicity_publish_failure_rolls_back` (monkeypatch `m2.agent_template.tool_assigned` publish à throw, vérifier junction count = 0). Pattern miroir P-01 Story 2.4 CR.
- ✅ **AC6** — Frontend `<AgentToolsPanel templateId={templateId} />` rendu en bas de `/config/agents/{templateId}` (Story 2.3 host inchangé — anti-scope respecté). Empty state si 0 serveur MCP enregistré + CTA. Checkboxes pré-cochées si déjà assignés. Bouton "Sauvegarder les assignments" disabled tant que la sélection == saved set. 2 tests composant : empty state + render+toggle+save.
- ✅ **AC7** — Page `/config/tools` (CRUD minimal serveurs) : header + bouton "Ajouter un serveur" + tableau des serveurs (nom, transport badge, status, count, date) + empty state CTA + dialog modal `AddToolServerDialog` avec form (nom + transport Select + connection_config JSONB textarea avec template auto-rempli selon transport choisi). Toast handling pour 409/503/422. Pas de DELETE/EDIT serveur (anti-scope).
- ✅ **AC8** — Tests : **491 backend (+21 vs baseline 470 post-2.4) + 99 frontend (+8 vs baseline 91)** = **29 nouveaux** (spec demandait ≥ 25). 0 régression. Lint (ruff + mypy + eslint + tsc) vert. Sidebar config étendue avec entrée "Outils MCP" link vers `/config/tools`.
- ✅ **AC8 Smoke runtime (P-04 CR 2026-05-10)** — exécuté contre stack Docker dev (commit 741c6db + Cluster A patches via uvicorn `--reload`). Capture exhaustive ci-dessous.

#### Smoke runtime — Capture 2026-05-10 21:02 UTC

**Pré-requis** : `docker compose up -d` + `docker compose exec backend uv run alembic upgrade head` (migration `20260510000000_tool_hub_tables` appliquée) + `AGENTIVE_ALLOW_MCP_REGISTRATION=true` dans `.env` (P-23 gate).

**Step 1 — POST /agents/templates** (baseline m2 pour avoir un template_id) :

```
{
    "template_id": "050c5a9d-210f-4e24-937f-fba7b55837a7",
    "name": "smoke-25-prod-459481",
    "archetype": "producteur",
    "version": 1,
    "created_at": "2026-05-10T21:02:32.695596Z"
}
```

**Step 2 — AC1 POST /tools/servers stdio** (mock MCP, body inclut `env.SECRET_TOKEN=hunter2-must-not-leak` pour vérifier P-03) :

```
{
    "server_id": "eb20254f-b043-4691-8533-258e6390067e",
    "name": "smoke-25-mcp-459481",
    "transport": "stdio",
    "status": "active",
    "connection_config": {
        "env": {"_redacted_keys": ["SECRET_TOKEN"]},
        "args": ["-m", "tests.fixtures.mcp_mock_server"],
        "command": "python"
    },
    "discovered_at": "2026-05-10T21:02:38.455073Z",
    "tools": [
        {"tool_id": "f18e481a-…", "name": "echo", "description": "Echo the input string back", "input_schema": {…}, "output_schema": null},
        {"tool_id": "06f7b9d4-…", "name": "add",  "description": "Add two integers and return the sum", "input_schema": {…}, "output_schema": null}
    ]
}
```

P-03 redaction check (count of "hunter2-must-not-leak" dans la réponse) : **0** ✅. Le secret n'a jamais quitté la DB.

**Step 3 — AC4 GET /tools/servers** (list lean view + tools_count) :

```
[{"server_id": "eb20254f-…", "name": "smoke-25-mcp-459481", "transport": "stdio", "status": "active", "tools_count": 2, "discovered_at": "…"}]
```

**Step 4 — AC4 GET /tools/servers/{id}** : same body as step 2, redaction sticks identiquement (idempotent).

**Step 5 — AC2 POST /agents/templates/{id}/tools** (REPLACE atomic avec les 2 tool_ids) — réponse P-01 enrichie (`assigned_at` + `input_schema` + `output_schema` ajoutés au contrat) :

```
{
    "template_id": "050c5a9d-…",
    "assigned_tools": [
        {
            "tool_id": "06f7b9d4-…", "name": "add", "description": "…",
            "server_id": "eb20254f-…",
            "input_schema": {"type": "object", "required": ["a","b"], "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}},
            "output_schema": null,
            "assigned_at": "2026-05-10T21:02:58.721627Z"
        },
        {
            "tool_id": "f18e481a-…", "name": "echo", "description": "…",
            "server_id": "eb20254f-…",
            "input_schema": {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}},
            "output_schema": null,
            "assigned_at": "2026-05-10T21:02:58.721627Z"
        }
    ]
}
```

P-01 contract check (per tool keys) : `['assigned_at', 'description', 'input_schema', 'name', 'output_schema', 'server_id', 'tool_id']` ✅ (7 fields, vs 4 avant fix).

**Step 6 — AC4 GET /agents/templates/{id}/tools** : retourne la même shape que step 5, reflet exact post-REPLACE.

**Step 7 — AC3 DELETE /agents/templates/{id}/tools/{tool_id}** (idempotency stricte) :

- 1ère DELETE : `204 No Content`
- 2ème DELETE : `404 Not Found` + RFC 7807 :
  ```
  {"type": "/errors/not-found", "status": 404, "detail": "Assignment (template=…, tool=…) not found", ...}
  ```

**Step 8 — docker logs grep audit events** (récents 2 minutes, sur 2 smokes consécutifs car le 1er a échoué pré-migration — cumul) :

```
$ docker compose logs backend --since 2m | grep -oE 'm5\.tool_server\.connected|m5\.tool\.discovered|m2\.agent_template\.tool_assigned|m2\.agent_template\.tool_unassigned' | sort | uniq -c
      4 m2.agent_template.tool_assigned
      2 m2.agent_template.tool_unassigned
      4 m5.tool.discovered
      2 m5.tool_server.connected
```

**Step 9 — DB outbox_events** (uniquement pour le smoke réussi : 1 server + 2 tools + 2 assignments + 1 unassign) :

```
$ docker compose exec db psql -U agentive_owner -d agentive -c "SELECT event_type, count(*) FROM outbox_events WHERE event_type LIKE 'm5.tool%' OR event_type LIKE 'm2.agent_template.tool_%' GROUP BY event_type ORDER BY event_type;"

            event_type             | count
-----------------------------------+-------
 m2.agent_template.tool_assigned   |     2
 m2.agent_template.tool_unassigned |     1
 m5.tool.discovered                |     2
 m5.tool_server.connected          |     1
(4 rows)
```

**Verdict AC8** : ✅ 8 ACs end-to-end validés contre le stack runtime. Cluster A des patches CR 2026-05-10 appliqués et exercés (P-23 gate, P-03 redaction, P-01 contract enrichi, P-05 emit_notify event_type correct vérifié indirectement par les 4 events distincts dans logs+DB).
- ✅ T0 — Pré-requis vérifiés : `mcp>=1.27.0` ✅, `infra/mcp/__init__.py` placeholder ✅, `features/m5_tool_hub/__init__.py` placeholder ✅, baseline grep = 3 hits Stories 2.1+2.2+2.4 ✅, `Checkbox` shadcn primitive **AJOUTÉE** (était absente — `frontend/src/shared/components/ui/checkbox.tsx` créé avec radix-ui pattern + lucide CheckIcon).
- ✅ T1 — Migration Alembic `20260510_000000_tool_hub_tables.py` : 3 tables (tool_servers + tools + agent_template_tools) avec FK CASCADE, UNIQUE NULLS NOT DISTINCT (cohérent Story 2.1 P-07), CHECK constraints, RLS tenant_isolation policy + GRANTS agentive_app.
- ✅ T2 — `infra/mcp/client.py` (~150 LOC) : `discover_tools(transport, connection_config, timeout=10s)` + `MCPDiscoveryTimeoutError` + `ToolInfo` dataclass.
- ✅ T3 — `shared/repositories/tool_hub_repo.py` : `ToolServerRepo`, `ToolRepo`, `AgentTemplateToolRepo` (avec `replace_in_session` qui retourne `(added, removed)` pour les events). `models.py` étendu avec 3 SQLAlchemy models. Barrel `__init__.py` mis à jour.
- ✅ T4 — Schemas Pydantic : 5 nouveaux côté m5 (`CreateToolServerRequest`, `ToolView`, `ToolServerView`, `ToolServerDetailView`, types `Transport`/`ToolServerStatus`) + 3 côté m2 (`ReplaceAgentToolsRequest`, `AssignedToolView`, `AgentToolsResponse`).
- ✅ T5 — 4 nouveaux events : `tool_events.py` (`ToolServerConnectedEvent` + `ToolDiscoveredEvent`) + extension `agent_events.py` (`AgentTemplateToolAssignedEvent` + `AgentTemplateToolUnassignedEvent`). Barrel `events/__init__.py` mis à jour.
- ✅ T6 — `ToolHubService.connect_server` (single-tx atomique : 1 server INSERT + N tools INSERT + 1+N audit events) + `list_servers` + `get_server_detail`. Discovery hors-tx pour ne pas bloquer connection 10s.
- ✅ T7 — `AgentRegistryService` étendu : `replace_template_tools` (REPLACE atomique avec diffs added/removed → events), `list_template_tools`, `unassign_tool`. Constructor étendu avec `tool_repo` + `assignment_repo`. Tests existants `test_update_template_service.py` + `test_instantiate_template_service.py` mis à jour avec les 2 nouveaux AsyncMock kwargs.
- ✅ T8 — 6 nouveaux endpoints : 3 m5 (`POST/GET tools/servers` + `GET tools/servers/{id}`) + 3 m2 (`POST/GET agents/templates/{id}/tools` + `DELETE agents/templates/{id}/tools/{tool_id}`). `_build_service` m2 étendu pour wirer 6 repos (assertion P-16 Story 2.4 CR mise à jour). m5 router inclus dans `app.main`.
- ✅ T9 — Frontend feature `tool_hub` : 4 fichiers TS (types, api, hooks, index barrel) + 2 composants (`AgentToolsPanel`, `AddToolServerDialog`) + 1 page `/config/tools/index.tsx` + 2 fichiers tests (`hooks.test.tsx` 6 tests, `AgentToolsPanel.test.tsx` 2 tests). Sidebar config étendue avec entrée "Outils MCP".
- ✅ T10 — Tests intégration : `tests/fixtures/mcp_mock_server.py` (mock MCP server stdio) + `tests/integration/m5_tool_hub/conftest.py` (re-export depuis m2 + include m5 router) + `test_tool_servers_e2e.py` (7 tests) + `test_assign_tools_e2e.py` (8 tests). Pattern Stories 2.4 P-09 _make_app factor étendu pour inclure m5 router.
- ✅ T11 — Documentation + sprint-status : ligne récap Story 2.5 review ajoutée + bump status. 9 nouveaux defer D52-D60 tracés (DELETE serveur endpoint, EDIT, REDISCOVER, Fernet credentials Story 9.2, MCP pool persistant Story 2.6+, tool versioning, health-check ping Story 7.x, runtime tool execution Story 2.6, runtime allowlist enforcement Story 4.x).

#### Décisions techniques d'implémentation

- **MCP discovery hors transaction DB** : la connexion subprocess MCP + `list_tools()` dure jusqu'à 10s ; on ne tient PAS la connection DB ouverte pendant ce temps. La transaction DB n'est ouverte qu'au step 3 (INSERT server + N tools + 1+N events). Évite les lock contention sur les pools.
- **Duplicate check pre-discovery** : on SELECT par name AVANT la discovery (étape 1). Si conflit → 409 sans payer le coût d'un timeout MCP. Évite le gaspillage de 10s pour un nom déjà pris.
- **`replace_in_session` retourne `(added, removed)`** : le service utilise les diffs pour émettre exactement N events `tool_assigned` + M events `tool_unassigned` (pas d'events spurious pour les tools inchangés). Pattern miroir Story 2.4 P-02 atomicité.
- **`tools.input_schema` JSONB shape libre** : aucune validation Pydantic Sprint 1 (le serveur MCP est l'autorité). Stockage textuel via `dict(tool.inputSchema)` après mapping camelCase → snake_case.
- **Mock MCP server stdio uniquement** : SSE testing happy-path défer Story 2.6 (rolls into **D59** runtime execution — amendement P-19 CR 2026-05-10 : la mention initiale "D55" était une typo, D55 = Fernet encryption uniquement). Le test transport routing positif (P-09 CR Cluster B) couvre le dispatch SSE via fake `sse_client`.
- **Frontend `AgentToolsPanel` panneau séparé** (décision #11 spec) : RENDU en bas de `/config/agents/{templateId}`, PAS intégré dans le Wizard/Expert form Story 2.3 (qui reste inchangé). UX cohérente avec UX-DR §"Tool grouping".
- **`useQueries` TanStack** dans `AgentToolsPanel` pour fetcher en parallèle le détail (avec tools array) de chaque serveur listé. Cache scopé par `["tool-server", serverId]` queryKey (cohérent invalidation Story 2.4 D40 chains).
- **`AddToolServerDialog` JSON config validation client** : `JSON.parse` + `Array.isArray(parsed) === false` côté client AVANT POST. Évite un round-trip 422 pour les erreurs JSON triviales.
- **404 strict + idempotency strict** sur DELETE assignment (cohérent Story 2.4 décision #7 + #10) : re-DELETE = 404, PAS 204. Convention REST stricte.
- **Pattern P-09 Story 2.4 CR _make_app** : `make_e2e_app` factor étendu pour inclure m5 router (Sprint 1 + Story 2.5+ tests cross-feature). `tests/integration/m5_tool_hub/conftest.py` re-importe juste depuis m2.

#### 9 nouveaux defer Story 2.5 (D52-D60)

- **D52** — `DELETE /tools/servers/{id}` endpoint (Sprint 1 anti-scope ; ON DELETE CASCADE en place sur les FK, mais pas d'UI/REST).
- **D53** — `PUT /tools/servers/{id}` EDIT (changer connection_config). Sprint 1 = re-créer via DELETE+POST (D52+POST).
- **D54** — `POST /tools/servers/{id}/rediscover` (re-fetch tools depuis le serveur MCP, UPSERT diff). Sprint 1 = manuel.
- **D55** — Fernet encryption sur `tool_servers.connection_config` (NFR6 chiffrement at-rest credentials). → Story 9.2.
- **D56** — MCP connection pool persistant (réutiliser la connexion entre plusieurs `list_tools()`/`call_tool()`). → Story 2.6 quand l'execution runtime arrive.
- **D57** — Tool versioning (un même tool MCP qui change de schema entre 2 discoveries). → Sprint 4+.
- **D58** — Health-check ping serveurs MCP (vérifier `status='active'` automatiquement). → Story 7.x dashboard.
- **D59** — Runtime tool execution (call MCP via sandbox bwrap). → Story 2.6.
- **D60** — Runtime tool allowlist enforcement (l'agent-instance ne peut appeler QUE ses tools assignés). → Story 4.x workflow_engine.

### File List

**Backend NEW**
- `backend/alembic/versions/20260510_000000_tool_hub_tables.py` (T1)
- `backend/src/agentive_backend/infra/mcp/client.py` (T2)
- `backend/src/agentive_backend/shared/repositories/tool_hub_repo.py` (T3)
- `backend/src/agentive_backend/features/m5_tool_hub/router.py` (T8)
- `backend/src/agentive_backend/features/m5_tool_hub/schemas.py` (T4)
- `backend/src/agentive_backend/features/m5_tool_hub/service.py` (T6)
- `backend/src/agentive_backend/shared/contracts/events/tool_events.py` (T5)
- `backend/tests/fixtures/__init__.py` (empty)
- `backend/tests/fixtures/mcp_mock_server.py` (T10.2)
- `backend/tests/integration/m5_tool_hub/__init__.py` (empty)
- `backend/tests/integration/m5_tool_hub/conftest.py` (T10.1)
- `backend/tests/integration/m5_tool_hub/test_tool_servers_e2e.py` (T10.3) — 7 tests
- `backend/tests/integration/m2_agent_registry/test_assign_tools_e2e.py` (T10.4) — 8 tests
- `backend/tests/unit/m5_tool_hub/__init__.py` (empty placeholder)

**Backend MODIFIED**
- `backend/src/agentive_backend/infra/db/models.py` (T3) — +3 SQLAlchemy models (`ToolServer`, `Tool`, `AgentTemplateTool`) + import `CheckConstraint`/`PrimaryKeyConstraint`.
- `backend/src/agentive_backend/shared/repositories/__init__.py` (T3) — exports +3.
- `backend/src/agentive_backend/shared/contracts/events/__init__.py` (T5) — exports +4.
- `backend/src/agentive_backend/shared/contracts/events/agent_events.py` (T5) — +2 classes (tool_assigned/unassigned events).
- `backend/src/agentive_backend/features/m5_tool_hub/__init__.py` (T8) — exports `router` + `ToolHubService`.
- `backend/src/agentive_backend/features/m2_agent_registry/schemas.py` (T4) — +3 schemas (ReplaceAgentToolsRequest, AssignedToolView, AgentToolsResponse).
- `backend/src/agentive_backend/features/m2_agent_registry/service.py` (T7) — +3 methods (replace_template_tools, list_template_tools, unassign_tool) + constructor étendu.
- `backend/src/agentive_backend/features/m2_agent_registry/router.py` (T8) — +3 endpoints + `_build_service` étendu (P-16 assertion 6 repos).
- `backend/src/agentive_backend/app/main.py` (T8) — `include_router(tools_router, prefix="/api/v1")`.
- `backend/tests/integration/m2_agent_registry/conftest.py` (T10.1) — `make_e2e_app` étendu pour inclure m5 router.
- `backend/tests/unit/m2_agent_registry/test_update_template_service.py` (T7) — fixture étendue avec 2 nouveaux AsyncMock kwargs.
- `backend/tests/unit/m2_agent_registry/test_instantiate_template_service.py` (T7) — fixture étendue avec 2 nouveaux AsyncMock kwargs.

**Frontend NEW**
- `frontend/src/shared/components/ui/checkbox.tsx` (T0.5)
- `frontend/src/features/tool_hub/types.ts` (T9.1)
- `frontend/src/features/tool_hub/api.ts` (T9.1)
- `frontend/src/features/tool_hub/hooks.ts` (T9.1)
- `frontend/src/features/tool_hub/index.ts` (T9.1)
- `frontend/src/features/tool_hub/AgentToolsPanel.tsx` (T9.2)
- `frontend/src/features/tool_hub/AddToolServerDialog.tsx` (T9.3)
- `frontend/src/features/tool_hub/hooks.test.tsx` (T9.6) — 6 tests
- `frontend/src/features/tool_hub/AgentToolsPanel.test.tsx` (T9.6) — 2 tests
- `frontend/src/app/routes/config/tools/index.tsx` (T9.3)

**Frontend MODIFIED**
- `frontend/src/app/routes/config/agents/$templateId.tsx` (T9.4) — `<AgentToolsPanel />` rendu en bas.
- `frontend/src/app/routes/config/index.tsx` (T9.5) — entrée "Outils MCP" ajoutée dans la sidebar.

**Story spec**
- `_bmad-output/implementation-artifacts/2-5-tool-hub-mcp-assignation.md` (T11) — Status: review + Tasks/Subtasks tous cochés [x] + Dev Agent Record rempli.
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (T11) — bump 2-5-tool-hub-mcp-assignation: in-progress → review + ligne récap.

---

### Amendement CR 2026-05-10 — D-01 admin-gate Sprint 1 (promu defer → patch P-23)

**Décision tranchée par John** : `POST /tools/servers` accepte du code arbitraire (subprocess.Popen sur `command` user-fourni) + URL non-filtrée pour SSE (`sse_client(url=...)`). Sans sandbox bwrap (Story 2.6 D59) ni allowlist runtime (Story 4.x D60), tout utilisateur authentifié peut déclencher RCE sur le backend ou probe les services internes (SSRF, ex. metadata cloud `169.254.169.254`).

**Sprint 1 = single-user MVP** (Story 1.7 auth token statique, pas de rôles), donc "admin-gate" se traduit en :

- **Feature flag env `AGENTIVE_ALLOW_MCP_REGISTRATION`** (default `false`) à ajouter dans `shared/config.py` `Settings`.
- **Gate `POST /tools/servers`** : si flag désactivé → 403 RFC 7807 avec `title="MCP server registration disabled"` + `detail` qui pointe Story 2.6 sandbox.
- **GET endpoints non gatés** (read-only, surface 0 RCE/SSRF) — `GET /tools/servers`, `GET /tools/servers/{id}`, `GET /agents/templates/{id}/tools`, `POST/DELETE /agents/templates/{id}/tools/{tool_id}` (assignment ne touche pas au subprocess/réseau).
- **`.env.example`** : ajouter la ligne commentée `# AGENTIVE_ALLOW_MCP_REGISTRATION=false  # RCE/SSRF risk — flip à true uniquement en dev/test ou après Story 2.6 sandbox.`
- **Smoke runtime AC8** (P-04) : exécuter avec `AGENTIVE_ALLOW_MCP_REGISTRATION=true` ; ajouter aussi un test e2e du chemin 403 (flag false → POST refusé).
- **Test e2e existant** (`test_create_tool_server_stdio_happy_path`) : doit forcer le flag à `true` dans la fixture (`monkeypatch.setenv` ou override Settings).

**Justification** : opt-in explicite vs default-secure. Quiconque déploie en prod sans avoir lu la doc ne s'expose pas par défaut.

**À fermer définitivement Story 2.6** : sandbox bwrap + allowlist URL → le flag peut redevenir `true` par défaut (ou disparaître si la sandbox couvre tout).
