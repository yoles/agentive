"""LLM error hierarchy — RFC 7807 ready.

All LLM-related exceptions inherit from :class:`LLMError` which itself
inherits from :class:`agentive_backend.shared.exceptions.AgentiveError`.
The FastAPI exception handler in ``app.main`` converts these to
``application/problem+json`` responses.

Two axes of classification
--------------------------
1. **Cause** : auth, rate limit, timeout, unavailability, validation, fallback.
2. **Recoverability** : ``retriable_with_fallback`` (5xx, timeout, 429, …) vs
   ``fatal`` (4xx ≠ 429, validation, no-fallback). Used by
   :func:`agentive_backend.shared.llm.error_classifier.classify_error`.
"""

from __future__ import annotations

from agentive_backend.shared.exceptions import AgentiveError


class LLMError(AgentiveError):
    """Base for all LLM-related exceptions."""

    type = "/errors/llm"
    title = "LLM error"
    status = 500


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Retriable (fallback can rescue)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class LLMProviderUnavailableError(LLMError):
    """Provider returned 5xx or is unreachable. Fallback should be tried."""

    type = "/errors/llm/provider-unavailable"
    title = "LLM provider unavailable"
    status = 503


class LLMProviderTimeoutError(LLMError):
    """Provider call exceeded the configured timeout. Fallback should be tried."""

    type = "/errors/llm/provider-timeout"
    title = "LLM provider timeout"
    status = 504


class LLMProviderRateLimitError(LLMError):
    """Provider returned 429. Sprint 0: fallback (no intra-provider retry)."""

    type = "/errors/llm/provider-rate-limit"
    title = "LLM provider rate-limited"
    status = 429


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fatal (fallback cannot rescue)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class LLMProviderAuthError(LLMError):
    """Invalid API key / 401 / 403. Fatal — fallback cannot rescue."""

    type = "/errors/llm/provider-auth"
    title = "LLM provider authentication failed"
    status = 401


class LLMProviderBadRequestError(LLMError):
    """Provider rejected the payload (400/422/404 model-not-found). Fatal — bug."""

    type = "/errors/llm/provider-bad-request"
    title = "LLM provider rejected request"
    status = 400


class LLMNoFallbackModelError(LLMError):
    """No equivalent model registered for the next provider in the chain."""

    type = "/errors/llm/no-fallback-model"
    title = "No fallback model registered"
    status = 500


class LLMAllProvidersFailedError(LLMError):
    """Every provider in the chain failed. ``context['attempts']`` lists each try."""

    type = "/errors/llm/all-providers-failed"
    title = "All LLM providers failed"
    status = 503
