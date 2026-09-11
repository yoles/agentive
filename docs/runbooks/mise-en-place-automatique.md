# Mise en Place Automatique Runbook (Story 4.5)

Every `POST /api/v1/workflows/{workflow_id}/runs` call now runs a
pre-workflow "Mise en Place" hook — four checks, in parallel — BEFORE the
`workflow_runs` row is created. If any check fails, the run does not start
(503) unless the caller explicitly bypasses it (`force`). This runbook
covers where the logic lives, how to tune it, how to read the report, and
how to bypass it in an emergency.

## Where the logic lives

- **Pure domain** (report assembly, no I/O):
  `backend/src/agentive_backend/features/workflow_engine/domain/mise_en_place.py`
  — `CheckResult`, `MiseEnPlaceReport`, `build_report`.
- **Orchestration** (the four checks themselves — MCP discovery, namespace
  lookup, Dry Run reuse, API-key presence):
  `backend/src/agentive_backend/features/workflow_engine/mise_en_place.py`
  — `MiseEnPlaceService`.
- **Hook point**: `WorkflowExecutionService.start_run`
  (`features/workflow_engine/service.py`) — between the Contrôleur/Producteur
  diversity check and the `workflow_runs` row INSERT. Nothing is persisted
  before the hook runs, which is what makes AC2's "the workflow does not
  start" true structurally rather than by convention.
- **Settings** live in `Settings` (`shared/config.py`) — see `.env.example`
  for `AGENTIVE_MISE_EN_PLACE_TOOL_PING_TIMEOUT_S`. The budget check reuses
  `AGENTIVE_DRY_RUN_BUDGET_CAP_USD` (Story 4.4) as-is — there is no second
  budget setting.

## The four checks

| code | what it verifies | never blocks on |
|---|---|---|
| `mcp_tools_reachable` | Every MCP server backing a tool assigned to a referenced template responds to `discover_tools` within `AGENTIVE_MISE_EN_PLACE_TOOL_PING_TIMEOUT_S`. | A template with no tools assigned. |
| `mcp_tools_reachable` *(complément)* | Chaque serveur MCP joignable doit **aussi** exposer encore les outils assignés aux templates (comparaison des noms retournés par `discover_tools`). Un serveur qui répond mais a perdu l'outil dont dépend un template échoue le check. | Étend AC1, qui n'énumérait que les échecs de connexion (revue BS3). À noter : les runs de workflow n'appellent pas encore d'outils (anti-scope `engine/agent_node`), ce check est donc anticipatif des deux côtés. |
| `memory_namespaces_accessible` | Every non-null `push_memory.namespace` a template will actually open exists (`NamespaceRepo.require_by_name`). A **Contrôleur without `optin: true`** is skipped: Story 3.5 AC2 means its namespace is never opened at runtime, so blocking on it would be a false positive. For every other archetype `optin` is ignored and a configured namespace is live. | Existence only — NOT department access control (`MemoryManagerService._check_department_access`), same class of debt as `tenant_id=None` elsewhere in Epic 4. |
| `budget_available` | Reuses `DryRunService.dry_run()` (Story 4.4, zero real LLM call) for `cost_estimate_usd`, compared against `AGENTIVE_DRY_RUN_BUDGET_CAP_USD`. | No cap configured, or no price resolved for the estimate — this check only blocks on a PROVEN overrun, never an uncertainty. |
| `llm_providers_configured` | Every LLM provider resolved from the templates' `llm_model` has a configured API key (`Settings.anthropic_api_key`/`openai_api_key`). | A model absent from every pricing table (nothing to check a key for). Checks PRESENCE of a key, not a real network health-check — no such mechanism exists anywhere in this repo, and building one would either cost a real LLM call or need infrastructure absent elsewhere. |

All four always run, and the report always carries all four — never a
partial report, whatever the outcome.

## Reading the response / error

**Success (201)** — `mise_en_place` is always present, even when every
check passed:

```json
{
  "run_id": "...",
  "status": "running",
  "warnings": [],
  "mise_en_place": {
    "checks": [
      {"code": "mcp_tools_reachable", "passed": true, "detail": "...", "suggested_action": null},
      {"code": "memory_namespaces_accessible", "passed": true, "detail": "...", "suggested_action": null},
      {"code": "budget_available", "passed": true, "detail": "...", "suggested_action": null},
      {"code": "llm_providers_configured", "passed": true, "detail": "...", "suggested_action": null}
    ],
    "all_passed": true,
    "bypassed": false,
    "bypass_reason": null
  }
}
```

