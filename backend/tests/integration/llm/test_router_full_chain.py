"""End-to-end router scenarios — happy path / fallback / total failure / fatal.

These exercise the full LLM stack in-process: ``LLMRouter`` →
``MockProvider`` → telemetry. The autouse ``_no_external_http`` fixture
guarantees no network call happens even if a regression silently
instantiates ``ChatAnthropic``.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from agentive_backend.shared.llm import (
    LLMAllProvidersFailedError,
    LLMProviderAuthError,
    LLMProviderUnavailableError,
    LLMRouter,
)
from agentive_backend.shared.llm.testing import MockProvider
from agentive_backend.shared.llm.types import ChatMessage, Completion


def _completion(text: str = "ok", provider: str = "mock_a") -> Completion:
    return Completion(
        text=text,
        model="claude-haiku-4-5",
        provider=provider,
        input_tokens=3,
        output_tokens=4,
        finish_reason="stop",
        latency_ms=12.0,
        cost_estimate_usd=Decimal("0.000005"),
    )


def _chain_router(
    primary_responses: list, secondary_responses: list, on_fallback=None
) -> tuple[LLMRouter, MockProvider, MockProvider]:
    primary = MockProvider("mock_a", primary_responses)
    secondary = MockProvider("mock_b", secondary_responses)
    router = LLMRouter(
        providers={"mock_a": primary, "mock_b": secondary},
        default_chain=["mock_a", "mock_b"],
        model_fallback_map={
            "claude-haiku-4-5": {"mock_b": "claude-haiku-4-5"},
        },
        on_fallback=on_fallback,
    )
    return router, primary, secondary


@pytest.mark.asyncio
async def test_happy_path_returns_completion_no_fallback() -> None:
    callback = AsyncMock()
    router, _primary, secondary = _chain_router(
        [_completion("primary")],
        [_completion("never-called")],
        on_fallback=callback,
    )

    out = await router.complete(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-haiku-4-5",
        max_tokens=10,
    )

    assert out.text == "primary"
    assert len(secondary.calls) == 0
    callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_path_publishes_event_via_callback() -> None:
    captured: list = []

    async def callback(ctx) -> None:
        captured.append(ctx)

    router, _primary, _secondary = _chain_router(
        [LLMProviderUnavailableError("primary 503")],
        [_completion("from-fallback")],
        on_fallback=callback,
    )

    out = await router.complete(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-haiku-4-5",
        max_tokens=10,
    )

    assert out.text == "from-fallback"
    assert len(captured) == 1
    assert captured[0].failed_provider == "mock_a"
    assert captured[0].next_provider == "mock_b"


@pytest.mark.asyncio
async def test_total_failure_path_aggregates_attempts() -> None:
    router, _primary, _secondary = _chain_router(
        [LLMProviderUnavailableError("primary 503")],
        [LLMProviderUnavailableError("secondary 503")],
    )

    with pytest.raises(LLMAllProvidersFailedError) as exc_info:
        await router.complete(
            messages=[ChatMessage(role="user", content="hi")],
            model="claude-haiku-4-5",
            max_tokens=10,
        )

    attempts = exc_info.value.context["attempts"]
    assert len(attempts) == 2
    assert {a["provider"] for a in attempts} == {"mock_a", "mock_b"}


@pytest.mark.asyncio
async def test_fatal_on_primary_short_circuits_chain() -> None:
    callback = AsyncMock()
    router, _primary, secondary = _chain_router(
        [LLMProviderAuthError("401")],
        [_completion("never-called")],
        on_fallback=callback,
    )

    with pytest.raises(LLMProviderAuthError):
        await router.complete(
            messages=[ChatMessage(role="user", content="hi")],
            model="claude-haiku-4-5",
            max_tokens=10,
        )

    assert len(secondary.calls) == 0
    callback.assert_not_awaited()
