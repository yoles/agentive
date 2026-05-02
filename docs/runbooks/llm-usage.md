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
- `m3.llm.completion_received` event payloads (Story 4.6)
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
