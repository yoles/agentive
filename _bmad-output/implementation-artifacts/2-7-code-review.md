# Story 2.7 — Code Review (Adversarial 3-Layer)

**Story:** 2.7 — Agent Playground test isolation (FR48)
**Commit reviewed:** `495edc8` (single commit, branch `staging`)
**Diff:** 29 files / +2171 / -8 LOC (saved to `.review-2.7.diff`)
**Spec:** `_bmad-output/implementation-artifacts/2-7-agent-playground.md`
**Review date:** 2026-05-19
**Review mode:** `full` (spec + project context loaded)
**Reviewers (parallel, no cross-context):**
- **Blind Hunter** — diff-only, adversarial — 40 findings
- **Edge Case Hunter** — diff + project read access — 24 findings
- **Acceptance Auditor** — diff + spec + context docs — 19 findings (6 AC + 5 deviations + 8 bad-spec)

**Raw findings:** 83 → **after dedup:** 56 unique → **classified below.**

---

## ⚠️ HARD BLOCKER — DO NOT BUMP TO `done`

> A first-pass static check (`python3 -m py_compile`) **fails on `service.py`**. The module cannot be imported. The dev-notes claim "580 backend tests pass, ruff+mypy verts" is **factually impossible** with this commit. Either tests were never run or the run was scoped to skip `m7_playground`. **Do not bump Story 2.7 to `done` without re-running the full suite from a clean state.**

---

## 🔴 Critical (1) — Must fix before any merge

### P-01 — Python 3 SyntaxError in `service.py:210` (sources: Blind F-01, Edge E-01, Auditor A-01)
- **Location:** `backend/src/agentive_backend/features/m7_playground/service.py:210`
- **Code:** `except json.JSONDecodeError, TypeError, ValueError:`
- **Fix:** wrap the exception tuple in parentheses → `except (json.JSONDecodeError, TypeError, ValueError):`
- **Impact:**
  - `python3 -m py_compile src/agentive_backend/features/m7_playground/service.py` → `SyntaxError`
  - Imported by: `router.py:24`, `__init__.py:19`, `app/main.py:212`, `tests/integration/m2_agent_registry/conftest.py:33`, `tests/unit/m7_playground/test_service.py:17`
  - **Every backend test suite that boots the FastAPI app or imports m2 conftest must currently be failing.** The dev's `make test` claim "580 backend tests pass, 0 régression" is empirically false on the working tree.
- **Severity:** Critical (release-blocking)

---

## 🟠 High (5) — Fix in Cluster A

### P-02 — System prompt injection + secret exfiltration (sources: Blind F-09/F-10, Edge E-02)
- **Location:** `service.py:531-533` + response `prompt_resolved` field (`schemas.py:380`)
- **Trigger:** `system_prompt_template.format_map(arguments)` with arbitrary user-controlled `arguments` and a template author who has put a server-side variable into the template.
- **Risk:** Two attack surfaces:
  1. Format-spec injection: `arguments` containing `{0}`, `{x.attr}`, `{x:!r}`, or `{` would raise `IndexError`/`ValueError`/`AttributeError`, none of which are caught → 500 instead of 422.
  2. Information disclosure: `prompt_resolved` is returned to the caller. If a template ever references a server-side secret (env-injected variable, hidden context), it leaks via the response.
- **Fix:**
  - Catch `(KeyError, IndexError, ValueError, AttributeError)` → `ValidationError(422)`.
  - Decide whether `prompt_resolved` should be returned at all when the template contains server-only variables. If yes — explicitly document the threat model. If no — gate the field behind a flag or strip it.
- **Severity:** High (security + reliability)

### P-03 — AC2 OTel span attribute `playground.run=true` missing (source: Auditor A-03, M-01)
- **Location:** spec T4.4 explicitly mandates `current_span.set_attribute("playground.run", True)`. Implementation only emits a structlog field `playground_run=True`. `grep -r "opentelemetry\|set_attribute" backend/src/agentive_backend/features/m7_playground/` returns 0 hits.
- **Fix:** add the OTel import + best-effort `trace.get_current_span().set_attribute("playground.run", True)`.
- **Severity:** High (AC2 unmet)

### P-04 — Frontend test count short by ≥ 6 (sources: Auditor A-04, M-02)
- **Spec AC8:** "≥ 10 frontend (≥ 4 PlaygroundPage + ≥ 3 OutputInspector + ≥ 3 hooks utilities)".
- **Actual:** only `PlaygroundPage.test.tsx` (4 tests). `AgentTesterForm.test.tsx` (T8.2) + `OutputInspector.test.tsx` (T8.3) + hooks tests — **missing entirely**. Dev notes claim "+4 frontend = 104 total" — confirming the gap.
- **Fix:** add the two missing test files + 3 hook-level tests (`useFn`, error mapping, retry semantics).
- **Severity:** High (AC8 unmet)

