"""AnthropicProvider — pure mapping logic (response → Completion)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage

from agentive_backend.infra.llm.anthropic_adapter import (
    MODEL_PRICING,
    AnthropicProvider,
    _compute_cost,
    _extract_text,
    _to_lc_messages,
)
from agentive_backend.shared.llm.exceptions import (
    LLMProviderAuthError,
    LLMProviderBadRequestError,
    LLMProviderRateLimitError,
    LLMProviderTimeoutError,
    LLMProviderUnavailableError,
)
from agentive_backend.shared.llm.types import ChatMessage


def test_to_lc_messages_extracts_single_system_prompt() -> None:
    messages = [
        ChatMessage(role="system", content="be helpful"),
        ChatMessage(role="user", content="hi"),
    ]
    converted, system = _to_lc_messages(messages)
    assert system == "be helpful"
    assert len(converted) == 1
    assert converted[0].content == "hi"


def test_to_lc_messages_concatenates_multiple_system_prompts() -> None:
    messages = [
        ChatMessage(role="system", content="rule one"),
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="system", content="rule two"),
    ]
    _, system = _to_lc_messages(messages)
    assert system == "rule one\n\nrule two"


def test_extract_text_handles_string_content() -> None:
    assert _extract_text("hello") == "hello"


def test_extract_text_handles_list_of_blocks() -> None:
    blocks = [
        {"type": "text", "text": "Hello "},
        {"type": "text", "text": "world"},
    ]
    assert _extract_text(blocks) == "Hello world"


def test_extract_text_skips_non_text_blocks() -> None:
    blocks = [
        {"type": "text", "text": "ok"},
        {"type": "tool_use", "id": "x"},
    ]
    assert _extract_text(blocks) == "ok"


def test_compute_cost_known_model() -> None:
    cost = _compute_cost("claude-haiku-4-5", input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == Decimal("4.800000")  # 0.80 + 4.00


def test_compute_cost_unknown_model_returns_none() -> None:
    assert _compute_cost("unknown-model", 1000, 1000) is None


def test_pricing_table_has_canonical_models() -> None:
    assert "claude-opus-4-7" in MODEL_PRICING
    assert "claude-sonnet-4-6" in MODEL_PRICING
    assert "claude-haiku-4-5" in MODEL_PRICING


def test_to_completion_maps_finish_reason_end_turn() -> None:
    response = AIMessage(
        content="hi",
        response_metadata={"model_name": "claude-haiku-4-5", "stop_reason": "end_turn"},
        usage_metadata={"input_tokens": 5, "output_tokens": 10, "total_tokens": 15},
    )
    completion = AnthropicProvider._to_completion(response, "claude-haiku-4-5", 100.0)
    assert completion.finish_reason == "stop"
    assert completion.text == "hi"
    assert completion.input_tokens == 5
    assert completion.output_tokens == 10
    assert completion.provider == "anthropic"
    assert completion.cost_estimate_usd is not None


def test_to_completion_maps_finish_reason_max_tokens() -> None:
    response = AIMessage(
        content="hi",
        response_metadata={"model_name": "claude-haiku-4-5", "stop_reason": "max_tokens"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    completion = AnthropicProvider._to_completion(response, "claude-haiku-4-5", 1.0)
    assert completion.finish_reason == "length"


def test_to_completion_maps_finish_reason_tool_use() -> None:
    response = AIMessage(
        content="",
        response_metadata={"model_name": "claude-sonnet-4-6", "stop_reason": "tool_use"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    completion = AnthropicProvider._to_completion(response, "claude-sonnet-4-6", 1.0)
    assert completion.finish_reason == "tool_use"


def test_to_completion_maps_unknown_stop_reason_to_error() -> None:
    response = AIMessage(
        content="",
        response_metadata={"model_name": "claude-sonnet-4-6", "stop_reason": "weird"},
        usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    )
    completion = AnthropicProvider._to_completion(response, "claude-sonnet-4-6", 1.0)
    assert completion.finish_reason == "error"


def test_to_completion_records_provider_request_id() -> None:
    response = AIMessage(
        content="hi",
        response_metadata={
            "model_name": "claude-haiku-4-5",
            "stop_reason": "end_turn",
            "id": "msg_01abcdef",
        },
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    completion = AnthropicProvider._to_completion(response, "claude-haiku-4-5", 1.0)
    assert completion.provider_request_id == "msg_01abcdef"


def test_to_completion_unknown_model_yields_none_cost() -> None:
    response = AIMessage(
        content="hi",
        response_metadata={"model_name": "claude-future-x-1", "stop_reason": "end_turn"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    completion = AnthropicProvider._to_completion(response, "claude-future-x-1", 1.0)
    assert completion.cost_estimate_usd is None


@pytest.mark.parametrize(
    ("exc_factory", "expected"),
    [
        (lambda: __import__("httpx").TimeoutException("slow"), LLMProviderTimeoutError),
        (lambda: __import__("httpx").ConnectError("nope"), LLMProviderUnavailableError),
        (
            lambda: __import__("httpx").HTTPStatusError(
                "401",
                request=__import__("httpx").Request("POST", "http://localhost/"),
                response=__import__("httpx").Response(401),
            ),
            LLMProviderAuthError,
        ),
        (
            lambda: __import__("httpx").HTTPStatusError(
                "429",
                request=__import__("httpx").Request("POST", "http://localhost/"),
                response=__import__("httpx").Response(429),
            ),
            LLMProviderRateLimitError,
        ),
        (
            lambda: __import__("httpx").HTTPStatusError(
                "503",
                request=__import__("httpx").Request("POST", "http://localhost/"),
                response=__import__("httpx").Response(503),
            ),
            LLMProviderUnavailableError,
        ),
        (
            lambda: __import__("httpx").HTTPStatusError(
                "400",
                request=__import__("httpx").Request("POST", "http://localhost/"),
                response=__import__("httpx").Response(400),
            ),
            LLMProviderBadRequestError,
        ),
    ],
)
def test_classify_anthropic_exception_maps_each_kind(exc_factory, expected) -> None:
    from agentive_backend.infra.llm.anthropic_adapter import (
        _classify_anthropic_exception,
    )

    domain_err = _classify_anthropic_exception(exc_factory())
    assert isinstance(domain_err, expected)


@pytest.mark.asyncio
async def test_complete_translates_anthropic_exceptions() -> None:
    """The adapter must convert SDK errors into the domain hierarchy."""
    import httpx

    provider = AnthropicProvider(api_key="sk-ant-fake-key-for-tests-do-not-trust-12345")

    with (
        patch.object(
            AnthropicProvider,
            "_to_completion",
            side_effect=AssertionError("should not reach mapping"),
        ),
        patch("agentive_backend.infra.llm.anthropic_adapter.ChatAnthropic") as chat_cls,
    ):
        chat_cls.return_value.ainvoke = AsyncMock(side_effect=httpx.TimeoutException("slow"))
        with pytest.raises(LLMProviderTimeoutError):
            await provider.complete(
                messages=[ChatMessage(role="user", content="hi")],
                model="claude-haiku-4-5",
                max_tokens=10,
            )
