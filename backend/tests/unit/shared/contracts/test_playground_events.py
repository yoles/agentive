"""Unit tests — PlaygroundRunCompletedEvent (Story 2.7 T3.3)."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentive_backend.shared.contracts.events import PlaygroundRunCompletedEvent


def test_event_type_constant() -> None:
    """``event_type`` MUST be ``playground.run.completed`` (consumer
    routers match exact string ; typo would silently break audit dispatch)."""
    assert PlaygroundRunCompletedEvent.event_type == "playground.run.completed"


def test_serializes_decimal_cost_as_string() -> None:
    """``Decimal`` is JSON-serialized via the @field_serializer."""
    event = PlaygroundRunCompletedEvent(
        template_id=uuid4(),
        tools_activated=2,
        duration_ms_total=500,
        input_tokens=10,
        output_tokens=20,
        cost_estimate_usd=Decimal("0.000456"),
        model_used="claude-sonnet-4-6",
        provider_used="anthropic",
        status="success",
    )
    dumped = event.model_dump(mode="json")
    assert dumped["cost_estimate_usd"] == "0.000456"


def test_rejects_invalid_status() -> None:
    """``status`` Literal enforced — only success/llm_error/tool_error."""
    with pytest.raises(ValidationError):
        PlaygroundRunCompletedEvent(
            template_id=uuid4(),
            tools_activated=0,
            duration_ms_total=0,
            input_tokens=0,
            output_tokens=0,
            model_used="x",
            provider_used="y",
            status="completed",  # type: ignore[arg-type]
        )


def test_rejects_negative_metrics() -> None:
    """``tools_activated`` / ``duration_ms_total`` / ``input_tokens`` /
    ``output_tokens`` all have ``ge=0``."""
    with pytest.raises(ValidationError):
        PlaygroundRunCompletedEvent(
            template_id=uuid4(),
            tools_activated=-1,
            duration_ms_total=0,
            input_tokens=0,
            output_tokens=0,
            model_used="x",
            provider_used="y",
            status="success",
        )


def test_extra_forbid_blocks_unknown_payload_keys() -> None:
    """``extra='forbid'`` prevents drift — adding a key to the event
    schema must be explicit (versioned)."""
    with pytest.raises(ValidationError):
        PlaygroundRunCompletedEvent.model_validate(
            {
                "template_id": str(uuid4()),
                "tools_activated": 0,
                "duration_ms_total": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "model_used": "x",
                "provider_used": "y",
                "status": "success",
                "raw_output": "LEAK SHOULD BE REJECTED",  # not on event
            }
        )
