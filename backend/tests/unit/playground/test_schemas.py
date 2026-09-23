"""Unit tests — playground schemas (Story 2.7 T2.4)."""

from __future__ import annotations

import math
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentive_backend.features.playground.schemas import (
    RunPlaygroundRequest,
    RunPlaygroundResponse,
    TokenUsage,
    ToolInvocationLog,
)


def test_run_playground_request_defaults_are_safe() -> None:
    """Empty body parses with empty arguments + all tools enabled +
    timeout 30s."""
    req = RunPlaygroundRequest()
    assert req.arguments == {}
    assert req.enabled_tool_ids is None
    assert req.timeout_seconds == 30.0


def test_run_playground_request_rejects_extra_fields() -> None:
    """``extra='forbid'`` prevents unknown keys (anti-typo + anti-injection)."""
    with pytest.raises(ValidationError):
        RunPlaygroundRequest.model_validate(
            {"arguments": {}, "unknown_field": "leak"},
        )


def test_run_playground_request_timeout_bounds() -> None:
    """``timeout_seconds`` must be 0 < x ≤ 120 (Sprint 1 cap)."""
    with pytest.raises(ValidationError):
        RunPlaygroundRequest.model_validate({"timeout_seconds": 0})
    with pytest.raises(ValidationError):
        RunPlaygroundRequest.model_validate({"timeout_seconds": 121})
    RunPlaygroundRequest.model_validate({"timeout_seconds": 120.0})  # OK


def test_run_playground_response_serializes_decimal_cost() -> None:
    """``cost_estimate_usd: Decimal`` is JSON-serialized as a string."""
    resp = RunPlaygroundResponse(
        prompt_resolved="hi",
        raw_output="bye",
        parsed_output={"ok": True},
        tokens={"input_tokens": 10, "output_tokens": 20},
        cost_estimate_usd=Decimal("0.001234"),
        model_used="claude-sonnet-4-6",
        provider_used="anthropic",
        tool_invocations=[],
        duration_ms_total=123,
    )
    dumped = resp.model_dump(mode="json")
    assert dumped["cost_estimate_usd"] == "0.001234"


def test_tool_invocation_log_status_literal() -> None:
    """``status`` Literal — invalid values rejected."""
    with pytest.raises(ValidationError):
        ToolInvocationLog(
            tool_id=uuid4(),
            tool_name="x",
            server_id=uuid4(),
            duration_ms=0,
            status="completed",  # type: ignore[arg-type]
        )


def test_token_usage_rejects_negative_counts() -> None:
    """P-22 — ``TokenUsage`` replaces the untyped ``dict[str, int]``;
    negative counts (an impossible LLM response) are rejected at the type
    boundary rather than silently accepted."""
    with pytest.raises(ValidationError):
        TokenUsage(input_tokens=-1, output_tokens=0)


def test_enabled_tool_ids_max_length_enforced() -> None:
    """P-31 — ``enabled_tool_ids`` is capped at 100 entries."""
    RunPlaygroundRequest.model_validate({"enabled_tool_ids": [str(uuid4()) for _ in range(100)]})
    with pytest.raises(ValidationError):
        RunPlaygroundRequest.model_validate(
            {"enabled_tool_ids": [str(uuid4()) for _ in range(101)]}
        )


def test_arguments_oversized_payload_rejected() -> None:
    """P-32 — an ``arguments`` payload above the 64 KiB Sprint 1 cap is
    rejected at validation, not forwarded to the LLM."""
    huge = {"blob": "x" * 100_000}
    with pytest.raises(ValidationError):
        RunPlaygroundRequest.model_validate({"arguments": huge})
    # Comfortably under the cap — still accepted.
    RunPlaygroundRequest.model_validate({"arguments": {"topic": "small payload"}})


@pytest.mark.parametrize("bad_timeout", [math.nan, math.inf, -math.inf])
def test_timeout_seconds_rejects_non_finite_values(bad_timeout: float) -> None:
    """P-39 (reviewed, not patched) — NaN/Infinity are already rejected by
    the existing ``gt``/``le`` bounds (any comparison against NaN is False ;
    ``inf`` fails the ``le=120.0`` upper bound). This test locks that
    behavior in rather than adding a redundant explicit check."""
    with pytest.raises(ValidationError):
        RunPlaygroundRequest.model_validate({"timeout_seconds": bad_timeout})
