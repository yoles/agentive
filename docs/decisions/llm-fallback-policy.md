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
`provider_chain = ["anthropic"]` or `provider_chain = ["openai", "anthropic"]`.

**What those declarations actually do changed in Story 4.6, and this
paragraph said otherwise until 2026-09-12.**

* **The order you write is not necessarily the order that runs.**
  `LLMRouter._resolve_model` hands index 0 the primary model VERBATIM and
  consults the fallback map only from index 1 on, so a chain whose head does
  not own `llm_model` would send that model to the wrong provider — a 400,
  classified `fatal`, with no fallback and no retry.
  `domain/provider_chain.resolve_provider_chain` therefore **rotates the
  model's owning provider to the head**. `["openai", "anthropic"]` is
  *not* "OpenAI primary" whenever `llm_model` is Anthropic-owned — which
  includes the very common case of `llm_model` being **unset**, since both
  `agent_node` and `dry_run` default it to `claude-sonnet-4-6`. It is
  "OpenAI as the fallback leg". A rotation is logged
  (`provider_chain_reordered`).
* **A single-provider chain is not a guarantee of "no fallback."** When the
  declared chain has no usable provider left after intersection with what the
  process registered, it is dropped and the node runs on the process default
  chain — which does fall back. That gap is recorded as defer **D96**;
  a template that needs a provider CONSTRAINT (data residency, say) does not
  have one today.

See `docs/runbooks/run-control-et-fallback.md` § 7 for the resolution table.

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
limiting + back-off). Sprint 0 keeps it empty, and it still is — Story 4.6
deliberately did not touch this table (a canary test,
`test_no_error_currently_classified_as_same_provider_sprint_0`, fails if
anyone moves 429 out of `retriable_with_fallback`).

## Three retry levels (Story 4.6)

Three distinct mechanisms, owned by three different stories, and very easy
to merge by mistake. They compose: level 2 wraps level 1, and level 3 would
sit inside level 1.

| # | Mechanism | Trigger | Action | Owner | Status |
| - | --------- | ------- | ------ | ----- | ------ |
| 1 | **Chain fallback** | provider A fails `retriable_with_fallback` | try provider B with the equivalent model from `DEFAULT_MODEL_FALLBACK_MAP` | `LLMRouter.complete()` — Story 1.6 | ✅ delivered |
| 2 | **Node retry** | the WHOLE chain failed (`LLMAllProvidersFailedError`) | re-run the whole chain, with backoff, up to `max_retries` | `execute_agent_node` — Story 4.6 (défer D13) | ✅ delivered |
| 3 | **Intra-provider retry** | 429 from A | wait, re-try A without switching provider | `retriable_same_provider` bucket | ⏭ Story 9.5 |

### Consequence: `error_policy.on_timeout` does NOT control fallback

`agent_templates.config["error_policy"].on_timeout` selects level 2 only:

- `retry_with_backoff` → up to `max_retries` node retries;
- `fail_fast` → **zero node retries**;
- `fallback_provider` → **zero node retries** (identical to `fail_fast`).

In all three cases the level-1 chain still applies. `fail_fast` means "do
not retry this node", never "do not fall back" — disabling the chain would
contradict NFR12, which is not negotiable per template. An agent that
genuinely wants a single provider says so with `provider_chain: ["anthropic"]`,
the override this document already describes above.

Level 2 fires **only** on `LLMAllProvidersFailedError`. A `fatal` error
propagates on the first attempt: the router raises those as-is precisely so
a misconfiguration surfaces instead of being retried into invisibility.

**Except when the fatal error arrives wrapped.** Raised as-is holds only for
a failure on the chain's FIRST provider. Fail fatally further down — the
classic being an `llm_model` absent from `DEFAULT_MODEL_FALLBACK_MAP`, i.e.
`LLMNoFallbackModelError` — and `LLMRouter` wraps it in
`LLMAllProvidersFailedError`, whose type says "the whole chain failed,
retriable" while its cause says the opposite. Level 2 then retried a
misconfiguration three times with backoff for three rigorously identical
failures, which is the outcome this paragraph claims to prevent. So
`execute_agent_node` reads `error_class` off the LAST attempt in the
exception's context rather than trusting the exception's type, and stops on
`fatal` (Story 4.6 review, P3).

### Runtime cap on `max_retries`

Story 2.2's `ErrorPolicy` schema validates `max_retries` up to 10; the
runtime honours at most **3** (`domain/error_policy.MAX_RUNTIME_RETRIES`).

`recovery.derive_stale_threshold_s` — which computes the point past which a
silent `running` row is treated as orphaned and re-executed — reads the
worst-case duration of a single node, retries and backoff included. Derived
against 10 retries it exceeds an hour, meaning a genuinely crashed run would
sit unexamined for an hour: one template's configuration degrading crash
detection for every other run in the process. The schema validates an
intention; the runtime guarantees a process invariant. A template asking for
more is capped and logged (`error_policy_retries_capped`), never silently.

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