### P-05 — Module `m7_playground` not covered by import-linter Contract 1 — AC2 isolation enforcement is illusory (sources: Auditor D-05, B-05)
- **Spec decision #3:** "isolation vérifiable via `import-linter` Contract 3" (also wrong contract number — it's Contract 1).
- **Reality:** `.import-linter` Contract 1 (features-isolated) lists `m7_chat` for the M7 slot. `m7_playground` is **absent**. The independence rule is not enforced; the linter passes today because the module is invisible to it.
- **Fix:** decide between:
  - (A) add `agentive_backend.features.m7_playground` to Contract 1 (and accept the M-slot collision documented as B-05/B-02), or
  - (B) rename the module to a non-M-slot name (`features/playground/`) or a free slot.
- **Severity:** High (AC2 contract enforcement missing)

### P-06 — Smoke runtime never executed a 200-success path (source: Auditor A-02)
- **AC7 step 3:** "POST /playground/.../run → 200 + body complet".
- **Completion notes evidence:** smoke ran against a dev stack without `ANTHROPIC_API_KEY` → returned 503 LLMProviderAuthError on every run. Audit events captured `status=llm_error` × 2. **No `status=success` was ever observed end-to-end.** AC7 demands proof of the 200 path.
- **Fix:** rerun smoke with a valid `ANTHROPIC_API_KEY` (or stub the LLM at the integration boundary) and capture the 200 response + matching audit event with `status=success` in Completion Notes.
- **Severity:** High (AC7 unmet)

---

## 🟡 Medium (12) — Fix in Cluster B

### P-07 — `cost_estimate_usd` type discipline (Decimal/float/string drift) (sources: Blind F-03/F-25, Edge E-19, Auditor D-01/B-01)
- **Symptom:** AC3 declares `float | None`; decision #9 declares "Decimal → float"; implementation serializes `Decimal | None → str(v)` (matches frontend types but contradicts AC3). Pydantic v2 silently coerces `float → Decimal` with binary-rounding artifacts.
- **Fix:** (a) make the service-internal type `Decimal | None` everywhere; (b) coerce providers' `float` inputs via `Decimal(str(x))`; (c) **amend AC3** to `cost_estimate_usd: str | None` (Decimal stringified) and **drop "→ float" from decision #9**. See bad-spec B-01.

### P-08 — Router `assert` for repo wiring stripped under `python -O` (sources: Blind F-06, Edge E-08)
- **Location:** `router.py:244-248` — `assert (template_repo._session_factory is tool_repo._session_factory is assignment_repo._session_factory)`.
- **Risk:** `PYTHONOPTIMIZE=1` (commonly set in Docker prod images) removes the check. Also accesses a private `_session_factory` attribute.
- **Fix:** replace with `if not (... is ... is ...): raise DependencyError("PlaygroundService wiring violation")`. Or expose `session_factory` as a public property on `BaseRepository`.

### P-09 — `json.dumps(arguments)` `TypeError` not caught → 500 (sources: Blind F-11, Edge E-03)
- **Location:** `service.py:553`. If `arguments` contains `Decimal`, `datetime`, `set`, etc., `json.dumps` raises `TypeError`.
- **Fix:** wrap in try/except → 422 ValidationError; or pass `default=str` and document loss.

### P-10 — `llm_cfg` numeric casts can raise `ValueError`/`TypeError` (source: Edge E-04)
- **Location:** `service.py:549-551`. `int(llm_cfg.get("max_tokens", 4096))` with stored `"unbounded"` → `ValueError` → 500.
- **Fix:** wrap each cast in try/except → 422 with field name in the error payload.

### P-11 — `asyncio.CancelledError` during LLM call → audit event lost (source: Edge E-05)
- **Location:** `service.py:566-602`. If the client disconnects mid-LLM, only `LLMError` is caught; `CancelledError` propagates up bypassing the audit publish.
- **Fix:** explicitly handle `CancelledError`: publish audit with `status=llm_error` (or new `status=cancelled`) then `raise`. Add test.

### P-12 — `system_prompt` is `None` → `str(None)` = literal `"None"` sent to LLM (source: Edge E-06)
- **Location:** `service.py:531`. `config_snapshot.get("system_prompt")` can return `None` (not just missing key).
- **Fix:** `system_prompt_template = str(config_snapshot.get("system_prompt") or "")`.

