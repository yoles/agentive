# Story 2.6 : Exécution des outils MCP dans sandbox bwrap + setrlimit

Status: ready-for-dev

> 🎯 **Sixième story Epic 2 — Agent Platform.** Cette story livre l'**exécution runtime sandboxée** des outils MCP enregistrés en Story 2.5 : appel d'un outil via `infra/mcp/sandbox.py` qui spawne le serveur MCP dans un sandbox `bubblewrap` (`bwrap`) avec namespace réseau dédié + filesystem read-only + `/tmp` éphémère + timeout strict. Fallback `setrlimit` si `bwrap` indisponible. Couvre **FR24** (exécution sandboxée) et **NFR10** (sandbox outils MCP).
>
> **Cible architecturale** : `infra/mcp/sandbox.py` nouveau (architecture L1571), wrapper bwrap + setrlimit fallback ; extension `infra/mcp/client.py` avec `call_tool(transport, connection_config, tool_name, arguments, timeout=...)` qui s'exécute via le sandbox ; tests de bypass en CI (fork bomb + network outbound non-whitelisté + filesystem escape) qui bloquent le build s'ils passent.
>
> **Pourquoi maintenant ?** Story 2.5 livre la plomberie M5 (registry serveurs + assignment outils) mais EXÉCUTION runtime des outils était strictement défer (D59 dans defer log Story 2.5). Stories 2.7 (Playground) et 4.x (workflow_engine) consomment toutes la primitive `call_tool` sandboxée. Sans cette story, un agent ne peut PAS appeler un outil ; sans le sandbox, un outil compromis peut RCE le backend (D-01 fix Story 2.5 a fermé temporairement la POST /tools/servers via admin-gate — Story 2.6 permet de réouvrir car la sandbox referme la surface).
>
> **Anti-scope strict** : (a) PAS d'allowlist runtime "tool not in agent's assigned tools" (Story 4.x D60 — workflow_engine), (b) PAS de pool MCP persistant (D56 Story 2.5 — éphémère uniquement Sprint 1, persistant Sprint 2+), (c) PAS de Playground UI (Story 2.7), (d) PAS d'intégration workflow_engine (Story 4.x), (e) PAS de Fernet sur connection_config (Story 9.2 D55), (f) PAS d'audit-trail migration vers AuditEventRepo (Story 9.1 — bypass pattern conservé), (g) PAS d'EDIT/DELETE serveur MCP (Story 2.5 anti-scope conservé D52/D53), (h) PAS de health-check ping (Story 7.x D58).

## Story

As the system,
I want exécuter les outils MCP dans un sandbox `bubblewrap` avec whitelist réseau + mount filesystem read-only + timeout configurable + fallback `setrlimit`,
So that les outils ne peuvent pas compromettre l'hôte ou exfiltrer des données (NFR10, FR24).

## Acceptance Criteria

**AC1 — Sandbox bwrap nominal (stdio happy path)**

**Given** l'image backend contient `bubblewrap` (`bwrap`) — déjà installé Story 1.1 / Epic 1 retro 2026-05-08 décision 2
**And** un serveur MCP enregistré (Story 2.5) avec un outil `echo`
**When** `infra.mcp.sandbox.call_tool(server, tool_name="echo", arguments={"text":"hi"}, timeout=30.0)` est appelé
**Then** le subprocess MCP démarre dans un namespace réseau dédié (`--unshare-net` ou `--share-net` selon profil)
**And** le filesystem hôte est monté read-only (`--ro-bind /usr /usr`, `--ro-bind /etc /etc`, etc.) sauf `/tmp` éphémère (`--tmpfs /tmp`)
**And** seuls les binaires whitelistés sont accessibles (whitelist explicite `--ro-bind /usr/bin/python /usr/bin/python` par profil)
**And** la réponse de l'outil est récupérée + retournée à l'appelant en `dict[str, Any]` (shape MCP `CallToolResult`)
**And** le timeout de 30s est appliqué via `asyncio.wait_for` (kill si dépassé → `MCPExecutionTimeoutError` translaté en `DependencyError` 503 par les couches features/)

**AC2 — Timeout strict + cleanup subprocess**

**Given** un outil MCP qui boucle infiniment (fork bomb, `while True`, `time.sleep(300)`)
**When** `call_tool(..., timeout=1.0)` est appelé
**Then** le subprocess est terminé (SIGKILL après grace SIGTERM ≤ 500ms) au plus tard à `timeout + 1.0s`
**And** AUCUN process zombie / file descriptor leak n'est constaté post-call (vérifiable via `/proc/self/fd` count avant/après dans un test boucle 50 itérations)
**And** une `MCPExecutionTimeoutError` est levée avec `timeout_seconds` en context (translatée en `DependencyError` 503 par les features/ — D-02 Story 2.5 fermé)

**AC3 — Fallback setrlimit si bwrap indisponible**

