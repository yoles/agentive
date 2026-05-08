"""Domain types — frozen + Decimal serialization round-trip."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agentive_backend.shared.llm.types import ChatMessage, Completion, LLMUsage


def test_chat_message_is_frozen() -> None:
    msg = ChatMessage(role="user", content="hi")
    with pytest.raises(ValidationError):
        msg.content = "mutated"  # type: ignore[misc]


def test_chat_message_role_must_be_canonical() -> None:
    with pytest.raises(ValidationError):
        ChatMessage(role="invalid", content="hi")  # type: ignore[arg-type]


def test_completion_serializes_decimal_as_string() -> None:
    c = Completion(
        text="hello",
        model="claude-haiku-4-5",
        provider="anthropic",
        input_tokens=10,
        output_tokens=20,
        finish_reason="stop",
        latency_ms=123.4,
        cost_estimate_usd=Decimal("0.000125"),
    )
    payload = json.loads(c.model_dump_json())
    assert payload["cost_estimate_usd"] == "0.000125"
    assert payload["finish_reason"] == "stop"


def test_completion_decimal_none_when_pricing_unknown() -> None:
    c = Completion(
        text="hi",
        model="unknown-model",
        provider="anthropic",
        input_tokens=1,
        output_tokens=1,
        finish_reason="stop",
        latency_ms=10.0,
        cost_estimate_usd=None,
    )
    payload = json.loads(c.model_dump_json())
    assert payload["cost_estimate_usd"] is None


def test_completion_finish_reason_constrained() -> None:
    with pytest.raises(ValidationError):
        Completion(
            text="x",
            model="m",
            provider="p",
            input_tokens=0,
            output_tokens=0,
            finish_reason="invented",  # type: ignore[arg-type]
            latency_ms=1.0,
        )


def test_llm_usage_token_counts_must_be_non_negative() -> None:
    with pytest.raises(ValidationError):
        LLMUsage(input_tokens=-1, output_tokens=0)
    LLMUsage(input_tokens=0, output_tokens=0)  # zero is valid (mock provider)