### P-13 — Hardcoded `claude-sonnet-4-6` model default (source: Blind F-16)
- **Location:** `service.py:549`. If a template has no `config.llm.model`, Playground hardcodes Anthropic regardless of host config.
- **Fix:** read default from `settings.default_llm_model` (or equivalent). If none, 422.

### P-14 — Frontend `OutputInspector` reads `error.message` but `ApiError` carries `.detail`/`.title` (sources: Edge E-11/E-12)
- **Location:** `OutputInspector.tsx:1878`. On any 4xx/5xx, the error panel renders blank.
- **Fix:** read `(error as ApiError).detail ?? (error as ApiError).title ?? error.message`.

### P-15 — AC2 isolation test is structural only (constructor signature check) — too weak (source: Blind F-38)
- **Location:** `test_service.py:1439-1454`. Verifies absence of `instance_repo`/`memory_chunk_repo`/`event_bus` from `__init__` kwargs.
- **Fix:** add an integration test that runs the service against a real DB seeded with a template + 1 assigned tool, executes `run()`, and asserts via `SELECT COUNT(*)`:
  - `agent_instances` rows for `template_id` = 0
  - `outbox_events WHERE event_type='m2.agent_instance.created'` = 0
  - `outbox_events WHERE event_type='m5.tool.invoked'` = 0
  - `outbox_events WHERE event_type='m7.playground.run_completed'` = 1

### P-16 — Audit publish uses `template_repo.with_tenant` session (T4.3 explicitly forbids) (source: Auditor D-04)
- **Location:** `service.py:_publish_audit_event` opens `async with self._template_repo.with_tenant(tenant_id) as session:`. Spec T4.3: "single-tx fresh session — **pas dans `template_repo.with_tenant`** car AC2 isolation".
- **Fix:** inject `session_factory` directly and open a fresh session.

### P-17 — AC4 "re-run sans recharger la page" not asserted by any test (source: Auditor A-05, M-03)
- **Spec AC4:** form state preserved, previous result replaced in OutputInspector.
- **Fix:** add a `PlaygroundPage.test.tsx` scenario: submit → assert result rendered → submit again → assert new result replaces old, form values unchanged.

### P-18 — AC6 "Variable manquante → 422" not asserted at integration level (source: Auditor A-06, M-04)
- **Current:** covered by `test_service.py` unit (KeyError → ValidationError). E2E docstring claims coverage but no e2e test exists.
- **Fix:** add `test_run_422_when_template_var_missing_from_arguments` to `test_playground_e2e.py`.

### P-19 — `max_tokens` no upper bound — cost denial-of-service (source: Blind F-17)
- **Location:** `service.py:550`. Template author sets `config.llm.max_tokens`; if `200000`, each Playground run = wallet drain.
- **Fix:** cap in `RunPlaygroundRequest` (or service-side clamp) at a sane Sprint 1 limit (e.g., 16k). Reference Story 9.4 FinOps for follow-up.

---

## 🟢 Low / Polish (20) — Fix in Cluster C

### Backend
- **P-20** — Audit `publish` + `emit_notify` ordering: confirm `with_tenant` commit happens-before NOTIFY, add explicit test (Blind F-05).
- **P-21** — `parsed_output` silently discards JSON arrays/scalars (returns `None`) → no signal to frontend that LLM produced *valid* non-dict JSON (Blind F-12, Edge E-21).
- **P-22** — `tokens: dict[str, int]` untyped — extract a typed `TokenUsage` sub-model (Blind F-13).
- **P-23** — `extra="ignore"` on `RunPlaygroundResponse`/`ToolInvocationLog` masks server-side typos; explicit constructor in `service.py` makes `extra="forbid"` safer (Blind F-14/F-33).
- **P-24** — `tool_repo` injected into `PlaygroundService` but never used — remove (Blind F-15, Auditor D-03).
- **P-25** — `temperature` not validated against provider-specific range (Blind F-18).
- **P-26** — `usePlaygroundAssignedTools` query has no `staleTime`/`gcTime` → over-fetches on every form mount (Blind F-21).
- **P-27** — `AgentTesterForm.toggleTool` reads `assignedTools` from a stale closure when the query hasn't resolved yet; intersect `enabledToolIds` with current `assignedTools` on data refresh (Blind F-22, Edge E-13/E-16).
- **P-28** — `cost_estimate_usd` `Decimal.__str__()` can emit scientific notation (`1E-7`) — apply explicit format in serializer (Blind F-25).
- **P-29** — `__init__.py` docstring claims "must not import `event_bus.publish_and_commit`" but the code imports `event_bus.publish` — align docstring with reality (Blind F-28).
- **P-30** — Add test for `raw_output == ""` (empty string) → `parsed_output=None` path (Blind F-31).
- **P-31** — `enabled_tool_ids` has no `max_length` constraint → unbounded list (Blind F-34).
- **P-32** — `arguments: dict[str, Any]` has no max-depth/size — bound at the schema or gateway level (Blind F-35, Edge E-09/E-22).
- **P-33** — `frontend/.../playground/index.ts` exports `usePlaygroundRun` but not `usePlaygroundAssignedTools` — barrel inconsistency (Blind F-37).
- **P-34** — `event_id` reference outside the `try` block needs `event_id: UUID | None = None` declaration (Blind F-39, Edge E-10).
- **P-35** — `sprint-status.yaml:122-124` `last_updated` comment is self-contradicting (concatenated old+new) — clean it up (Blind F-40).
- **P-36** — Add test for `_publish_audit_event` swallowed `Exception` branch (audit publish failure must not break the response) (Edge E-18).

