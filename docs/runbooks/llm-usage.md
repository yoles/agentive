# LLM Usage Runbook

## Canonical caller pattern

```python
from agentive_backend.shared.llm import (
    AgentLLMConfig,
    ChatMessage,
    LLMRouter,
)

# router is injected via FastAPI Depends(get_llm_router) — see api/deps.py
async def call_llm(router: LLMRouter, user_input: str) -> str:
    completion = await router.complete(
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content=user_input),
        ],
        model="claude-sonnet-4-6",
        max_tokens=1024,
        temperature=0.5,
        timeout_s=30.0,
    )
    return completion.text
```

The router falls back from Anthropic to OpenAI automatically on retriable
errors (5xx, timeouts, 429). Fatal errors (401/403/4xx≠429) short-circuit.

## Per-agent override

Pass `provider_chain=` to `complete()` to override the default chain for
a single call:

```python
# Force Anthropic only — no fallback (use case: prompt caching matters
# more than availability for this agent).
await router.complete(
    messages=[...], model="claude-sonnet-4-6", max_tokens=2048,
    provider_chain=["anthropic"],
)
```

## Anthropic prompt caching (escape hatch)

```python
anthropic = router.providers["anthropic"]
response = await anthropic.raw_provider_call(
    model="claude-sonnet-4-6",
    max_tokens=2048,
    system=[
        {
            "type": "text",
            "text": LARGE_SYSTEM_PROMPT,  # 50 KB+
            "cache_control": {"type": "ephemeral"},
        }
    ],
    messages=[{"role": "user", "content": user_input}],
)
# response is the raw langchain_core.AIMessage — no normalization,
# no metrics, no fallback. Caller owns the format.
return str(response.content)
```

## Adding a new provider (3rd, 4th, …)

1. Create `backend/src/agentive_backend/infra/llm/<name>_adapter.py`
   implementing the `LLMProvider` Protocol — see `anthropic_adapter.py`
   for the canonical structure.
2. Add per-model entries to `MODEL_PRICING` with `(input, output)` USD
   per million tokens.
3. Update `DEFAULT_MODEL_FALLBACK_MAP` in `shared/llm/router.py` with
   the cross-provider equivalences. Leave entries out rather than
   guessing — `LLMNoFallbackModelError` is louder than silent wrong
   fallback.
4. Wire the adapter in `app/lifespan.py:_build_llm_router()`.
5. Add unit tests under `tests/unit/llm/test_<name>_adapter.py` — a
   parametrized test for `_resolve_max_tokens_kwarg` (or its
   equivalent), the response mapping, and the exception classifier.
6. Add an integration test under `tests/integration/llm/` using
   `httpx.MockTransport` (no real network).
7. Update this runbook if the new provider introduces a novel pattern
   (streaming format, tool calling shape, vision input, …).

## Debugging fallback issues

**Symptom:** an agent calls `router.complete()`, the primary fails, but
the fallback never runs.

1. Check that the exception type is in `retriable_with_fallback`. Look
   at `Tasks/Subtasks → AC5` in story 1.6 for the table, or grep
   `error_classifier.py`. If the upstream SDK raised an error not
   covered, the default branch returns `retriable_with_fallback` so
   this should be rare — if it's classified as `fatal`, the chain
   short-circuits intentionally.
2. Check `DEFAULT_MODEL_FALLBACK_MAP[model]` has an entry for the next
   provider in the chain. Missing entries raise
   `LLMNoFallbackModelError` (fatal) — surface the misconfig.
3. Check the metric `agentive_llm_fallback_triggered_total` is
   incrementing in your Prometheus snapshot. If yes, fallback is
   happening but the secondary provider is also failing — look at
   `LLMAllProvidersFailedError.context["attempts"]` for the per-attempt
   breakdown.
4. Check structlog for `event=llm_router.fallback_triggered` records —
   they include the failed provider, error class, and next provider.

**Symptom:** `LLMNoFallbackModelError` raised at boot or on first call.

The router resolved the primary model but found no fallback mapping
for the next provider. Either:

- Add an entry to `DEFAULT_MODEL_FALLBACK_MAP[<primary_model>]` keyed
  by the next provider name (e.g. `"claude-sonnet-4-6": {"openai": "gpt-5"}`).
- Or instantiate the router with a custom `model_fallback_map=` kwarg
  if the agent should use a non-canonical mapping.
- Or restrict the chain to a single provider via `provider_chain=["anthropic"]`
  if no fallback is desired for this agent.

## Updating MODEL_PRICING

Run a `git grep MODEL_PRICING` and:

1. Compare each model's tuple with the live pricing page (Anthropic
   https://docs.anthropic.com/claude/docs/models-overview#model-pricing,
   OpenAI https://platform.openai.com/docs/pricing).
2. Update the tuples in
   `infra/llm/anthropic_adapter.py` and `infra/llm/openai_adapter.py`.
3. Update the snapshot date comment at the top of each file.
4. Bump the date in `docs/decisions/llm-fallback-policy.md`.
5. Land via a PR labelled `pricing-snapshot`.

## Running tests without API keys

The integration test suite never makes real network calls. The autouse
fixture `_no_external_http` (in `tests/integration/llm/conftest.py`)
intercepts every `httpx.AsyncClient.send` and refuses any non-loopback
URL. Use `MockProvider` (in `agentive_backend.shared.llm.testing`) for
unit tests and `httpx.MockTransport` for adapter-level integration tests.

## NFR9 — caller responsibility (Story 1.6 review fix-batch P15)

> ⚠️ **Do NOT pass API keys, OAuth tokens, or other secrets in
> `messages[*].content` or in the `system=` kwarg.**

The redaction processor that runs in the structlog pipeline only
catches the **well-known prefixes** (`sk-ant-`, `sk-proj-`, `sk-`,
`pa-`). Arbitrary secrets in user-controlled content escape unredacted
into:

- `Completion.text` (returned to the caller and stored in event payloads)
- `workflow_engine.llm.completion_received` event payloads (Story 4.6)
- The `attempts` context of `LLMAllProvidersFailedError` (RFC 7807 body
  served to the client)

If an upstream tool output contains a secret, **the caller MUST redact
it before passing it through `LLMRouter.complete()`**. Anthropic and
OpenAI providers can echo prompts back inside their error messages,
which would propagate the leak across the wire even with the SDK
exception classifier in place.

For internal helpers that receive tool outputs likely to contain
secrets (e.g. MCP servers exposing environment variables), wrap the
content through `agentive_backend.shared.llm.redaction.redact_secrets`
before constructing the `ChatMessage`.

## NFR9 — verifying API key redaction

The structlog pipeline runs `redact_api_keys_processor` immediately
before `JSONRenderer`. Smoke check it:

```bash
docker compose run --rm backend uv run python - <<'PY'
import structlog
from agentive_backend.shared.logging import configure_logging
configure_logging()
log = structlog.get_logger()
log.info("test", body="leaked sk-ant-fake1234567890abcdefghij1234567890")
PY
```

Output should contain `[REDACTED]` rather than the fake key.

## Résumés de passage (Story 4.7, FR53)

`features.workflow_engine.engine.handoff.summarize_handoff` condenses a
node's output into a `HandoffSummary` (`decisions`/`artifacts_refs`/
`blockers`/`next_questions`) for the next agent in the chain, so the next
node's prompt carries the condensed summary instead of the raw output —
default behaviour.

**What this does and does not fix.** It shrinks the CONSTANT, not the order
of growth. `_serialize_upstream` still hands a node every upstream output
committed before it, one entry per upstream node, so prompt size across a
long linear DAG still grows quadratically with node count — replacing each
term with a smaller term does not change that. `MAX_UPSTREAM_OUTPUT_CHARS`
(50 000 chars, oldest entries evicted first, drop logged as
`workflow_engine.upstream_outputs_truncated`) remains the actual bound, and
on a wide enough DAG it will still fire. Do not read this feature as a
reason to stop watching that log line.

### The model has no `provider_chain`, on purpose

`summarize_handoff` is called with the engine's own model
(`AGENTIVE_WORKFLOW_HANDOFF_SUMMARY_MODEL`, default `claude-haiku-4-5`) and
no `provider_chain=` — mirror the hybrid-routing escalation call
(`engine/hybrid_router.py`). Both are PROCESS-WIDE auxiliary calls, not a
behaviour of the agent template's author: a per-agent `provider_chain`
governs the agent's own completion, never the engine's internal
optimizations. This also means summarization is NOT retried and NOT
fallback-chained the way a node's own completion is (Story 4.6's
`error_policy` dispatcher) — a failed summary degrades to the raw output for
that one hop (see below), so retrying it would only add latency to a
best-effort call.

### What the ratio actually measures

`reduction_ratio` compares, **per substitution actually performed**, the
producer's own output tokens against the tokens of the summary that replaced
them in a consumer's prompt. Both figures are router-measured; no tokenizer
is involved anywhere.

Two channels, two questions, and they are deliberately not the same number:

| Channel | Keyed by | Answers |
|---|---|---|
| `handoffs` | producer | what producing the summaries **cost** (`metrics.handoffs.tokens`, `.cost_usd`, folded into the run totals) |
| `handoff_substitutions` | consumer | what substituting them actually **saved** (`raw_tokens_replaced`, `summary_tokens`, `reduction_ratio`) |

The consumer side is what feeds FR53's ratio, because a summary only saves
anything at the moment a prompt uses it instead of the raw output. Three
cases make the two sides genuinely differ, and all three used to be credited
as savings that never happened: a consumer that opted out and read the raw
output anyway; an entry the `MAX_UPSTREAM_OUTPUT_CHARS` cap dropped before
any prompt saw it; and, on a chain, an output that `_serialize_upstream`'s
cumulative upstream view forwards to *every* downstream node in turn —
replaced as many times as it is forwarded, not once.

`handoff_substitutions[consumer]["sources"]` lists which producers that
prompt condensed, so a run's checkpoint says where a reported saving came
from rather than asking you to trust the arithmetic.

A run resumed from a checkpoint written before this change has no
`handoff_substitutions` channel: the ratio then reads `null`, the same
honest "nothing measured" as a workflow that never substituted — never a
back-filled figure.

### Reading the metric

Per-run: `workflow_runs.metrics.handoffs` = `{raw_tokens_replaced,
summary_tokens, reduction_ratio}` — `reduction_ratio` compares the producing
node's own `output_tokens` (what would have been forwarded raw) against the
summary's `output_tokens` (what was forwarded instead). `None` when no
handoff exists in the run (e.g. every node was terminal).

Per-workflow (aggregated across every run): `GET
/api/v1/workflows/{workflow_id}/handoff-stats`, mirror
`/routing-stats` — never a Prometheus label (unbounded cardinality by
`workflow_id`, same rule as the hybrid-routing ratio, see
`features/workflow_engine/metrics.py`'s own module docstring).

### Disabling it for one agent

Set `config["include_raw_previous_output"] = true` on the CONSUMING
template — it reads `node_outputs` (the raw upstream output) in full instead
of `handoffs`, exactly the pre-4.7 behaviour. Only the literal boolean
`True` opts out; a mistyped value (e.g. the string `"true"`) does not — and
since the review of 2026-09-12 a mistyped value is logged
(`workflow_engine.include_raw_previous_output_ignored`) rather than ignored
in silence, because `config` is a free JSONB blob that no schema validates.

Opting a consumer out also stops its PRODUCERS paying for summaries nobody
would read: a node whose successors have ALL opted out is not summarized at
all. Before that fix the opt-out increased the bill — the run paid for the
summary *and* forwarded the raw output.

### Disabling it fleet-wide (rollout / rollback)

`AGENTIVE_WORKFLOW_HANDOFF_SUMMARY_ENABLED=false` turns off every handoff
summary without touching a single template: the engine falls back to the
pre-4.7 path byte for byte. Use it to stage a rollout, or to roll one back
without editing every consuming template under incident.

This matters because summarization is **on by default**, which changes the
prompt of templates already in production. A template written against an
upstream node's raw output (reading, say, `upstream_outputs["a"]["invoice_id"]`)
stops finding that field, with no change to its own config. If you are
enabling this on an existing fleet, turn it on deliberately and watch the
consuming agents' outputs — the per-template opt-out is the permanent fix,
this switch is the emergency brake.

### Failure mode

A failed summarization (LLM error, unparsable response, a response that
does not validate against `HandoffSummary`, an empty summary, or a reply cut
off at `max_tokens`) is logged (`workflow_engine.handoff_summary_failed`),
counted (`agentive_workflow_engine_handoff_summary_failures_total{reason}`)
and simply omits that node from `handoffs` for the run — the next node then
falls back to that ONE node's raw output (never the whole prompt).
`node_outputs` and the run's own correctness are never affected either way.

**Alert on the counter, not on the ratio.** `GET /workflows/{id}/handoff-stats`
returns `reduction_ratio_pct: null` both when no node in the workflow has a
downstream successor and when every summary failed — a misconfigured
`AGENTIVE_WORKFLOW_HANDOFF_SUMMARY_MODEL` looks exactly like a workflow that
never summarizes, while billing one wasted LLM round-trip per node.
`reason="LLMNoFallbackModelError"` climbing is that case.

A `reduction_ratio` below zero means the summaries came out BIGGER than the
outputs they replaced (common on nodes with very short, structured outputs).
The value is reported as measured rather than clamped — clamping would erase
the only signal saying the optimization costs more than it saves — and
`workflow_engine.handoff_summary_inflated` is logged when it happens.
