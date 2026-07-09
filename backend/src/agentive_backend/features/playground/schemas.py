"""Pydantic v2 schemas — request/response models for ``/api/v1/playground/*``
(Story 2.7, FR48).

All request models use ``ConfigDict(extra="forbid")`` (anti-typo +
anti-injection). Response models use ``extra="ignore"`` (P-05 Story 2.4
CR — defensive against ORM/result-dict drift).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class RunPlaygroundRequest(BaseModel):
    """Body of ``POST /api/v1/playground/agents/{template_id}/run``.

    - ``arguments`` is a flat dict ; variable names in the template's
      ``system_prompt`` (notation ``{key}``) are substituted via
      ``str.format_map`` (Sprint 1 ; Jinja2/Mustache deferred Story 4.x).
    - ``enabled_tool_ids=None`` → all assigned tools enabled (default).
      Explicit empty list ``[]`` → all assigned tools disabled.
    - ``timeout_seconds`` capped at 120s (longer runs belong in workflow
      engine Story 4.x).
    """

    model_config = ConfigDict(extra="forbid")

    arguments: dict[str, Any] = Field(default_factory=dict)
    enabled_tool_ids: list[UUID] | None = None
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=120.0)


class ToolInvocationLog(BaseModel):
    """A single tool call invoked during a Playground run.

    Sprint 1 — Playground does not actually invoke tools (the LLM doesn't
    receive tool definitions in tool_use formal format ; Story 4.x).
    The shape is wired in the response payload so the frontend
    ``OutputInspector`` tab can render an empty state today and full data
    in Sprint 4+.
    """

    model_config = ConfigDict(extra="ignore")

    tool_id: UUID
    tool_name: str
    server_id: UUID
    arguments_redacted: dict[str, Any] = Field(default_factory=dict)
    result_summary: str = ""
    duration_ms: int = Field(ge=0)
    status: Literal["success", "error", "timeout"]


class RunPlaygroundResponse(BaseModel):
    """Response of ``POST .../run`` — Story 2.7 AC3.

    Rich payload : the frontend ``OutputInspector`` renders multiple tabs
    from this body (Prompt, Raw Output, Parsed Output, Tool Invocations,
    Tokens & Cost). ``parsed_output`` is best-effort — if the LLM returns
    text that does not match the template's ``output_contract``, this
    field is ``None`` and the frontend shows a "parsing failed" badge.
    """

    model_config = ConfigDict(extra="ignore")

    prompt_resolved: str
    raw_output: str
    parsed_output: dict[str, Any] | None
    tokens: dict[str, int]
    cost_estimate_usd: Decimal | None
    model_used: str
    provider_used: str
    tool_invocations: list[ToolInvocationLog]
    duration_ms_total: int = Field(ge=0)

    @field_serializer("cost_estimate_usd")
    def _serialize_decimal(self, v: Decimal | None) -> str | None:
        return str(v) if v is not None else None


__all__ = [
    "RunPlaygroundRequest",
    "RunPlaygroundResponse",
    "ToolInvocationLog",
]
