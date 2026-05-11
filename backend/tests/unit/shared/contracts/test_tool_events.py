"""Unit tests for ``shared/contracts/events/tool_events.py`` — Story 2.6 T5.3.

Covers the new :class:`ToolInvokedEvent` shape : event_type constant,
serialization round-trip, status Literal enforcement, args_redacted
default.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentive_backend.shared.contracts.events import (
    ToolDiscoveredEvent,
    ToolInvokedEvent,
    ToolServerConnectedEvent,
)


def test_tool_invoked_event_type_constant() -> None:
    """``event_type`` MUST be ``m5.tool.invoked`` (consumer routes match
    this string exactly — typo would break audit dispatch)."""
    assert ToolInvokedEvent.event_type == "m5.tool.invoked"


def test_tool_invoked_serializes_with_required_fields() -> None:
    """Build a complete event and assert ``model_dump`` round-trips."""
    event = ToolInvokedEvent(
        tool_id=uuid4(),
        server_id=uuid4(),
        agent_template_id=None,
        tool_name="echo",
        args_redacted={"text": "hi"},
        duration_ms=42,
        status="success",
        sandbox_backend="bwrap",
    )
    dumped = event.model_dump()
    assert dumped["tool_name"] == "echo"
    assert dumped["duration_ms"] == 42
    assert dumped["status"] == "success"
    assert dumped["sandbox_backend"] == "bwrap"
    assert dumped["actor"] == "system"  # D1 default
    assert dumped["tenant_id"] is None


def test_tool_invoked_rejects_invalid_status() -> None:
    """``status`` is a Pydantic Literal — invalid values fail validation."""
    with pytest.raises(ValidationError):
        ToolInvokedEvent(
            tool_id=uuid4(),
            server_id=uuid4(),
            tool_name="echo",
            duration_ms=0,
            status="completed",  # type: ignore[arg-type]  # NOT in Literal
            sandbox_backend="bwrap",
        )


def test_tool_invoked_rejects_invalid_sandbox_backend() -> None:
    """``sandbox_backend`` is a Pydantic Literal — invalid values fail."""
    with pytest.raises(ValidationError):
        ToolInvokedEvent(
            tool_id=uuid4(),
            server_id=uuid4(),
            tool_name="echo",
            duration_ms=0,
            status="success",
            sandbox_backend="docker",  # type: ignore[arg-type]  # NOT in Literal
        )


def test_tool_invoked_duration_ms_must_be_non_negative() -> None:
    """``duration_ms: int = Field(ge=0)`` rejects negative values."""
    with pytest.raises(ValidationError):
        ToolInvokedEvent(
            tool_id=uuid4(),
            server_id=uuid4(),
            tool_name="echo",
            duration_ms=-1,
            status="success",
            sandbox_backend="bwrap",
        )


def test_tool_events_barrel_exports_all_three() -> None:
    """The barrel ``events/__init__.py`` exports the 3 m5 events."""
    assert ToolServerConnectedEvent.event_type == "m5.tool_server.connected"
    assert ToolDiscoveredEvent.event_type == "m5.tool.discovered"
    assert ToolInvokedEvent.event_type == "m5.tool.invoked"
