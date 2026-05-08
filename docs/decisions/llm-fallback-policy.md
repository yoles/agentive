# LLM Fallback Policy (Story 1.6)

**Status:** Accepted (Sprint 0)
**Date:** 2026-05-02

## Canonical chain

Default `provider_chain` = `["anthropic", "openai"]`.

Anthropic comes first because:

- Prompt caching support reduces cost on long system prompts (Pôle
  Dev architect/researcher agents will use 50KB+ system prompts).
- Sonnet 4.6 has a better quality / cost ratio than GPT-5 on the code
  workflows we benchmark against (qualitative — to be revisited Sprint 2+).

Per-agent override is supported: an agent template can declare
`provider_chain = ["anthropic"]` (no fallback) or
`provider_chain = ["openai", "anthropic"]` (OpenAI primary).

## Error classification table

| Exception                                | Class                          | Rationale                                                |
| ---------------------------------------- | ------------------------------ | -------------------------------------------------------- |
| `LLMProviderTimeoutError`                | `retriable_with_fallback`      | Provider slow — fallback may be faster                   |
| `LLMProviderUnavailableError` (5xx, conn) | `retriable_with_fallback`      | Provider degraded — fallback may be reachable            |
| `LLMProviderRateLimitError` (429)        | `retriable_with_fallback`      | Sprint 0 — fallback. Story 9.5 adds intra-provider retry |
| `LLMProviderAuthError` (401/403)         | `fatal`                        | Auth invalid — fallback won't fix                        |
| `LLMProviderBadRequestError` (400/422/404) | `fatal`                        | Payload bug — fallback may mask the bug                  |
| `LLMNoFallbackModelError`                | `fatal`                        | Misconfig — surface the missing map entry                |
| `TypeError`, `ValueError`                | `fatal`                        | Programmer bug                                           |
| Anything else                            | `retriable_with_fallback`      | Conservative default — chain is bounded                  |

The bucket `retriable_same_provider` is reserved for **Story 9.5** (rate
limiting + back-off). Sprint 0 keeps it empty.

## `MODEL_FALLBACK_MAP` snapshot (2026-05)

Encoded in `agentive_backend.shared.llm.router.DEFAULT_MODEL_FALLBACK_MAP`.

| Primary model               | OpenAI fallback     | Anthropic fallback     |
| --------------------------- | ------------------- | ---------------------- |
| `claude-opus-4-7`           | `gpt-5`             | —                      |
| `claude-sonnet-4-6`         | `gpt-5`             | —                      |
| `claude-haiku-4-5`          | `gpt-5-mini`        | —                      |
| `claude-haiku-4-5-20251001` | `gpt-5-mini`        | —                      |
| `gpt-5`                     | —                   | `claude-sonnet-4-6`    |
| `gpt-5-mini`                | —                   | `claude-haiku-4-5`     |
| `gpt-4.1`                   | —                   | `claude-sonnet-4-6`    |
| `o4-mini`                   | —                   | `claude-haiku-4-5`     |

A missing entry triggers `LLMNoFallbackModelError` rather than silently
downgrading. Stories that need a custom map (e.g. forbid Sonnet → Haiku
silent downgrade) inject their own `model_fallback_map` at router
construction time.

## Pricing snapshot policy

`MODEL_PRICING` in `infra/llm/anthropic_adapter.py` and
`infra/llm/openai_adapter.py` mirrors the public pricing pages:

- Anthropic — https://docs.anthropic.com/claude/docs/models-overview#model-pricing
- OpenAI — https://platform.openai.com/docs/pricing

Updates are merged via PR with `(pricing snapshot YYYY-MM)` in the
commit message. A model absent from the table yields
`Completion.cost_estimate_usd = None`; the
`agentive_llm_cost_usd_total` metric skips that call (no double-counting).

## Update procedure

When a provider releases a new generation (e.g. Claude 5, GPT-6):

1. Add the new model to `MODEL_PRICING` with the input/output tuple.
2. Decide its fallback target on the **other** provider and add it to
   `DEFAULT_MODEL_FALLBACK_MAP`. If unsure, leave it out — callers will
   get an explicit `LLMNoFallbackModelError` rather than a wrong fallback.
3. Update this document's snapshot table and bump the date in the
   header.
4. Land via a PR labelled `pricing-snapshot` — review by John.
