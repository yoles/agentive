"""Pydantic v2 schemas — request/response models for ``/api/v1/playground/*``
(Story 2.7, FR48).

All request models use ``ConfigDict(extra="forbid")`` (anti-typo +
anti-injection). Response models use ``extra="ignore"`` (P-05 Story 2.4
CR — defensive against ORM/result-dict drift).
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

# P-32 (fix-batch 2026-08-31) — Sprint 1 Playground arguments are typed
# manually into a JSON textarea (no schema-driven form builder — D76 defer
# Story 4.x). Without a size cap, a pasted multi-MB payload becomes a
# multi-MB LLM system message on every run. 64 KiB comfortably covers any
# manually-authored test payload.
_MAX_ARGUMENTS_JSON_BYTES: Final = 65_536


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
    # P-31 (fix-batch 2026-08-31) — an unbounded list here means the
    # service's ``_count_activated_tools`` builds an unbounded diff set on
    # every request. 100 is far above what a Sprint 1 UI (manual checkbox
    # toggles) could realistically assign.
    enabled_tool_ids: list[UUID] | None = Field(default=None, max_length=100)
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=120.0)

    @field_validator("arguments")
    @classmethod
    def _bound_arguments_size(cls, v: dict[str, Any]) -> dict[str, Any]:
        """P-32 — reject an ``arguments`` payload whose JSON-serialized size
        exceeds :data:`_MAX_ARGUMENTS_JSON_BYTES`, instead of forwarding an
        unbounded blob straight into the LLM system message."""
        try:
            size = len(json.dumps(v, ensure_ascii=False).encode("utf-8"))
        except TypeError as exc:  # pragma: no cover — Any within JSON body is already JSON-safe
            raise ValueError("arguments must be JSON-serializable") from exc
        if size > _MAX_ARGUMENTS_JSON_BYTES:
            raise ValueError(
                f"arguments JSON payload is {size} bytes, exceeding the "
                f"{_MAX_ARGUMENTS_JSON_BYTES}-byte Sprint 1 limit"
            )
        return v


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


class TokenUsage(BaseModel):
    """P-22 (fix-batch 2026-08-31) — typed replacement for the untyped
    ``tokens: dict[str, int]`` field ; same wire shape (JSON object with
    the two keys below), so no frontend change is required."""

    model_config = ConfigDict(extra="ignore")

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


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
    tokens: TokenUsage
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
    "TokenUsage",
    "ToolInvocationLog",
]
