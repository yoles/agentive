"""MockProvider — FIFO replay correctness."""

from __future__ import annotations

from decimal import Decimal

import pytest

from agentive_backend.shared.llm.testing import MockProvider
from agentive_backend.shared.llm.types import ChatMessage, Completion


def _make_completion(text: str = "ok") -> Completion:
    return Completion(
        text=text,
        model="claude-haiku-4-5",
        provider="mock",
        input_tokens=1,
        output_tokens=2,
        finish_reason="stop",
        latency_ms=1.0,
        cost_estimate_usd=Decimal("0.0"),
    )


@pytest.mark.asyncio
async def test_returns_queued_completions_in_fifo_order() -> None:
    a = _make_completion("first")
    b = _make_completion("second")
    provider = MockProvider("mock_a", [a, b])
    msg = [ChatMessage(role="user", content="x")]

    out1 = await provider.complete(messages=msg, model="m", max_tokens=10)
    out2 = await provider.complete(messages=msg, model="m", max_tokens=10)

    assert out1.text == "first"
    assert out2.text == "second"


@pytest.mark.asyncio
async def test_raises_queued_exception() -> None:
    err = RuntimeError("boom")
    provider = MockProvider("mock_b", [err])
    with pytest.raises(RuntimeError, match="boom"):
        await provider.complete(
            messages=[ChatMessage(role="user", content="x")],
            model="m",
            max_tokens=10,
        )


@pytest.mark.asyncio
async def test_raises_value_error_when_exhausted() -> None:
    provider = MockProvider("mock_c", [])
    with pytest.raises(ValueError, match="exhausted"):
        await provider.complete(
            messages=[ChatMessage(role="user", content="x")],
            model="m",
            max_tokens=10,
        )


@pytest.mark.asyncio
async def test_records_call_kwargs_for_assertions() -> None:
    provider = MockProvider("mock_d", [_make_completion()])
    msg = [ChatMessage(role="user", content="hi")]
    await provider.complete(
        messages=msg,
        model="claude-sonnet-4-6",
        max_tokens=512,
        temperature=0.3,
        timeout_s=15.0,
    )
    [call] = provider.calls
    assert call["model"] == "claude-sonnet-4-6"
    assert call["max_tokens"] == 512
    assert call["temperature"] == 0.3
    assert call["timeout_s"] == 15.0
