"""LLM exception hierarchy — RFC 7807 contract per subclass."""

from __future__ import annotations

from agentive_backend.shared.exceptions import AgentiveError
from agentive_backend.shared.llm.exceptions import (
    LLMAllProvidersFailedError,
    LLMError,
    LLMNoFallbackModelError,
    LLMProviderAuthError,
    LLMProviderBadRequestError,
    LLMProviderRateLimitError,
    LLMProviderTimeoutError,
    LLMProviderUnavailableError,
)


def test_all_subclass_agentive_error() -> None:
    for cls in (
        LLMError,
        LLMProviderUnavailableError,
        LLMProviderTimeoutError,
        LLMProviderRateLimitError,
        LLMProviderAuthError,
        LLMProviderBadRequestError,
        LLMNoFallbackModelError,
        LLMAllProvidersFailedError,
    ):
        assert issubclass(cls, LLMError)
        assert issubclass(cls, AgentiveError)


def test_status_codes_are_canonical() -> None:
    assert LLMProviderUnavailableError.status == 503
    assert LLMProviderTimeoutError.status == 504
    assert LLMProviderRateLimitError.status == 429
    assert LLMProviderAuthError.status == 401
    assert LLMProviderBadRequestError.status == 400
    assert LLMAllProvidersFailedError.status == 503


def test_types_have_llm_namespace_uri() -> None:
    for cls in (
        LLMProviderUnavailableError,
        LLMProviderTimeoutError,
        LLMProviderRateLimitError,
        LLMProviderAuthError,
        LLMProviderBadRequestError,
        LLMNoFallbackModelError,
        LLMAllProvidersFailedError,
    ):
        assert cls.type.startswith("/errors/llm/")


def test_context_propagation() -> None:
    exc = LLMAllProvidersFailedError(
        detail="all 2 providers failed",
        context={"attempts": [{"provider": "anthropic", "error_type": "timeout"}]},
    )
    assert exc.context["attempts"][0]["provider"] == "anthropic"
    assert exc.detail == "all 2 providers failed"