**Given** `bwrap` n'est PAS dans le `$PATH` (ex : dev local sans `bubblewrap` installé)
**When** le backend boot (lifespan startup)
**Then** un warning structlog `mcp_sandbox.bwrap_unavailable_falling_back_to_setrlimit` est loggé avec niveau WARNING
**And** une métrique Dashboard `mcp_sandbox_backend{kind="setrlimit"}` est exposée (compteur d'appels par backend type)
**And** `call_tool` utilise le fallback `resource.setrlimit` (CPU, mémoire, fichiers) + timeout subprocess + whitelist réseau outbound via `socket` monkey-patch (best-effort uniquement)
**And** la réponse de l'outil est identique au cas bwrap (contract-preserving fallback)

**AC4 — Tests de bypass en CI (bloquants)**

**Given** un test de bypass dans `tests/integration/mcp/test_sandbox_bypass.py`
**When** la CI exécute `make test`
**Then** **fork-bomb test** : un script Python qui fork() en boucle est tué par cgroups bwrap (process count cap) OU rlimit RLIMIT_NPROC en fallback — le test échoue si le subprocess parent observe plus de N processes (cap configurable, par défaut 16)
**And** **network outbound test** : un script Python qui tente `socket.connect(("8.8.8.8", 53))` se voit refuser par le namespace réseau bwrap — le test échoue si la connexion réussit
**And** **filesystem write test** : un script Python qui tente `open("/etc/passwd", "w")` se voit refuser (read-only bind) — le test échoue si l'écriture réussit
**And** chaque test est tagué `@pytest.mark.security` ET `@pytest.mark.skip_if_no_bwrap` (skip explicite si bwrap absent du runner CI — devient un test bloquant en CI Linux où bwrap est garanti)

**AC5 — Atomicité audit-event `m5.tool.invoked` (single-tx avec result)**

**Given** un `call_tool(...)` qui termine avec succès
**When** la session MCP retourne un `CallToolResult`
**Then** un audit event `m5.tool.invoked` est publié via le pattern bypass `event_bus.publish_and_commit('m5.tool.invoked', ...)` avec payload `{tool_id, server_id, agent_template_id?, args_redacted, duration_ms, status='success', sandbox_backend ∈ {bwrap, setrlimit}}` (PAS de `result` — peut contenir des secrets)
**And** TODO Story 9.1 cleanup ajouté sur 1 ligne (cohérent décision Story 2.5 P-15 placement) — `git grep "audit-event bypass cleanup"` retourne 8 hits post-Story 2.6 (7 baseline post-2.5 + 1 nouveau pour `m5.tool.invoked`)

**AC6 — Erreurs sandbox traduites en domain errors**

**Given** un appel `call_tool` qui échoue
**When** la cause est un timeout
**Then** `MCPExecutionTimeoutError(timeout=...)` est levée (translaté en `DependencyError` 503 par features/, P-08 Story 2.5 pattern)

**Given** un appel `call_tool` qui échoue
**When** la cause est un crash subprocess (SIGSEGV, SIGABRT, exit code ≠ 0)
**Then** `MCPExecutionError(returncode=..., stderr_tail="...")` est levée (translaté en `DependencyError` 503, stderr tronqué à 500 chars + secrets-safe P-03 redaction sur args)

**Given** un appel `call_tool` qui échoue
**When** la cause est `tool_name` inconnu (le serveur MCP renvoie `CallToolResult.isError=True`)
**Then** `MCPToolError(tool_name=..., detail=...)` est levée (translaté en `NotFoundError` 404)

**AC7 — Smoke runtime + capture exhaustive**

**Given** la stack Docker dev tourne avec `AGENTIVE_ALLOW_MCP_REGISTRATION=true` (P-23 Story 2.5 gate)
**When** John exécute le smoke prescrit (cf section "Smoke Runtime — séquence prescrite" en Dev Notes)
**Then** la capture inclut : (1) connect server stdio mock, (2) call tool `echo({"text":"smoke"})` retourne `{"text":"smoke"}`, (3) call tool `add({"a":2,"b":3})` retourne `5`, (4) call tool avec timeout=0.1s sur outil qui sleep 5s renvoie 503 timeout, (5) docker logs grep `m5.tool.invoked` retourne ≥ 3 events, (6) DB outbox compte 3 `m5.tool.invoked` (status=success ×2 + status=timeout ×1).

**AC8 — Tests + lint verts (baseline + nouveaux)**

**Given** la baseline post-Story 2.5 = **514 backend + 100 frontend**
**When** la Story 2.6 est implémentée
**Then** ≥ 25 nouveaux tests : ≥ 18 backend (≥ 5 sandbox unit + ≥ 4 sandbox security/bypass + ≥ 4 client.call_tool integration + ≥ 3 schemas + ≥ 2 lifespan health-check bwrap) + ≥ 7 frontend (utility hook tests seulement — pas de UI dans cette story)
**And** baseline 514 + 100 strictement préservée (0 régression)
**And** `make lint` (ruff + mypy + eslint + tsc) vert
**And** smoke runtime AC7 capturé en Completion Notes (decision Story 2.4 B-01 / Story 2.5 P-04 pattern)

---

## Pré-requis

> **Vérifier avant T0**

- Story 2.5 done ✅ (commit Cluster C `3e6f1a6` sur staging). Tool Hub MCP plomberie + admin-gate `AGENTIVE_ALLOW_MCP_REGISTRATION` opérationnels.
- `bubblewrap` installé dans `backend/Dockerfile` stage `base` ✅ (Epic 1 retro 2026-05-08 décision 2, dev+prod).
- `infra/mcp/client.py` existe avec `discover_tools(...)` ✅ — Story 2.6 étend avec `call_tool(...)`.
- Pas de table DB nouvelle requise (l'audit event `m5.tool.invoked` écrit dans `outbox_events` existant).
- Pas de dépendance pip nouvelle (utilise `resource` stdlib pour setrlimit + `subprocess.Popen` via le SDK MCP `mcp>=1.27.0` déjà en deps).
- Pattern audit-event bypass `event_bus.publish_and_commit('m5.tool.invoked', ...)` avec TODO Story 9.1 cleanup sur 1 ligne — convention Story 2.1 P-15 + Story 2.5 P-23.
- Pattern atomicité P-02 Story 2.1 (single-tx pour la row audit ; PAS de single-tx pour le call lui-même — l'exécution MCP est out-of-DB-tx par nature).
- Pattern `_make_app` factor étendu — la conftest m5 inclut déjà m5 router (Story 2.5 B-01 amend DRY).
- `_redact_connection_config` helper Story 2.5 P-03 réutilisé pour rediger `arguments` dans l'audit payload (secrets safety).

---

## Tasks / Subtasks

- [ ] **T0 — Pré-flight checks**
  - [ ] T0.1 `which bwrap` dans le container backend dev (`docker compose exec backend which bwrap`) → confirme présence
  - [ ] T0.2 Lire `infra/mcp/client.py` (Story 2.5) pour comprendre le pattern `stdio_client` + `ClientSession` + dispatch transport
  - [ ] T0.3 Lire `features/m5_tool_hub/service.py` (Story 2.5) pour comprendre le pattern audit-event bypass + P-03 redaction
  - [ ] T0.4 Vérifier baseline tests : `make test` → 514 backend + 100 frontend, 0 failure

- [ ] **T1 — `infra/mcp/sandbox.py` core (architecture L1571)**
  - [ ] T1.1 Créer `infra/mcp/sandbox.py` avec API publique :
    ```python
    @dataclass(frozen=True)
    class SandboxProfile:
        """Per-tool sandbox profile. Defaults to deny-all + minimal whitelist."""
        unshare_net: bool = True
        ro_binds: tuple[str, ...] = ("/usr", "/etc", "/lib", "/lib64", "/bin", "/sbin")
        tmpfs_paths: tuple[str, ...] = ("/tmp",)
        env_passthrough: tuple[str, ...] = ("PATH",)
        max_processes: int = 16
        cpu_seconds: int = 30
        memory_mb: int = 512

    class MCPExecutionTimeoutError(Exception):
        def __init__(self, *, timeout: float) -> None: ...

    class MCPExecutionError(Exception):
        def __init__(self, *, returncode: int, stderr_tail: str) -> None: ...

    class MCPToolError(Exception):
        def __init__(self, *, tool_name: str, detail: str) -> None: ...

    def detect_sandbox_backend() -> Literal["bwrap", "setrlimit"]:
        """Probe shutil.which('bwrap') ; log + return 'setrlimit' if missing."""

    @asynccontextmanager
    async def sandboxed_subprocess(
        command: str, args: list[str], env: dict[str, str] | None,
        *, profile: SandboxProfile = SandboxProfile()
    ) -> AsyncIterator[asyncio.subprocess.Process]:
        """Spawn a subprocess inside bwrap (or setrlimit fallback)."""
    ```
  - [ ] T1.2 Implémenter `_build_bwrap_argv(command, args, env, profile)` qui construit la liste d'args bwrap : `["bwrap", "--unshare-net", "--ro-bind", "/usr", "/usr", ...]` puis `command, *args`
  - [ ] T1.3 Implémenter `_setrlimit_preexec(profile)` (`def preexec(): resource.setrlimit(...)`) appliqué via `subprocess.Popen(preexec_fn=...)` ou `asyncio.subprocess` via wrapper Python — fallback mode
  - [ ] T1.4 SIGTERM grace 500ms + SIGKILL après timeout (cohérent pattern P-10 Story 2.5)
  - [ ] T1.5 Cleanup obligatoire : `try/finally` autour du `process.wait()` qui kill le process si encore vivant

- [ ] **T2 — Extension `infra/mcp/client.py` avec `call_tool`**
  - [ ] T2.1 Ajouter `async def call_tool(transport, connection_config, tool_name, arguments, timeout=30.0, profile=None) -> dict[str, Any]`
  - [ ] T2.2 La connexion MCP s'établit via `stdio_client(StdioServerParameters(...))` MAIS le subprocess est wrappé dans `sandboxed_subprocess(...)` (override du `command` → `bwrap` + `--` + command original). Pour SSE : la sandbox réseau ne peut PAS s'appliquer (le client SSE EST l'instance qui parle HTTP) → SSE bypasse la sandbox réseau bwrap mais applique quand même le `setrlimit`-fallback côté client process. Documenter explicitement dans la docstring + AC1 stdio-only happy path.
  - [ ] T2.3 Appel `session.call_tool(tool_name, arguments)` + récupère `CallToolResult`
  - [ ] T2.4 Si `result.isError` → `MCPToolError(tool_name=..., detail=result.content[0].text or "tool error")`
  - [ ] T2.5 Sinon return `dict(result.content[0].model_dump())` (ou shape équivalente) — décision exécution à clarifier dans T6.1 (mapping CallToolResult → dict)
  - [ ] T2.6 Tests `tests/integration/mcp/test_client.py` : ≥ 4 tests (happy stdio + timeout via wait_for réel + isError → MCPToolError + sandbox network deny via bypass test)

- [ ] **T3 — Lifespan health-check bwrap au boot**
  - [ ] T3.1 Dans `app/main.py` lifespan, appeler `detect_sandbox_backend()` au startup
  - [ ] T3.2 Log structlog INFO avec `event="mcp_sandbox.backend_selected", backend=...` si bwrap dispo ; WARNING avec `event="mcp_sandbox.bwrap_unavailable_falling_back_to_setrlimit"` si fallback
  - [ ] T3.3 Stocker la valeur dans `app.state.mcp_sandbox_backend` pour exposition `/health` (champ optionnel `sandbox_backend`)
  - [ ] T3.4 Tests `tests/integration/auth/test_lifespan_*.py` ou nouveau `tests/integration/mcp/test_lifespan_sandbox.py` : ≥ 2 tests (bwrap dispo → backend=bwrap ; bwrap monkey-patched absent → backend=setrlimit + warning loggé)

- [ ] **T4 — Service M5 `ToolHubService.invoke_tool` (orchestration audit)**
  - [ ] T4.1 Ajouter à `features/m5_tool_hub/service.py` :
    ```python
    async def invoke_tool(
        self, *, server_id: UUID, tool_id: UUID, arguments: dict[str, Any],
        agent_template_id: UUID | None = None,
        timeout: float = 30.0,
        tenant_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Sprint 1 — invoke runtime, single-call (no pool).

        Audit event m5.tool.invoked published in single-tx with the RESPONSE
        timing (success/timeout/error) so we never log an invocation that
        never happened OR forget one that did.
        """
    ```
  - [ ] T4.2 Charger le `Tool` + son `ToolServer` via repos (réutiliser `ToolHubService` repos existants)
  - [ ] T4.3 Appeler `call_tool(...)` ; mesurer `duration_ms`
  - [ ] T4.4 Construire `args_redacted = _redact_arguments(arguments)` (réutilise `_redact_connection_config` Story 2.5 P-03 OU nouveau helper similar)
  - [ ] T4.5 Publish event `m5.tool.invoked` avec `status ∈ {success, timeout, error}` + `duration_ms` + `args_redacted` + `sandbox_backend` (PAS de `result` — secret safety)
  - [ ] T4.6 TODO Story 9.1 cleanup sur 1 ligne (`git grep "audit-event bypass cleanup"` → 8 hits attendus post-Story 2.6)
  - [ ] T4.7 Tests `tests/unit/m5_tool_hub/test_service.py` : ≥ 4 nouveaux tests (happy invoke success / invoke timeout → MCPExecutionTimeoutError translated / invoke isError → MCPToolError / args redaction prouvé sur payload secret)

- [ ] **T5 — Nouveau event `ToolInvokedEvent` (m5.tool.invoked)**
  - [ ] T5.1 Étendre `shared/contracts/events/tool_events.py` avec :
    ```python
    @dataclass(frozen=True)
    class ToolInvokedEvent(DomainEvent):
        event_type = "m5.tool.invoked"
        tool_id: UUID
        server_id: UUID
        agent_template_id: UUID | None
        args_redacted: dict[str, Any]
        duration_ms: int
        status: Literal["success", "timeout", "error"]
        sandbox_backend: Literal["bwrap", "setrlimit"]
        actor: str = "system"  # D1 défer Story 9.1
        tenant_id: UUID | None = None
    ```
  - [ ] T5.2 Export dans `events/__init__.py` barrel
  - [ ] T5.3 Tests `tests/unit/shared/contracts/test_tool_events.py` : ≥ 3 tests (event_type constant + serialization + invalid status rejected via dataclass)

- [ ] **T6 — Schemas Pydantic (réponse API si exposée Story 2.7+)**
  - [ ] T6.1 Réflexion CallToolResult → dict mapping : décider Sprint 1 si on retourne `{"content": [...], "isError": False}` shape MCP brute OU un wrapper `InvokeToolResponse{result: dict, duration_ms: int}` plus rich. Recommandation : shape MCP brute (le caller workflow_engine Story 4.x décidera de wrapper). Documenter en Completion Notes.
  - [ ] T6.2 Tests `tests/unit/m5_tool_hub/test_schemas.py` : ≥ 3 tests si on ajoute un schema, sinon section "decision : pas de schema response Sprint 1, défer Story 4.x" en Completion Notes

- [ ] **T7 — Tests sandbox bypass (CI bloquants)**
  - [ ] T7.1 Créer `tests/integration/mcp/test_sandbox_bypass.py`
  - [ ] T7.2 Test fork-bomb : un script Python qui `os.fork()` en boucle → cap bwrap process OU `RLIMIT_NPROC` fallback bloquent au 16ème process. Asserter `subprocess.Popen("python -c 'import os; ...'") returns non-zero` AVANT que le système soit instable.
  - [ ] T7.3 Test network outbound deny : un script Python qui `socket.socket().connect(("1.1.1.1", 443))` → bwrap namespace réseau refuse, `OSError [Errno 101] Network is unreachable`. Si fallback setrlimit, `socket` n'est pas restreint → test marquage `@pytest.mark.skipif(not bwrap_available, reason="setrlimit fallback does not restrict network")`.
  - [ ] T7.4 Test filesystem write deny : un script Python qui `open("/etc/passwd", "w")` → bwrap ro-bind refuse, `PermissionError`. Idem skipif setrlimit (RLIMIT_FSIZE limite la taille, pas l'écriture sur paths spécifiques).
  - [ ] T7.5 Marquer tous les tests `@pytest.mark.security` (selection CI dédiée) ET `@pytest.mark.integration`. Doc dans `tests/integration/mcp/README.md` (optionnel) que ces tests REQUIÈRENT bwrap (CI Linux Ubuntu/Debian-based où bwrap est apt-installable).

- [ ] **T8 — Tests sandbox unit (no subprocess)**
  - [ ] T8.1 `tests/unit/mcp/test_sandbox.py` (créer dossier `tests/unit/mcp/`)
  - [ ] T8.2 Test `_build_bwrap_argv` shape (≥ 3 tests : profile default produit `["bwrap", "--unshare-net", ...]` / custom profile applique overrides / env passthrough whitelist filtre les keys non-listées)
  - [ ] T8.3 Test `detect_sandbox_backend` (≥ 2 tests : monkeypatch `shutil.which` → "/usr/bin/bwrap" returns "bwrap" / returns None returns "setrlimit" + warning)

- [ ] **T9 — Pas de Frontend (anti-scope strict)**
  - Cette story est 100% backend. Pas de nouveau type / hook / composant frontend. Story 2.7 (Playground) consommera l'API.
  - Note : si lifespan expose `sandbox_backend` dans `/health`, le frontend pourrait l'afficher dans le futur Dashboard Story 7.x — défer.

- [ ] **T10 — Tests d'intégration MCP réels (mock server)**
  - [ ] T10.1 Étendre `tests/fixtures/mcp_mock_server.py` Story 2.5 : ajouter handler `call_tool` pour `echo` (retourne `{"text": args["text"]}`) et `add` (retourne `{"sum": args["a"] + args["b"]}`)
  - [ ] T10.2 Test `tests/integration/mcp/test_client.py::test_call_tool_stdio_echo_happy` : seed un server via Story 2.5 → `call_tool("echo", {"text":"hi"})` → résultat `{"text":"hi"}` (≥ 1 test)
  - [ ] T10.3 Test `test_call_tool_timeout_kills_subprocess` : tool qui sleep 5s + timeout=0.5s → `MCPExecutionTimeoutError` + asserter qu'aucun zombie subprocess (compteur `/proc/self/fd` avant/après ; tolerance ±2) (≥ 1 test)
  - [ ] T10.4 Test `test_call_tool_unknown_tool_raises_mcp_tool_error` : appeler `call_tool("nonexistent", {})` → `MCPToolError` (≥ 1 test)
  - [ ] T10.5 Test `test_invoke_tool_e2e_publishes_audit_event` : via `ToolHubService.invoke_tool(...)`, vérifier outbox count(`m5.tool.invoked`) == 1 + payload contains `duration_ms` > 0 + `status=success` + `args_redacted` ≠ original args si secret-named keys (≥ 1 test)

- [ ] **T11 — Documentation + sprint-status**
  - [ ] T11.1 Mettre à jour `_bmad-output/implementation-artifacts/sprint-status.yaml` : `2-6-execution-outils-sandbox-bwrap: backlog → ready-for-dev` (auto par le bmad-create-story workflow)
  - [ ] T11.2 Ajouter ligne récap dans sprint-status.yaml chronologique
  - [ ] T11.3 (Post-dev) Completion Notes en spec md avec capture smoke AC7
  - [ ] T11.4 (Post-dev) 1 ligne récap dans sprint-status.yaml post-implementation

---

## Dev Notes

### Décisions techniques d'implémentation (à valider par dev-story agent)

1. **`bubblewrap` déjà installé** Story 1.1 / Epic 1 retro 2026-05-08 décision 2 — pas de `apt-get install` à faire. Vérifier `docker compose exec backend which bwrap` au T0.1.

2. **Sandbox profile par défaut deny-all** : `--unshare-net` (no network), `--ro-bind /usr /usr` (read-only host binaries), `--tmpfs /tmp` (ephemeral writable), `env_passthrough=("PATH",)` (whitelist explicite). Per-tool override possible Sprint 2+ via colonne `tools.sandbox_profile JSONB` (anti-scope D Sprint 2).

3. **stdio vs SSE sandbox asymétrie** : la sandbox bwrap s'applique au subprocess MCP stdio (le serveur tourne dans le namespace réseau bwrap). Pour SSE, le serveur tourne distant — la sandbox bwrap ne s'applique PAS à un serveur distant ; seule la PROCESS CLIENT (notre backend) peut être restreinte via setrlimit. Story 2.6 Sprint 1 = **stdio sandboxé strictement, SSE traffic OK sans sandbox réseau** (la défense pour SSE = whitelist URL Story 4.x D60).

4. **Timeout = couche multiple** : (a) bwrap `--die-with-parent` (clean kill si parent meurt), (b) `asyncio.wait_for` côté client (timeout strict), (c) SIGTERM puis SIGKILL grace 500ms.

5. **Audit event `m5.tool.invoked` AVANT le retour à l'appelant** : pattern Story 2.1 P-02 atomicité — la row outbox est committed dans la même transaction qui marque "fin d'invocation". Si crash backend entre le `call_tool` et le `publish_and_commit`, la perte audit est acceptable (l'invocation a déjà eu lieu, mais on perd le log — D-audit-loss défer Story 9.1).

6. **`args_redacted` strategy** : réutiliser `_redact_connection_config` Story 2.5 P-03 OU créer un helper similaire dans `shared/redaction.py` (refactor souhaitable). Sprint 1 = reproduire logic Story 2.5 inline dans m5/service.py (pas de refactor prématuré).

7. **Pas de pool MCP persistant** (D-56 Story 2.5) : chaque `call_tool` re-spawne le subprocess. Cost = ~50-200ms overhead par call. Acceptable Sprint 1, optimisable Sprint 2+ via pool persistant (D-56).

8. **Erreur taxonomy** :
   - `MCPDiscoveryTimeoutError` (existant Story 2.5) — timeout discovery 10s
   - `MCPExecutionTimeoutError` (nouveau) — timeout call_tool
   - `MCPExecutionError` (nouveau) — subprocess crash (SIGSEGV, exit != 0)
   - `MCPToolError` (nouveau) — outil renvoie isError=True
   
   Toutes infra-level (jamais leak vers features/ ; translaté par le service).

9. **`detect_sandbox_backend()` cache au boot** : appelé 1 fois au lifespan startup, résultat stocké dans `app.state.mcp_sandbox_backend`. PAS de re-detect par appel (overhead `shutil.which` négligeable mais évite tout shenanigan TOCTOU si `bwrap` est retiré pendant runtime).

10. **Tests bypass = bloquants en CI Linux** (AC4) : marker `@pytest.mark.security` runnable via `make test-security` ou inclus dans `make test`. Sur CI Ubuntu, bwrap installé via `apt-get install bubblewrap` dans le workflow GitHub Actions. Skip uniquement si `not shutil.which("bwrap")` (warning logged).

11. **`--die-with-parent` bwrap flag** OBLIGATOIRE : sans, le subprocess MCP survit si le backend crash → zombie.

12. **`--cap-drop ALL`** OBLIGATOIRE : la sandbox ne doit avoir AUCUNE capability Linux (pas de raw socket, pas de mount, etc.).

13. **Mock MCP server étendu** : ajouter handler `@server.call_tool()` pour `echo` + `add` dans `tests/fixtures/mcp_mock_server.py`. Cohérent avec Story 2.5 fixture.

14. **AGENTIVE_ALLOW_MCP_REGISTRATION flag** (Story 2.5 P-23) : peut rester `false` par défaut puisque l'admin-gate gardait POST /tools/servers, pas `call_tool`. Décision : Story 2.6 ne touche PAS ce flag. Si on veut un gate sur `call_tool` (ex : "registration enabled but execution disabled"), ce serait un nouveau flag → défer (D-67 nouveau si besoin).

15. **Frontend = ZÉRO changement** : la story est backend-only. Story 2.7 (Playground) consommera l'API. Reproduction du pattern Story 2.4 (backend-only) — la story file `## Frontend Changes` section sera vide ou marquée "N/A".

### Pattern audit-event bypass — placement TODO

Cohérent décision Story 2.5 P-15 : le TODO doit être sur 1 ligne **adjacente au `publish(...)`** call (pas sur la définition de l'event). Exemple :

```python
# TODO Story 9.1 — migrate to AuditEventRepo.record() — audit-event bypass cleanup (Epic 1 retro 2026-05-08).
event_id = await publish(ToolInvokedEvent.event_type, event, session=session)
```

`git grep "audit-event bypass cleanup"` doit retourner **8 hits** post-Story 2.6 (7 baseline post-Story 2.5 + 1 nouveau `m5.tool.invoked` dans `features/m5_tool_hub/service.py`).

### Smoke Runtime — séquence prescrite (AC7)

```bash
TOKEN="change_me"
BASE="http://localhost:8000/api/v1"
SUFFIX=$$

# Setup
docker compose up -d
docker compose exec backend uv run alembic upgrade head
# AGENTIVE_ALLOW_MCP_REGISTRATION=true déjà dans .env post-Story 2.5

# Step 1: create template
TEMPLATE=$(curl -sS -X POST "$BASE/agents/templates" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "{\"archetype\":\"producteur\",\"name\":\"smoke-26-prod-$SUFFIX\"}")
TEMPLATE_ID=$(echo "$TEMPLATE" | python3 -c 'import sys,json; print(json.load(sys.stdin)["template_id"])')

# Step 2: connect MCP server (Story 2.5)
SERVER=$(curl -sS -X POST "$BASE/tools/servers" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "{\"name\":\"smoke-26-mcp-$SUFFIX\",\"transport\":\"stdio\",\"connection_config\":{\"command\":\"python\",\"args\":[\"-m\",\"tests.fixtures.mcp_mock_server\"]}}")
SERVER_ID=$(echo "$SERVER" | python3 -c 'import sys,json; print(json.load(sys.stdin)["server_id"])')
TOOL_ECHO_ID=$(echo "$SERVER" | python3 -c 'import sys,json; tools=json.load(sys.stdin)["tools"]; print([t for t in tools if t["name"]=="echo"][0]["tool_id"])')
TOOL_ADD_ID=$(echo "$SERVER" | python3 -c 'import sys,json; tools=json.load(sys.stdin)["tools"]; print([t for t in tools if t["name"]=="add"][0]["tool_id"])')

# Step 3-5: invoke tools (the NEW endpoint Story 2.6 — to design in T2/T4)
# Option A: POST /api/v1/tools/servers/{server_id}/tools/{tool_id}/invoke (Sprint 1 simple)
# Option B: Internal-only, no HTTP endpoint Sprint 1 (Story 4.x exposera via workflow_engine)
# Décision exécution à clarifier en T6

# If Option A:
INVOKE_ECHO=$(curl -sS -X POST "$BASE/tools/servers/$SERVER_ID/tools/$TOOL_ECHO_ID/invoke" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"arguments":{"text":"smoke"}}')
echo "echo: $INVOKE_ECHO"  # expected: {"result":{"text":"smoke"}, "duration_ms": 100..500, "sandbox_backend":"bwrap"}

INVOKE_ADD=$(curl -sS -X POST "$BASE/tools/servers/$SERVER_ID/tools/$TOOL_ADD_ID/invoke" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"arguments":{"a":2,"b":3}}')
echo "add: $INVOKE_ADD"

INVOKE_TIMEOUT=$(curl -sS -o /dev/null -w "%{http_code}" -X POST "$BASE/tools/servers/$SERVER_ID/tools/$TOOL_ECHO_ID/invoke" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"arguments":{"text":"sleep"}, "timeout_seconds": 0.001}')
echo "timeout HTTP code: $INVOKE_TIMEOUT"  # expected 503

# Step 6: audit log grep
docker compose logs backend --since 1m | grep -c "m5.tool.invoked"  # expected ≥ 3
docker compose exec db psql -U agentive_owner -d agentive -c "SELECT event_type, payload->>'status', count(*) FROM outbox_events WHERE event_type = 'm5.tool.invoked' GROUP BY event_type, payload->>'status';"
# expected: success x 2, timeout x 1
```

> **Décision exécution à trancher en T2/T4** : exposer un HTTP endpoint `POST /tools/servers/{server_id}/tools/{tool_id}/invoke` Sprint 1 OR garder `invoke_tool` interne uniquement (Story 4.x workflow_engine sera le seul caller). Recommandation : **exposer HTTP Sprint 1** pour permettre Story 2.7 Playground frontend de l'utiliser directement (UX-DR §"Playground inline"). Anti-scope reste : l'allowlist runtime "is tool in assigned tools of this agent" reste Story 4.x.

### Dépendances avec d'autres stories

- **Bloque** Story 2.7 (Playground) — le Playground a besoin de `invoke_tool` pour exécuter les outils sélectionnés.
- **Bloque** Story 4.x (workflow_engine) — le workflow_engine appelle `invoke_tool` lors des steps "tool_call".
- **Référence** Story 9.1 (Audit migration) — le TODO `audit-event bypass cleanup` ajouté ici sera fermé là.
- **Référence** Story 9.2 (Fernet) — `connection_config` reste clear Sprint 1 ; Story 9.2 chiffrera at-rest.

### Anti-scope strict (Story 2.6)

- ❌ **Allowlist runtime** "is tool_id in agent_template's assigned_tools ?" — défer Story 4.x (workflow_engine sait quel agent appelle quel tool).
- ❌ **Pool MCP persistant** — défer D56 Story 2.6+ / Sprint 2.
- ❌ **Playground UI** — défer Story 2.7.
- ❌ **Workflow engine integration** — défer Story 4.x.
- ❌ **Fernet encryption connection_config** — défer Story 9.2 (D55).
- ❌ **AuditEventRepo migration** — défer Story 9.1 (bypass pattern conservé).
- ❌ **DELETE/EDIT tool_server endpoints** — défer (D52/D53 Story 2.5).
- ❌ **Health-check ping serveur MCP** — défer Story 7.x (D58).
- ❌ **Multi-tenant** — défer Story 12.
- ❌ **Versioning tools** — défer Sprint 4+ (D57 Story 2.5).
- ❌ **Per-tool sandbox profile customization** (colonne `tools.sandbox_profile`) — défer Sprint 2.
- ❌ **SSE sandbox réseau** — impossible (le serveur SSE est distant). Défense URL allowlist défer Story 4.x.

### Nouveaux defer attendus Story 2.6 (D61+)

- **D61** — Pool MCP persistant (réutiliser connexion entre `call_tool` calls) → Sprint 2.
- **D62** — Per-tool sandbox profile override via `tools.sandbox_profile JSONB` → Sprint 2.
- **D63** — SSE URL allowlist enforcement runtime → Story 4.x.
- **D64** — `invoke_tool` budget/rate limiting per agent → Story 9.4/9.5.
- **D65** — Metric `mcp_sandbox_backend{kind=...}` Prometheus integration → Story 7.x Dashboard.
- **D66** — Result caching pour outils déterministes (LRU sur `(tool_id, args_hash)`) → Sprint 4+.
- **D67** — Flag `AGENTIVE_ALLOW_MCP_EXECUTION` séparé de registration (registration enabled but execution disabled) → si besoin opérationnel.
- **D68** — Audit-loss recovery si crash entre `call_tool` et `publish` (audit gap window) → Story 9.1.
- **D69** — Sandbox profile per archetype (controleur vs producteur peuvent avoir des profils différents) → Story 4.x.
- **D70** — Stdout/stderr capture des tool calls pour debug → Story 8.x Trace Explorer.

---

## File List (post-implementation, target)

**Backend NEW**
- `backend/src/agentive_backend/infra/mcp/sandbox.py` (T1) — bwrap wrapper + setrlimit fallback + SandboxProfile + erreur classes
- `backend/src/agentive_backend/shared/contracts/events/tool_events.py` (T5) — extension `ToolInvokedEvent`
- `backend/tests/unit/mcp/__init__.py` + `test_sandbox.py` (T8)
- `backend/tests/integration/mcp/test_sandbox_bypass.py` (T7) — tests bloquants CI

**Backend MODIFIED**
- `backend/src/agentive_backend/infra/mcp/client.py` (T2) — `call_tool(...)` ajouté
- `backend/src/agentive_backend/features/m5_tool_hub/service.py` (T4) — `ToolHubService.invoke_tool` ajouté + audit event bypass `m5.tool.invoked` (8e TODO `audit-event bypass cleanup`)
- `backend/src/agentive_backend/features/m5_tool_hub/router.py` (T6) — si décision HTTP endpoint Sprint 1 : `POST /tools/servers/{server_id}/tools/{tool_id}/invoke` ajouté
- `backend/src/agentive_backend/features/m5_tool_hub/schemas.py` (T6) — `InvokeToolRequest` / `InvokeToolResponse` si endpoint exposé
- `backend/src/agentive_backend/app/main.py` (T3) — lifespan détecte `sandbox_backend` au startup
- `backend/src/agentive_backend/shared/contracts/events/__init__.py` (T5) — barrel export `ToolInvokedEvent`
- `backend/tests/fixtures/mcp_mock_server.py` (T10.1) — handlers `call_tool` pour `echo` + `add`
- `backend/tests/integration/mcp/test_client.py` (T10) — ≥ 4 nouveaux tests
- `backend/tests/unit/m5_tool_hub/test_service.py` (T4.7) — ≥ 4 nouveaux tests
- `backend/tests/unit/shared/contracts/test_tool_events.py` (T5.3) — ≥ 3 nouveaux tests
- `backend/tests/integration/m5_tool_hub/test_invoke_tool_e2e.py` (T10.5, NEW si HTTP endpoint exposé)

**Frontend** : aucun changement (anti-scope strict — Story 2.7 consommera).

**Story spec**
- `_bmad-output/implementation-artifacts/2-6-execution-outils-sandbox-bwrap.md` (cette story, Status: ready-for-dev)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — bump 2-6 backlog → ready-for-dev

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
| 2026-05-11 | bmad-create-story (Claude Opus 4.7 1M ctx) | Initial story creation — context engine pass complet : Epic 2 + Story 2.6 AC + AR23 sandbox + NFR10 + Story 2.5 patterns (audit-event bypass, P-02 atomicité, P-03 redaction, P-23 admin-gate) + Dockerfile bwrap check (déjà installé Epic 1 retro). 17 défer dont 10 nouveaux D61-D70. Ready for dev-story implementation. |
