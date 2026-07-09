"""Playground audit events — Epic M7 (Story 2.7).

Story 2.7 AC5 — single minimal audit event ``playground.run.completed``
published after each Playground run regardless of success/failure. The
payload contains METRICS only (template_id, tools_activated count,
duration, token usage, cost, model, status) — NO prompt_resolved, NO
raw_output, NO parsed_output, NO arguments. Secret-safety + volume-
bounded (one row per Playground run, payload < 500 bytes).

This is the ONLY ``event_bus.publish`` call from the Playground feature
— a discipline encoded in the import structure (cf
``features/playground/__init__.py`` docstring).
"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class PlaygroundRunCompletedEvent(BaseModel):
    """Published after a Playground run terminates (Story 2.7 AC5).

    ``status`` taxonomy :
    - ``success`` : LLM call succeeded + audit recorded.
    - ``llm_error`` : LLM provider failed (timeout, rate limit, etc.) ;
      service raised DependencyError 503 to caller.
    - ``tool_error`` : Sprint 1 unused (LLM does not invoke tools
      formally yet — Story 4.x will populate).

    Payload constraints (P-05 Story 2.6 alignment) :
    - NO ``prompt_resolved`` (may contain secrets injected via arguments).
    - NO ``raw_output`` / ``parsed_output`` (LLM may echo PII/secrets).
    - NO ``arguments`` (caller payload).
    - ``cost_estimate_usd: Decimal | None`` serialized as string.
    """

    event_type: ClassVar[str] = "playground.run.completed"

    template_id: UUID
    tools_activated: int = Field(ge=0)
    duration_ms_total: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_estimate_usd: Decimal | None = None
    model_used: str
    provider_used: str
    status: Literal["success", "llm_error", "tool_error"]
    actor: str = Field(default="system", description="user_id or 'system' (D1 defer Story 9.1)")
    tenant_id: UUID | None = None

    model_config = ConfigDict(extra="forbid")

    @field_serializer("cost_estimate_usd")
    def _serialize_decimal(self, v: Decimal | None) -> str | None:
        return str(v) if v is not None else None


__all__ = ["PlaygroundRunCompletedEvent"]