### Frontend
- **P-37** — `AgentTesterForm` timeout input: `Number(e.target.value)` returns `NaN` on empty — clamp client-side (Edge E-14).
- **P-38** — Submit button: guard against rapid double-click before `isRunning` propagates (Edge E-15).
- **P-39** — Reject `NaN`/`Infinity` explicitly in `timeout_seconds` Pydantic validator (Edge E-20).
- **P-40** — Add a React `<ErrorBoundary>` around `PlaygroundPage` so render errors (e.g., circular ref in `JSON.stringify`) don't crash the config shell (Edge E-23/E-24).

---

## 📋 Bad-Spec (5) — Spec amendments before next iteration

### B-01 — AC3 `cost_estimate_usd` type contradicts decision #9 and the implementation
- **Spec lines:** AC3 (`float | None`) vs Decision #9 (`Decimal → float`) vs impl (`Decimal | None → str`).
- **Amendment:** change AC3 to `cost_estimate_usd: str | None` (Decimal stringified, JSON-safe). Drop "→ float" from Decision #9.

### B-02 — M-slot collision: `m7_playground` vs `m7_chat`
- **Spec target architecture (line 7) + File List name the module `m7_playground`.** The architectural M-slot map (and `.import-linter` Contract 1) reserves `m7_chat` for the M7 slot.
- **Amendment:** pick one: (a) rename module to a non-conflicting prefix (e.g., `features/playground/` un-prefixed, or `m13_playground`); (b) explicitly extend the M-slot map to allow `m7_playground` co-tenant + amend Contract 1 list. Decision must be reflected in spec architecture section.

### B-03 — AC7 step 3 "tool_invocations × 1" contradicts Decision #11 ("tool_invocations=[] Sprint 1")
- **Amendment:** AC7 step 3 should require `tool_invocations == []` for Sprint 1; "× 1" requirement moves to Story 4.x (D80).

### B-04 — Decision #3 cites wrong import-linter contract number
- **Spec line:** Decision #3 says "Contract 3" — Contract 3 is external-package forbidden modules. Feature isolation = Contract 1.
- **Amendment:** change "Contract 3" → "Contract 1" + add concrete action item: register `m7_playground` in the contract.

### B-05 — Pré-requis line 110 has stale `LLMRouter` signature
- **Spec text:** "`LLMRouter.complete(messages) → Completion(content, token_usage, ...)`".
- **Reality (Story 1.6):** `complete(messages, model, max_tokens, temperature, system=..., timeout_s=...) → Completion(text, input_tokens, output_tokens, cost_estimate_usd, ...)`.
- **Amendment:** update line 110 to the real Story 1.6 signature.

### (Also worth resolving but lower-priority bad-spec candidates from Auditor):
- B-06 — `tenant_id` field on event vs anti-scope (j) — either remove field or drop anti-scope claim.
- B-07 — T4.2 / T4.6(5) describe tool-call resolution that Decision #11 explicitly defers — mark as D80.
- B-08 — AC2 "si décidé" hedge on audit event is stale after John's 2026-05-11 decision — remove hedge.

---

## ⏸️ Defer (8) — Pre-existing or out-of-scope

