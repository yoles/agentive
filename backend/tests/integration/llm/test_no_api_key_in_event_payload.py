"""Fallback event payload must not leak API keys.

The router invokes the ``on_fallback`` callback with a
:class:`FallbackContext` whose ``error_type`` and the
``LLMAllProvidersFailedError.context`` both go through
``redact_secrets``. This test asserts the redaction holds even when the
upstream SDK exception text embeds the key.
"""

from __future__ import annotations

import pytest

from agentive_backend.shared.llm import (
    LLMAllProvidersFailedError,
    LLMProviderUnavailableError,
    LLMRouter,
)
from agentive_backend.shared.llm.testing import MockProvider
from agentive_backend.shared.llm.types import ChatMessage


@pytest.mark.asyncio
async def test_fallback_attempt_detail_is_redacted() -> None:
    fake_key = "sk-ant-leak-one-two-three-1234567890abcdefghij12345"
    error_with_key = LLMProviderUnavailableError(
        detail=f"401 from upstream — Authorization: Bearer {fake_key}"
    )

    primary = MockProvider("mock_a", [error_with_key])
    secondary = MockProvider("mock_b", [LLMProviderUnavailableError("503")])
    router = LLMRouter(
        providers={"mock_a": primary, "mock_b": secondary},
        default_chain=["mock_a", "mock_b"],
        model_fallback_map={"claude-haiku-4-5": {"mock_b": "claude-haiku-4-5"}},
    )

    with pytest.raises(LLMAllProvidersFailedError) as exc_info:
        await router.complete(
            messages=[ChatMessage(role="user", content="hi")],
            model="claude-haiku-4-5",
            max_tokens=10,
        )

    attempts = exc_info.value.context["attempts"]
    serialized = str(attempts)
    assert "sk-ant-leak-one-two" not in serialized
    assert "[REDACTED]" in serialized
