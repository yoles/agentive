"""Multi-provider LLM abstraction.

Public API
----------
:class:`LLMRouter`
    Multi-provider chain with automatic fallback (NFR12).
:class:`LLMProvider`
    Protocol every adapter satisfies (Anthropic, OpenAI, …).
:class:`ChatMessage`, :class:`Completion`, :class:`LLMUsage`
    Frozen Pydantic v2 domain types — JSON-serializable for events.
:class:`AgentLLMConfig`
    Per-agent LLM configuration (consumed by ``M8 Agent Configurator``).

Exception hierarchy
-------------------
All errors subclass :class:`LLMError` (itself an
:class:`agentive_backend.shared.exceptions.AgentiveError`) so the FastAPI
RFC 7807 handler renders them consistently. ``classify_error`` decides
whether the router should fallback to the next provider.

Direct imports of ``langchain-anthropic`` / ``langchain-openai`` from
``features/*`` or ``api/*`` are forbidden (``import-linter`` Contract 5)
— callers go through :class:`LLMRouter` instead.
"""

from __future__ import annotations

from agentive_backend.shared.llm.config import AgentLLMConfig
from agentive_backend.shared.llm.error_classifier import ErrorClass, classify_error
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
from agentive_backend.shared.llm.interface import LLMProvider
from agentive_backend.shared.llm.router import (
    DEFAULT_MODEL_FALLBACK_MAP,
    FallbackCallback,
    FallbackContext,
    LLMRouter,
    RouterCall,
)
from agentive_backend.shared.llm.types import (
    ChatMessage,
    ChatRole,
    Completion,
    FinishReason,
    LLMUsage,
)

__all__ = [
    "DEFAULT_MODEL_FALLBACK_MAP",
    "AgentLLMConfig",
    "ChatMessage",
    "ChatRole",
    "Completion",
    "ErrorClass",
    "FallbackCallback",
    "FallbackContext",
    "FinishReason",
    "LLMAllProvidersFailedError",
    "LLMError",
    "LLMNoFallbackModelError",
    "LLMProvider",
    "LLMProviderAuthError",
    "LLMProviderBadRequestError",
    "LLMProviderRateLimitError",
    "LLMProviderTimeoutError",
    "LLMProviderUnavailableError",
    "LLMRouter",
    "LLMUsage",
    "RouterCall",
    "classify_error",
]