- **D-2.7-1** — Audit publish lost when DB session pool is poisoned by upstream LLM exception (FinOps Story 9.4) (Blind F-04).
- **D-2.7-2** — `tenant_id=None` hardcoded everywhere (Story 12 multi-tenant) (Blind F-07).
- **D-2.7-3** — `enabled_tool_ids` is decorative Sprint 1 — checkboxes don't gate LLM access since `tool_use` formal is D80 (Blind F-08).
- **D-2.7-4** — Audit event `actor="system"` hardcoded (D1 Story 9.1 cleanup) (Blind F-27).
- **D-2.7-5** — Frontend tests stub `fetch` globally without verifying auth/content-type headers (Blind F-30).
- **D-2.7-6** — `PlaygroundService` could still bypass AC2 via global-module-level imports; structural constructor test is the weakest possible isolation check (Blind F-38; partial action P-15 above strengthens it).
- **D-2.7-7** — Story-2.6-inherited: `_redact_arguments` reuse for `arguments_redacted` in `ToolInvocationLog` is dead code Sprint 1; alive Story 4.x when tool_use lands.
- **D-2.7-8** — Sprint 1 lacks budget/rate-limit cap on Playground runs per user; Story 9.4 FinOps will own.

---

## ❌ Rejected (15) — Noise / false positives / handled elsewhere

| Source | Reason for rejection |
|---|---|
| Blind F-19 (P-23 gate is wrong) | Decision #14 Story 2.6 CR explicitly aligned Playground with `AGENTIVE_ALLOW_MCP_REGISTRATION`. Intentional defense-in-depth. |
| Blind F-20 (settings monkeypatch fragility) | Router reads `settings.mcp_allow_registration` per-request; monkeypatch.setattr scoping is standard pytest. |
| Blind F-23 (apiFetch content-type) | `apiFetch` shared helper handles this; out of diff scope. |
| Blind F-24 (e2e_auth_headers callable) | Conftest re-export convention check; no bug. |
| Blind F-26 (enabled_tool_ids dedup undocumented) | Sub-finding of F-08, already covered by P-25/D-2.7-3. |
| Blind F-29 (cost not in structlog) | Audit event carries it; logs are a secondary surface. |
| Blind F-32 (default tab="raw") | UX preference, not a defect. |
| Blind F-36 ("9 hits" TODO count) | Project bookkeeping, not code. |
| Edge E-17 (flag flipped mid-request) | Single-read at request entry already. |
| Auditor B-04 (Contract 3 typo) | Promoted to bad-spec B-04 above. |
| Auditor M-06 (arguments_redacted never populated) | Sprint 1 design — tool_invocations always `[]`; promoted to D-2.7-7. |
| Auditor "Scope Creep" section | Empty — no creep observed. |
| Edge E-23 (JSON.stringify circular ref) | Server-controlled data path; unreachable. Promoted to P-40 as part of broader error boundary. |
| Auditor D-02 (tool_invocations=[] vs AC7) | Promoted to bad-spec B-03. |
| Auditor D-05 (M-slot collision) | Promoted to bad-spec B-02 + patch P-05. |

---

## 📊 Summary

| Bucket | Count |
|---|---|
| 🔴 Critical | **1** |
| 🟠 High | **5** |
| 🟡 Medium | **12** |
| 🟢 Low / Polish | **20** |
| 📋 Bad-spec | **5** (+3 minor B-06/B-07/B-08) |
| ⏸️ Defer | **8** |
| ❌ Rejected | **15** |
| **Total raw** | **83** |
| **Total triaged unique** | **51** patches + 8 bad-spec + 8 defer |

---

## 🚦 Recommended Next Actions

1. **Block bump-to-done** until P-01 is fixed and `make test` is re-run from a clean state. The dev's "580 tests pass" claim must be re-validated empirically.
2. **Cluster A (Critical + High)** — P-01..P-06 — small fix-batch, ideally same commit cluster: SyntaxError, prompt injection / secret leakage, OTel attribute, frontend tests, import-linter Contract 1 registration, smoke re-run with valid `ANTHROPIC_API_KEY`.
3. **Resolve bad-spec amendments (B-01..B-05)** in the spec file *before* Cluster A — they unblock cleanly-aligned fixes (especially B-02 which decides whether to register `m7_playground` or rename).
4. **Cluster B (Medium)** — P-07..P-19 — backend correctness/security hardening + missing AC tests.
5. **Cluster C (Low/Polish)** — P-20..P-40 — defensive polish; can be batched.
6. After fix-batch: re-run smoke runtime end-to-end with a 200 LLM success path and capture it in Completion Notes (P-06 evidence).

---

## Process Note

- The Blind Hunter, with only the diff and no project context, correctly surfaced the SyntaxError that all three reviewers independently flagged. This is exactly the kind of defect a context-loaded reviewer (or the dev themselves) misses by assuming intent.
- The dev's Completion Notes (`580 backend tests pass, 0 régression, lint vert`) appear to overclaim — `python3 -m py_compile` on `service.py` was sufficient to falsify them. Worth tightening the dev-side gate so a `make ci` invocation is part of the DoD before bumping to `review`.
