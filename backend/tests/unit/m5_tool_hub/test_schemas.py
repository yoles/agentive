"""Unit tests for ``m5_tool_hub/schemas.py`` — Story 2.6 T6.2 / P-19.

Covers the Story 2.6-introduced schemas :
* ``InvokeToolRequest`` — ``timeout_seconds`` bounds, ``arguments`` default,
  ``extra=forbid`` enforcement, ``agent_template_id`` optional.
* ``InvokeToolResponse`` — ``duration_ms`` ge=0, ``sandbox_backend`` Literal.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentive_backend.features.m5_tool_hub.schemas import (
    InvokeToolRequest,
    InvokeToolResponse,
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# InvokeToolRequest
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_invoke_tool_request_defaults_are_safe() -> None:
    """Empty body parses with default ``arguments={}`` + ``timeout_seconds=30``."""
    req = InvokeToolRequest()
    assert req.arguments == {}
    assert req.timeout_seconds == 30.0
    assert req.agent_template_id is None


def test_invoke_tool_request_rejects_extra_fields() -> None:
    """``extra='forbid'`` prevents unexpected fields (anti-typo + anti-injection)."""
    with pytest.raises(ValidationError) as exc_info:
        InvokeToolRequest.model_validate(
            {"arguments": {}, "unknown_field": "leak"},
        )
    assert "unknown_field" in str(exc_info.value).lower()


def test_invoke_tool_request_timeout_lower_bound() -> None:
    """``timeout_seconds`` must be ``> 0`` (Field gt=0)."""
    with pytest.raises(ValidationError):
        InvokeToolRequest.model_validate({"timeout_seconds": 0})
    with pytest.raises(ValidationError):
        InvokeToolRequest.model_validate({"timeout_seconds": -1.5})


def test_invoke_tool_request_timeout_upper_bound_600s() -> None:
    """``timeout_seconds`` cap at 600s (Field le=600.0). Defense against
    callers requesting hour-long timeouts that would tie up workers."""
    InvokeToolRequest.model_validate({"timeout_seconds": 600.0})  # OK
    with pytest.raises(ValidationError):
        InvokeToolRequest.model_validate({"timeout_seconds": 600.001})


def test_invoke_tool_request_accepts_agent_template_id_uuid() -> None:
    """``agent_template_id`` is optional UUID."""
    tid = uuid4()
    req = InvokeToolRequest.model_validate({"agent_template_id": str(tid)})
    assert req.agent_template_id == tid


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# InvokeToolResponse
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_invoke_tool_response_duration_ms_non_negative() -> None:
    """``duration_ms: int = Field(ge=0)`` rejects negative values."""
    InvokeToolResponse(result={}, duration_ms=0, sandbox_backend="bwrap")  # OK
    with pytest.raises(ValidationError):
        InvokeToolResponse(result={}, duration_ms=-1, sandbox_backend="bwrap")


def test_invoke_tool_response_sandbox_backend_literal_enforced() -> None:
    """``sandbox_backend`` is a Pydantic Literal — only 'bwrap' or 'setrlimit'."""
    with pytest.raises(ValidationError):
        InvokeToolResponse(
            result={},
            duration_ms=10,
            sandbox_backend="docker",  # type: ignore[arg-type]
        )
