"""Error classification table — exhaustive coverage AC5."""

from __future__ import annotations

import httpx
import pytest

from agentive_backend.shared.llm.error_classifier import classify_error
from agentive_backend.shared.llm.exceptions import (
    LLMNoFallbackModelError,
    LLMProviderAuthError,
    LLMProviderBadRequestError,
    LLMProviderRateLimitError,
    LLMProviderTimeoutError,
    LLMProviderUnavailableError,
)


@pytest.mark.parametrize(
    "exc",
    [
        LLMProviderTimeoutError("timeout"),
        LLMProviderUnavailableError("503"),
        LLMProviderRateLimitError("429"),
        # The router accepts an unknown adapter exception as retriable to keep
        # the chain progressing rather than fataling on unexpected types.
        RuntimeError("unknown adapter quirk"),
    ],
)
def test_retriable_with_fallback(exc: Exception) -> None:
    assert classify_error(exc, "anthropic") == "retriable_with_fallback"


@pytest.mark.parametrize(
    "exc",
    [
        LLMProviderAuthError("401"),
        LLMProviderBadRequestError("400"),
        LLMNoFallbackModelError("no map"),
        TypeError("bug"),
        ValueError("bug"),
    ],
)
def test_fatal(exc: Exception) -> None:
    assert classify_error(exc, "openai") == "fatal"


def test_no_error_currently_classified_as_same_provider_sprint_0() -> None:
    """Sprint 0 invariant: bucket reserved for Story 9.5 stays empty."""
    candidates = (
        LLMProviderTimeoutError("t"),
        LLMProviderRateLimitError("r"),
        LLMProviderUnavailableError("u"),
        httpx.ConnectError("c"),
    )
    for exc in candidates:
        assert classify_error(exc, "p") != "retriable_same_provider"
