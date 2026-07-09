"""Domain types for the LLM layer — frozen Pydantic v2 models.

These types are part of the public API consumed by every caller of
:class:`agentive_backend.shared.llm.LLMRouter`. They are intentionally
minimal and immutable so callers cannot mutate state shared across
provider invocations.

``Completion`` is JSON-serializable so it can travel inside event bus
payloads (e.g. ``workflow_engine.llm.completion_received`` — Story 4.6).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

FinishReason = Literal["stop", "length", "tool_use", "content_filter", "error"]
ChatRole = Literal["system", "user", "assistant"]


class ChatMessage(BaseModel):
    """A single message in the conversation transcript."""

    model_config = ConfigDict(frozen=True)

    role: ChatRole
    content: str = Field(..., min_length=0)


class LLMUsage(BaseModel):
    """Token + cost usage for one provider call.

    ``cost_estimate_usd`` is ``None`` when the model is absent from
    the provider's ``MODEL_PRICING`` snapshot — the metric
    ``LLM_COST_USD_TOTAL`` skips this call rather than reporting zero.
    """

    model_config = ConfigDict(frozen=True)

    input_tokens: int = Field(..., ge=0)
    output_tokens: int = Field(..., ge=0)
    cost_estimate_usd: Decimal | None = None

    @field_serializer("cost_estimate_usd")
    def _serialize_decimal(self, v: Decimal | None) -> str | None:
        return str(v) if v is not None else None


class Completion(BaseModel):
    """Normalized LLM response — provider-agnostic."""

    model_config = ConfigDict(frozen=True)

    text: str
    model: str
    provider: str
    input_tokens: int = Field(..., ge=0)
    output_tokens: int = Field(..., ge=0)
    finish_reason: FinishReason
    latency_ms: float = Field(..., ge=0.0)
    provider_request_id: str | None = None
    cost_estimate_usd: Decimal | None = None

    @field_serializer("cost_estimate_usd")
    def _serialize_decimal(self, v: Decimal | None) -> str | None:
        return str(v) if v is not None else None
