"""LLMRouter — fallback chain semantics + telemetry."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from prometheus_client import REGISTRY

from agentive_backend.shared.llm.exceptions import (
    LLMAllProvidersFailedError,
    LLMProviderAuthError,
    LLMProviderUnavailableError,
)
from agentive_backend.shared.llm.router import (
    DEFAULT_MODEL_FALLBACK_MAP,
    FallbackContext,
    LLMRouter,
)
from agentive_backend.shared.llm.testing import MockProvider
from agentive_backend.shared.llm.types import ChatMessage, Completion


def _completion(provider: str = "mock_a", text: str = "ok") -> Completion:
    return Completion(
        text=text,
        model="claude-haiku-4-5",
        provider=provider,
        input_tokens=2,
        output_tokens=3,
        finish_reason="stop",
        latency_ms=10.0,
        cost_estimate_usd=Decimal("0.000001"),
    )


def _messages() -> list[ChatMessage]:
    return [ChatMessage(role="user", content="hi")]


def _fallback_map() -> dict[str, dict[str, str]]:
    """Test-local map — keep us decoupled from the canonical Sprint 0 map."""
    return {
        "claude-haiku-4-5": {"mock_b": "claude-haiku-4-5"},
        "claude-sonnet-4-6": {"mock_b": "claude-sonnet-4-6"},
        "shared-model": {"mock_a": "shared-model", "mock_b": "shared-model"},
    }


@pytest.mark.asyncio
async def test_happy_path_primary_succeeds() -> None:
    primary = MockProvider("mock_a", [_completion("mock_a", "primary")])
    secondary = MockProvider("mock_b", [_completion("mock_b", "should-not-be-called")])
    router = LLMRouter(
        providers={"mock_a": primary, "mock_b": secondary},
        default_chain=["mock_a", "mock_b"],
        model_fallback_map=_fallback_map(),
    )

    result = await router.complete(messages=_messages(), model="claude-haiku-4-5", max_tokens=10)

    assert result.text == "primary"
    assert len(primary.calls) == 1
    assert len(secondary.calls) == 0


@pytest.mark.asyncio
async def test_fallback_on_5xx_uses_secondary() -> None:
    primary = MockProvider("mock_a", [LLMProviderUnavailableError("503 from mock_a")])
    secondary = MockProvider("mock_b", [_completion("mock_b", "fallback win")])
    captured: list[FallbackContext] = []

    async def callback(ctx: FallbackContext) -> None:
        captured.append(ctx)

    router = LLMRouter(
        providers={"mock_a": primary, "mock_b": secondary},
        default_chain=["mock_a", "mock_b"],
        model_fallback_map=_fallback_map(),
        on_fallback=callback,
    )

    result = await router.complete(messages=_messages(), model="claude-haiku-4-5", max_tokens=10)

    assert result.text == "fallback win"
    assert len(captured) == 1
    assert captured[0].failed_provider == "mock_a"
    assert captured[0].next_provider == "mock_b"
    assert captured[0].error_class == "retriable_with_fallback"


@pytest.mark.asyncio
async def test_fallback_on_timeout() -> None:
    from agentive_backend.shared.llm.exceptions import LLMProviderTimeoutError

    primary = MockProvider("mock_a", [LLMProviderTimeoutError("slow")])
    secondary = MockProvider("mock_b", [_completion()])
    router = LLMRouter(
        providers={"mock_a": primary, "mock_b": secondary},
        default_chain=["mock_a", "mock_b"],
        model_fallback_map=_fallback_map(),
    )

    out = await router.complete(messages=_messages(), model="claude-haiku-4-5", max_tokens=10)
    assert out is not None


@pytest.mark.asyncio
async def test_all_providers_fail_raises_aggregate() -> None:
    primary = MockProvider("mock_a", [LLMProviderUnavailableError("a")])
    secondary = MockProvider("mock_b", [LLMProviderUnavailableError("b")])
    router = LLMRouter(
        providers={"mock_a": primary, "mock_b": secondary},
        default_chain=["mock_a", "mock_b"],
        model_fallback_map=_fallback_map(),
    )

    with pytest.raises(LLMAllProvidersFailedError) as exc_info:
        await router.complete(messages=_messages(), model="claude-haiku-4-5", max_tokens=10)

    assert len(exc_info.value.context["attempts"]) == 2
    assert exc_info.value.context["attempts"][0]["provider"] == "mock_a"
    assert exc_info.value.context["attempts"][1]["provider"] == "mock_b"


@pytest.mark.asyncio
async def test_fatal_on_primary_no_fallback_attempted() -> None:
    primary = MockProvider("mock_a", [LLMProviderAuthError("401")])
    secondary = MockProvider("mock_b", [_completion()])
    router = LLMRouter(
        providers={"mock_a": primary, "mock_b": secondary},
        default_chain=["mock_a", "mock_b"],
        model_fallback_map=_fallback_map(),
    )

    with pytest.raises(LLMProviderAuthError):
        await router.complete(messages=_messages(), model="claude-haiku-4-5", max_tokens=10)

    # Secondary must NOT have been called.
    assert len(secondary.calls) == 0


@pytest.mark.asyncio
async def test_provider_chain_kwarg_overrides_default() -> None:
    primary = MockProvider("mock_a", [_completion("mock_a", "default-chain")])
    secondary = MockProvider("mock_b", [_completion("mock_b", "override")])
    router = LLMRouter(
        providers={"mock_a": primary, "mock_b": secondary},
        default_chain=["mock_a", "mock_b"],
        model_fallback_map=_fallback_map(),
    )

    out = await router.complete(
        messages=_messages(),
        model="claude-haiku-4-5",
        max_tokens=10,
        provider_chain=["mock_b"],
    )
    assert out.text == "override"
    assert len(primary.calls) == 0


@pytest.mark.asyncio
async def test_no_fallback_model_aggregates_into_all_providers_failed() -> None:
    """Mid-chain ``LLMNoFallbackModelError`` is appended to the attempts list
    rather than propagated raw, so callers see BOTH the original retriable
    failure AND the misconfig (review fix-batch P7).
    """
    from agentive_backend.shared.llm.exceptions import LLMAllProvidersFailedError

    primary = MockProvider("mock_a", [LLMProviderUnavailableError("a")])
    secondary = MockProvider("mock_b", [_completion()])
    router = LLMRouter(
        providers={"mock_a": primary, "mock_b": secondary},
        default_chain=["mock_a", "mock_b"],
        model_fallback_map={
            # Only the primary direction is mapped — the lookup for mock_b
            # has no entry for "claude-future".
            "claude-future": {},
        },
    )

    with pytest.raises(LLMAllProvidersFailedError) as exc_info:
        await router.complete(messages=_messages(), model="claude-future", max_tokens=10)

    attempts = exc_info.value.context["attempts"]
    assert len(attempts) == 2
    assert attempts[0]["provider"] == "mock_a"
    assert attempts[0]["error_type"] == "LLMProviderUnavailableError"
    assert attempts[1]["provider"] == "mock_b"
    assert attempts[1]["error_type"] == "LLMNoFallbackModelError"
    assert attempts[1]["error_class"] == "fatal"
    # The secondary provider's queue should NOT have been consumed —
    # the no-fallback-model abort happens before the second `complete()` call.
    assert len(secondary.calls) == 0


@pytest.mark.asyncio
async def test_unknown_provider_in_chain_raises() -> None:
    primary = MockProvider("mock_a", [_completion()])
    with pytest.raises(ValueError, match="unknown provider"):
        LLMRouter(
            providers={"mock_a": primary},
            default_chain=["mock_a", "ghost"],
        )


@pytest.mark.asyncio
async def test_in_flight_gauge_returns_to_zero_after_failure() -> None:
    primary = MockProvider("mock_a", [LLMProviderUnavailableError("a")])
    secondary = MockProvider("mock_b", [LLMProviderUnavailableError("b")])
    router = LLMRouter(
        providers={"mock_a": primary, "mock_b": secondary},
        default_chain=["mock_a", "mock_b"],
        model_fallback_map=_fallback_map(),
    )

    with pytest.raises(LLMAllProvidersFailedError):
        await router.complete(messages=_messages(), model="claude-haiku-4-5", max_tokens=10)

    # Inspect the gauge for the test labels.
    samples = {
        sample.labels.get("provider"): sample.value
        for metric in REGISTRY.collect()
        if metric.name == "agentive_llm_requests_in_flight"
        for sample in metric.samples
    }
    assert samples.get("mock_a", 0) == 0
    assert samples.get("mock_b", 0) == 0


@pytest.mark.asyncio
async def test_default_fallback_map_has_canonical_pairs() -> None:
    assert "claude-sonnet-4-6" in DEFAULT_MODEL_FALLBACK_MAP
    assert DEFAULT_MODEL_FALLBACK_MAP["claude-sonnet-4-6"]["openai"] == "gpt-5"
    assert DEFAULT_MODEL_FALLBACK_MAP["gpt-5"]["anthropic"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_callback_invoked_on_each_fallback() -> None:
    primary = MockProvider("mock_a", [LLMProviderUnavailableError("a")])
    secondary = MockProvider("mock_b", [_completion()])
    callback = AsyncMock()
    router = LLMRouter(
        providers={"mock_a": primary, "mock_b": secondary},
        default_chain=["mock_a", "mock_b"],
        model_fallback_map=_fallback_map(),
        on_fallback=callback,
    )

    await router.complete(messages=_messages(), model="claude-haiku-4-5", max_tokens=10)
    callback.assert_awaited_once()
    arg = callback.await_args.args[0]
    assert arg.failed_provider == "mock_a"
    assert arg.next_provider == "mock_b"


@pytest.mark.asyncio
async def test_raw_provider_call_dispatches_to_named_provider() -> None:
    primary = MockProvider("mock_a", [_completion("mock_a", "passthrough")])
    router = LLMRouter(
        providers={"mock_a": primary},
        default_chain=["mock_a"],
        model_fallback_map=_fallback_map(),
    )

    # raw_provider_call uses the same FIFO queue.
    result = await router.raw_provider_call("mock_a", anything="goes")
    assert isinstance(result, Completion)
    assert result.text == "passthrough"
