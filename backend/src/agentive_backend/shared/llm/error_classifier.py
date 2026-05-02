"""Error classification for the fallback chain.

The router asks :func:`classify_error` whether the next provider in the
chain should be tried. The function is **pure** — no I/O, no logging —
so it can be unit-tested cheaply against the canonical error table
documented in the story Dev Notes.

Sprint 0 invariant: the ``retriable_same_provider`` bucket is **empty**.
Story 9.5 (rate limiting + back-off) will move 429 / transient 5xx
into it once we add intra-provider retries with jitter.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ValidationError as PydanticValidationError

from agentive_backend.shared.llm.exceptions import (
    LLMNoFallbackModelError,
    LLMProviderAuthError,
    LLMProviderBadRequestError,
    LLMProviderRateLimitError,
    LLMProviderTimeoutError,
    LLMProviderUnavailableError,
)

ErrorClass = Literal["retriable_with_fallback", "retriable_same_provider", "fatal"]


def classify_error(exc: Exception, _provider: str) -> ErrorClass:
    """Bucket ``exc`` for fallback routing decisions."""
    # Domain-typed errors get routed first — they already encode the
    # adapter's translation of the SDK / HTTP exception.
    if isinstance(exc, LLMProviderTimeoutError):
        return "retriable_with_fallback"
    if isinstance(exc, LLMProviderUnavailableError):
        return "retriable_with_fallback"
    if isinstance(exc, LLMProviderRateLimitError):
        return "retriable_with_fallback"
    if isinstance(exc, LLMProviderAuthError):
        return "fatal"
    if isinstance(exc, LLMProviderBadRequestError):
        return "fatal"
    if isinstance(exc, LLMNoFallbackModelError):
        return "fatal"

    # Programmer errors — never retry, never fallback. Pydantic v2's
    # ValidationError does NOT subclass ValueError, so it needs an
    # explicit branch (NFR9 spec table mandates fatal classification).
    if isinstance(exc, PydanticValidationError):
        return "fatal"
    if isinstance(exc, (TypeError, ValueError, AttributeError, KeyError)):
        return "fatal"

    # Unknown / unexpected — be conservative: try the next provider,
    # but the router caps at the chain length so an infinite chain of
    # bugs cannot happen.
    return "retriable_with_fallback"
