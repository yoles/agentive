# ADR — LLM Abstraction (Story 1.6)

**Status:** Accepted (Sprint 0)
**Date:** 2026-05-02
**Ticket:** Story 1.6 — Core LLM abstraction (multi-provider)

## Context

Agentive depends on Large Language Models for every agent inference, every workflow step,
every memory ingestion. The architecture (planning-artifacts/architecture.md ligne 393)
mandates a **multi-provider abstraction** to satisfy:

- **NFR12** — graceful degradation when one provider is unavailable
- **NFR20** — at least two providers (Anthropic + OpenAI) supported simultaneously
- **NFR9** — no API keys in clear text in logs / traces
- **NFR14** — configurable timeouts + retry + back-off

Without an abstraction layer, every agent module would import the vendor SDKs
directly. Three concrete consequences:

1. **Lock-in** — swapping a provider would require touching every consumer
   (12 feature modules in our `features/m*` plan).
2. **Fallback impossible** — Story 4.6 (workflow engine fallback) cannot be
   implemented without a routing point that owns the chain.
3. **Security drift** — exception messages from raw SDKs leak query strings,
   headers, and occasionally the API key itself; without a central choke
   point we would need to audit every call site for redaction.

## Decision

We expose a thin facade in `agentive_backend.shared.llm`:

- `LLMProvider` (`runtime_checkable` Protocol) with two methods:
  - `complete(messages, *, model, max_tokens, …) -> Completion` — the
    canonical, provider-agnostic chat-completion API.
  - `raw_provider_call(**provider_specific_kwargs) -> Any` — escape
    hatch for vendor-specific features (Anthropic prompt caching,
    OpenAI parallel tool calls, JSON mode, vision, …).
- `LLMRouter` accepting a `provider_chain` (e.g. `["anthropic", "openai"]`)
  and falling back automatically on retriable errors (5xx, timeouts,
  429, connection issues). Fatal errors (401, 403, 4xx≠429, validation)
  short-circuit the chain immediately.
- `MODEL_FALLBACK_MAP` translates a primary model name to its fallback
  equivalent on a different provider (e.g. `"claude-sonnet-4-6" → {"openai": "gpt-5"}`).
  No silent guessing — a missing entry raises `LLMNoFallbackModelError`.

Concrete adapters live in `agentive_backend.infra.llm`:
- `AnthropicProvider` wraps `langchain_anthropic.ChatAnthropic`.
- `OpenAIProvider` wraps `langchain_openai.ChatOpenAI`, handling the
  `max_tokens` vs `max_completion_tokens` divergence between legacy and
  new OpenAI models.

`import-linter` Contract 5 forbids `features/*` and `api/*` from importing
`langchain_anthropic`, `langchain_openai`, `langchain_core`, `anthropic`,
or `openai` directly. The choke point is the router.

## Options Considered

### Option A — `litellm` as a unified gateway

Pro: single dependency, hundreds of providers covered, fallback built-in.
Con: opaque exception translation (we still need the abstraction layer
to normalize exceptions for our `LLMError` hierarchy), additional
dependency mass with security surface, prompt caching support lags
behind official SDKs by weeks. Also: `litellm` would still need
wrapping to cover the `complete()` / `raw_provider_call()` split, so
the layer would not actually disappear.

### Option B — LangChain partner SDKs (chosen)

Pro: already pinned by Story 1.1 for the LangGraph spike, well-typed
output (`AIMessage` with `usage_metadata`), prompt caching exposed
natively, no extra dependency. Con: LangChain's typing is too
permissive (`content: str | list[ContentBlock]`) — we re-normalize.
Mitigation: tight unit tests on the mapping functions.

### Option C — Raw `httpx` against vendor REST APIs

Pro: minimal dependency surface, full control.
Con: Anthropic SSE format is messy, OpenAI streaming nuances, retries
on transient errors must be implemented from scratch, breaking changes
on the API would force us to rewrite. Maintenance burden too high for
solo dev.

We chose **Option B** because it leverages a dependency we already
own, gives us SDK-quality response parsing, and keeps the abstraction
layer thin enough to swap if needed.

## Why a Protocol, not an ABC

Structural typing matches the duck behaviour we need (each adapter is
written independently, no shared state). `runtime_checkable` lets the
router validate at registration time without forcing inheritance.

## Why `raw_provider_call` instead of unified tool-calling

Tool calling APIs differ enough across providers (Anthropic uses
`tool_use` content blocks, OpenAI uses `tool_calls` in the assistant
message, with parallel tool calls being an OpenAI-specific kwarg)
that unifying them would either lose information or invent a
lowest-common-denominator schema that satisfies neither side. The
escape hatch keeps `complete()` clean for the 80% case and lets the
20% (advanced tool use, prompt caching) opt in explicitly.

## Why no intra-provider retry Sprint 0

Retry-with-back-off intra-provider belongs to **Story 9.5** (rate
limiting + multi-provider). Sprint 0 implements only fallback to the
next provider. The classification function (`classify_error`) already
has a `retriable_same_provider` bucket that is intentionally empty
right now — Story 9.5 will populate it.

## Consequences

**Positive:**

- Adding a 3rd provider (Mistral, Voyage, Gemini, …) is one new
  adapter class + entries in `MODEL_PRICING` and `MODEL_FALLBACK_MAP`.
  No `features/*` change required.
- NFR12 implementable from Sprint 0 (router does the dispatch).
- NFR20 met (≥ 2 providers active by default in production).
- NFR9 enforced by the structlog `redact_api_keys_processor` running
  before `JSONRenderer`, plus `redact_secrets()` applied to every
  exception detail before it is stored or published.
- Tests run with zero network calls (fixture `_no_external_http` autouse).

**Negative:**

- LangChain version upgrades may break our mapping (`response.usage_metadata`
  shape changed in 1.3 → 1.4). Mitigation: pinned bounds + integration tests.
- Each `complete()` call instantiates a fresh `ChatAnthropic` /
  `ChatOpenAI` (TCP handshake cost). Sprint 4+ optimization: cache
  instances by `(model, kwargs_signature)` hash if metrics reveal a
  meaningful share of latency. Sprint 0: dominated by inference time.

## Revisitability

Triggers to re-open this decision:

- Any vendor breaks Anthropic's prompt caching contract such that
  `raw_provider_call()` no longer works → migrate to `litellm` or raw
  HTTP.
- LangChain partner SDK abandoned / not updated for > 6 months → the
  abstraction protects us, but we should pin the underlying vendor SDK
  directly and write a minimal client.
- Cost or latency overhead of the LangChain layer becomes meaningful
  (> 5% of LLM call wall-clock) → consider direct vendor SDK wrapping.

## References

- `_bmad-output/planning-artifacts/architecture.md` lignes 393, 451,
  475, 554-563, 607, 1525-1568.
- `_bmad-output/planning-artifacts/prd.md` NFR9 / NFR12 / NFR14 / NFR20.
- `_bmad-output/implementation-artifacts/1-6-core-llm-abstraction.md`
  — story spec.
- `docs/decisions/llm-fallback-policy.md` — runtime classification
  table + `MODEL_FALLBACK_MAP` snapshot.
- `docs/runbooks/llm-usage.md` — caller patterns + debugging.
