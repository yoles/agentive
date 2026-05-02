"""OpenAIProvider — pure mapping logic + max_tokens kwarg resolution."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage

from agentive_backend.infra.llm.openai_adapter import (
    MODEL_PRICING,
    OpenAIProvider,
    _classify_openai_exception,
    _compute_cost,
    _resolve_max_tokens_kwarg,
)
from agentive_backend.shared.llm.exceptions import (
    LLMProviderAuthError,
    LLMProviderRateLimitError,
    LLMProviderTimeoutError,
    LLMProviderUnavailableError,
)
from agentive_backend.shared.llm.types import ChatMessage


@pytest.mark.parametrize(
    ("model", "expected_kwarg"),
    [
        ("gpt-4", "max_tokens"),
        ("gpt-4.1", "max_tokens"),
        ("gpt-3.5-turbo", "max_tokens"),
        ("gpt-5", "max_completion_tokens"),
        ("gpt-5-mini", "max_completion_tokens"),
        ("o3-mini", "max_completion_tokens"),
        ("o4-mini", "max_completion_tokens"),
        ("o1", "max_completion_tokens"),
    ],
)
def test_resolve_max_tokens_kwarg(model: str, expected_kwarg: str) -> None:
    assert _resolve_max_tokens_kwarg(model) == expected_kwarg


def test_compute_cost_known_model() -> None:
    cost = _compute_cost("gpt-5", input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == Decimal("40.000000")


def test_compute_cost_unknown_model_returns_none() -> None:
    assert _compute_cost("gpt-future", 100, 100) is None


def test_pricing_table_has_canonical_models() -> None:
    for model in ("gpt-5", "gpt-5-mini", "gpt-4.1", "o4-mini"):
        assert model in MODEL_PRICING


def test_to_completion_maps_finish_reason_stop() -> None:
    response = AIMessage(
        content="hi",
        response_metadata={"model_name": "gpt-5", "finish_reason": "stop"},
        usage_metadata={"input_tokens": 5, "output_tokens": 10, "total_tokens": 15},
    )
    completion = OpenAIProvider._to_completion(response, "gpt-5", 100.0)
    assert completion.finish_reason == "stop"
    assert completion.provider == "openai"


def test_to_completion_maps_finish_reason_length() -> None:
    response = AIMessage(
        content="hi",
        response_metadata={"model_name": "gpt-5", "finish_reason": "length"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    completion = OpenAIProvider._to_completion(response, "gpt-5", 1.0)
    assert completion.finish_reason == "length"


def test_to_completion_maps_tool_calls_to_tool_use() -> None:
    response = AIMessage(
        content="",
        response_metadata={"model_name": "gpt-5", "finish_reason": "tool_calls"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    completion = OpenAIProvider._to_completion(response, "gpt-5", 1.0)
    assert completion.finish_reason == "tool_use"


def test_to_completion_maps_content_filter() -> None:
    response = AIMessage(
        content="",
        response_metadata={"model_name": "gpt-5", "finish_reason": "content_filter"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    completion = OpenAIProvider._to_completion(response, "gpt-5", 1.0)
    assert completion.finish_reason == "content_filter"


def test_to_completion_legacy_token_usage() -> None:
    """Legacy `token_usage.prompt_tokens` mapping fallback."""
    response = AIMessage(
        content="hi",
        response_metadata={
            "model_name": "gpt-4",
            "finish_reason": "stop",
            "token_usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        },
    )
    completion = OpenAIProvider._to_completion(response, "gpt-4", 1.0)
    assert completion.input_tokens == 7
    assert completion.output_tokens == 3


def test_to_completion_unknown_model_yields_none_cost() -> None:
    response = AIMessage(
        content="hi",
        response_metadata={"model_name": "gpt-future", "finish_reason": "stop"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    completion = OpenAIProvider._to_completion(response, "gpt-future", 1.0)
    assert completion.cost_estimate_usd is None


def test_classify_openai_exception_timeout() -> None:
    import httpx

    err = _classify_openai_exception(httpx.TimeoutException("slow"))
    assert isinstance(err, LLMProviderTimeoutError)


def test_classify_openai_exception_401() -> None:
    import httpx

    err = _classify_openai_exception(
        httpx.HTTPStatusError(
            "auth",
            request=httpx.Request("POST", "http://localhost/"),
            response=httpx.Response(401),
        )
    )
    assert isinstance(err, LLMProviderAuthError)


def test_classify_openai_exception_429() -> None:
    import httpx

    err = _classify_openai_exception(
        httpx.HTTPStatusError(
            "rate",
            request=httpx.Request("POST", "http://localhost/"),
            response=httpx.Response(429),
        )
    )
    assert isinstance(err, LLMProviderRateLimitError)


def test_classify_openai_exception_5xx() -> None:
    import httpx

    err = _classify_openai_exception(
        httpx.HTTPStatusError(
            "503",
            request=httpx.Request("POST", "http://localhost/"),
            response=httpx.Response(503),
        )
    )
    assert isinstance(err, LLMProviderUnavailableError)


@pytest.mark.asyncio
async def test_complete_translates_openai_exceptions() -> None:
    import httpx

    provider = OpenAIProvider(api_key="sk-fake-test-only-not-real-1234567890abcdefghij")

    with patch("agentive_backend.infra.llm.openai_adapter.ChatOpenAI") as chat_cls:
        chat_cls.return_value.ainvoke = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "rate",
                request=httpx.Request("POST", "http://localhost/"),
                response=httpx.Response(429),
            )
        )
        with pytest.raises(LLMProviderRateLimitError):
            await provider.complete(
                messages=[ChatMessage(role="user", content="hi")],
                model="gpt-5",
                max_tokens=10,
            )


@pytest.mark.asyncio
async def test_complete_passes_max_completion_tokens_for_gpt5() -> None:
    """gpt-5* must NOT receive `max_tokens=` directly — must be in model_kwargs."""
    from langchain_core.messages import AIMessage as _AI

    provider = OpenAIProvider(api_key="sk-fake-key-only-1234567890abcdefghijklmn")
    fake_response = _AI(
        content="ok",
        response_metadata={"model_name": "gpt-5", "finish_reason": "stop"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )

    with patch("agentive_backend.infra.llm.openai_adapter.ChatOpenAI") as chat_cls:
        chat_cls.return_value.ainvoke = AsyncMock(return_value=fake_response)
        await provider.complete(
            messages=[ChatMessage(role="user", content="hi")],
            model="gpt-5",
            max_tokens=200,
        )
        # ChatOpenAI was instantiated with model_kwargs, not max_tokens.
        kwargs = chat_cls.call_args.kwargs
        assert "max_tokens" not in kwargs
        assert kwargs["model_kwargs"] == {"max_completion_tokens": 200}


@pytest.mark.asyncio
async def test_complete_passes_max_tokens_for_legacy_gpt() -> None:
    from langchain_core.messages import AIMessage as _AI

    provider = OpenAIProvider(api_key="sk-fake-key-only-1234567890abcdefghijklmn")
    fake_response = _AI(
        content="ok",
        response_metadata={"model_name": "gpt-4.1", "finish_reason": "stop"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )

    with patch("agentive_backend.infra.llm.openai_adapter.ChatOpenAI") as chat_cls:
        chat_cls.return_value.ainvoke = AsyncMock(return_value=fake_response)
        await provider.complete(
            messages=[ChatMessage(role="user", content="hi")],
            model="gpt-4.1",
            max_tokens=200,
        )
        kwargs = chat_cls.call_args.kwargs
        assert kwargs["max_tokens"] == 200
        assert "model_kwargs" not in kwargs