**Blocked (AC2)** — no `workflow_runs` row is created and no
`workflow_run.started` event is published. The refusal IS traced, as a
`workflow_engine.workflow_run.mise_en_place_refused` event in
`outbox_events` carrying `workflow_id`, `failed_checks`, `retryable` and the
full report (review BS2). It has **no `run_id`** — there is no run, and the
payload says so rather than fabricating one. Query it by `workflow_id`:

```sql
SELECT payload FROM outbox_events
WHERE event_type = 'workflow_engine.workflow_run.mise_en_place_refused'
  AND payload->>'workflow_id' = :workflow_id;
```

This exists because AC1 ("un rapport est persisté, que le workflow démarre
ou non") and AC2 ("AUCUNE row n'est créée") contradict each other: the
column lives on `workflow_runs`, so a refused launch has nowhere to persist
its report. AC2 is the stronger requirement — inventing a `refused` run row
would leak a phantom run into run listings, the recovery worker's stale
sweep and routing stats — so the outbox carries the trace instead, next to
the `mise_en_place_bypassed` event this story already publishes.

The audit write is **best-effort**: if it fails, the refusal is still
returned (the caller already has the full report in the error body, and
replacing a precise "your MCP server is down" with an opaque database error
would be a downgrade). The failure is logged at ERROR as
`workflow_engine.mise_en_place_refusal_audit_failed`.

The RFC 7807 body's `detail` carries a human-readable summary of
every failing check (`app.main`'s handler drops any `context` key that
collides with a reserved field, `detail` included — the summary lives on
the exception's own `detail`, not nested).

**Which status code?** Each `CheckResult` carries `retryable` (review BS5):

| Failure | `retryable` | Why |
|---|---|---|
| MCP server unreachable / timed out | `true` | May come back with no human action |
| Check could not be evaluated (timeout, DB error) | `true` | Same |
| API key missing, namespace absent, budget exceeded, tool no longer exposed | `false` | Still broken on the next attempt |

The refusal is **503 only when EVERY failing check is `retryable`**, and
**422 (`/errors/business-rule`)** as soon as one is not. 503 tells clients,
proxies and gateways to retry — emitting it for a permanently misconfigured
workflow turned a launch that could never succeed into an infinite automatic
retry loop. 422 is also what `status != "active"` already returns, for the
same reason: nothing will change until a human changes it.

The example below is the retryable case:

```json
{
  "type": "/errors/dependency",
  "title": "Downstream dependency unavailable",
  "status": 503,
  "detail": "mcp_tools_reachable: MCP server(s) unreachable: my-server",
  "correlation_id": "...",
  "suggested_actions": ["Vérifier que le serveur MCP my-server est démarré et joignable"],
  "failed_checks": ["mcp_tools_reachable"],
  "mise_en_place": { "...": "the full report, checks and all_passed included" }
}
```

`suggested_actions` is plural because it is a list — the per-check field
inside `mise_en_place.checks[]` is the singular `suggested_action`, a string.
The key is OMITTED entirely when no failing check carries an action, rather
than rendered as an empty array that promises a remediation and gives none.

### Ce que `mcp_tools_reachable` exécute réellement

Le check appelle `discover_tools` sur chaque serveur MCP distinct assigné —
et pour un transport `stdio`, cela **lance la commande stockée dans
`tool_servers.connection_config`**. Deux conséquences à connaître :

- **Le sandbox s'applique** (revue IG2). La Story 2.6 n'enveloppait que
  `call_tool` ; `discover_tools` spawnait la commande sans confinement. Les
  deux passent désormais par bwrap (ou le bootstrap setrlimit en repli), avec
  le même filtrage de `env` via `profile.env_passthrough`. Un serveur
  incapable de répondre à `list_tools` sous le sandbox n'aurait de toute
  façon jamais pu servir un `call_tool`.
- **Le déclencheur a changé de population.** L'enregistrement d'un serveur est
  gaté par `AGENTIVE_ALLOW_MCP_REGISTRATION` (403 par défaut) ; `POST
  /workflows/{id}/runs` ne l'est pas. Depuis cette story, tout appelant
  autorisé à lancer un workflow provoque l'exécution des commandes déjà
  enregistrées — au rythme d'une par lancement, plafonnée à 8 probes
  simultanées et bornée par `AGENTIVE_MISE_EN_PLACE_TOOL_PING_TIMEOUT_S`.
  Les commandes restent celles qu'un administrateur a validées à
  l'enregistrement ; ce n'est donc pas une nouvelle surface d'injection, mais
  bien une nouvelle fréquence et une nouvelle population de déclencheurs.

### Running without provider keys (dev / CI)

When NO provider key is configured, `app.lifespan` deliberately wires a
`MockProvider` so the app boots without secrets (production refuses to boot
this way). `llm_providers_configured` recognises that mode and **passes** —
a missing key predicts no failure when every node completes against the
mock, and blocking would force `force=true` on every single local launch.
Its `detail` says so explicitly, so a pass is never mistaken for a verified
provider.

This applies only when *no* key is configured at all. A **partial**
configuration (say Anthropic but not OpenAI) builds the real providers, so a
template targeting the unconfigured one still fails the check.

A check that could not be *evaluated* (timeout, database error, an
unexpected exception anywhere in the check) is reported as `passed: false`
with an explanatory `detail`, never silently passed — the hook fails closed
and `force=true` stays the deliberate way through.

## Bypassing a failing check (AC3)

```bash
curl -X POST .../workflows/{workflow_id}/runs \
  -H "Authorization: Bearer $AGENTIVE_API_TOKEN" \
  -d '{"input": {}, "force": true, "reason": "incident P1, MCP server restart in progress"}'
```

- `reason` is REQUIRED whenever `force=true` — validated both by
  `StartRunRequest`'s Pydantic model (422 at the HTTP boundary) and again
  inside `WorkflowExecutionService.start_run` itself (422, defensive —
  the service is a real surface a caller can reach directly, e.g. in tests
  or a future internal caller). This holds UNCONDITIONALLY, whether or not
  any check is actually failing. The service additionally enforces the
  2000-character cap that `WorkflowRunMiseEnPlaceBypassedEvent.reason`
  imposes, so an over-long reason is a clean 422 rather than a Pydantic
  error raised mid-transaction.
- The converse is refused too: a `reason` WITHOUT `force=true` is a 422.
  It used to be accepted and then silently discarded — never persisted,
  never published, never echoed back — so an operator who mistyped the
  bypass believed they had filed a justification when they had not.
- `reason` is passed through `redact_secrets` before being persisted and
  published: it is free text that reaches both `workflow_runs.mise_en_place`
  and the outbox payload.
- If every check happens to pass anyway, `force=true` is a silent no-op —
  the run starts normally, `mise_en_place.bypassed` stays `false`, and NO
  `mise_en_place_bypassed` event is published (nothing was actually
  bypassed, so the event would lie).
- On an actual bypass, `workflow_engine.workflow_run.mise_en_place_bypassed`
  is published in the SAME transaction as the row INSERT + `started` event
  — query `outbox_events` for it, or watch the SSE stream. The persisted
  `workflow_runs.mise_en_place` JSONB carries `bypassed: true` and
  `bypass_reason: "<reason>"` even after the fact, so it stays the source of
  truth for Trace Explorer (NFR15).

## Tuning

```bash
# .env or environment
AGENTIVE_MISE_EN_PLACE_TOOL_PING_TIMEOUT_S=3.0   # per-MCP-server ping timeout
# Budget threshold is the SAME setting as Story 4.4 — no second knob:
AGENTIVE_DRY_RUN_BUDGET_CAP_USD=50.0
```

`AGENTIVE_MISE_EN_PLACE_TOOL_PING_TIMEOUT_S` is deliberately shorter than
`infra/mcp/client.py`'s registration-time discovery timeout (10s) — this
check can ping several servers in parallel on the hot path of every
workflow launch, and one bad server must not freeze it for 10s+.

## Known gaps (Dev Notes § Divergences assumées)

- `llm_providers_configured` checks key PRESENCE, not a real provider ping.
- `memory_namespaces_accessible` checks EXISTENCE, not department-level
  access control.
- `budget_available` compares against ONE global cap, not Story 9.4's real
  per-department/per-workflow budget tracking (backlog).
- No UI surfaces the report yet — Epic 6 (Chat Interface), same as every
  Epic 4 story to date. This endpoint's response already carries everything
  a future Dialog would need.
