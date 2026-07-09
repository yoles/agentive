"""Unit tests — playground schemas (Story 2.7 T2.4)."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentive_backend.features.playground.schemas import (
    RunPlaygroundRequest,
    RunPlaygroundResponse,
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
