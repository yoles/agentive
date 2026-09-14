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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

FinishReason = Literal["stop", "length", "tool_use", "content_filter", "error"]
#: ``"tool"`` (Story 5.0) porte le RÉSULTAT d'un outil dans le transcript,
#: renvoyé au modèle pour qu'il poursuive. Les adaptateurs le traduisent en
#: ``ToolMessage`` LangChain ; il n'est jamais produit par un appelant humain.
ChatRole = Literal["system", "user", "assistant", "tool"]


class ToolDefinition(BaseModel):
    """A tool offered to the model, provider-agnostic (Story 5.0 AC1).

    Deliberately NOT an Anthropic or OpenAI shape: each adapter translates
    this into its provider's format via LangChain's ``bind_tools``, so no
    caller — and no feature — ever handles a provider dialect. Mirrors the
    posture of every other type here.

    ``input_schema`` is the tool's JSON Schema, taken as-is from what the MCP
    server advertises (``discover_tools``' ``inputSchema``). It is not
    re-validated here: the MCP server validates arguments against it at call
    time, and a second, drifting copy of that validation is exactly the kind
    of duplicated rule this codebase rejects elsewhere.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(..., max_length=2048)
    input_schema: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    """One tool invocation REQUESTED by the model (Story 5.0 AC1).

    Normalized from LangChain's ``AIMessage.tool_calls``, which already
    reconciles Anthropic's ``tool_use`` blocks and OpenAI's ``tool_calls``.

    ``id`` is the provider's correlation handle: the result must be returned
    in a ``ChatMessage(role="tool", tool_call_id=id)`` or the model cannot
    match answer to question. It is opaque and never interpreted here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    """A single message in the conversation transcript."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: ChatRole
    content: str = Field(..., min_length=0)
    #: Set ONLY on ``role="tool"`` — the ``ToolCall.id`` this message answers.
    #: Optional so every pre-Story-5.0 caller keeps constructing messages
    #: unchanged; a ``"tool"`` message without it is refused below, because a
    #: result the model cannot correlate is worse than no result.
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def _tool_messages_carry_their_correlation_id(self) -> ChatMessage:
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("a ChatMessage(role='tool') must carry tool_call_id")
        if self.role != "tool" and self.tool_call_id is not None:
            raise ValueError("tool_call_id is only meaningful on role='tool'")
        return self


class LLMUsage(BaseModel):
    """Token + cost usage for one provider call.

    ``cost_estimate_usd`` is ``None`` when the model is absent from
    the provider's ``MODEL_PRICING`` snapshot — the metric
    ``LLM_COST_USD_TOTAL`` skips this call rather than reporting zero.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(..., ge=0)
    output_tokens: int = Field(..., ge=0)
    cost_estimate_usd: Decimal | None = None

    @field_serializer("cost_estimate_usd")
    def _serialize_decimal(self, v: Decimal | None) -> str | None:
        return str(v) if v is not None else None


class Completion(BaseModel):
    """Normalized LLM response — provider-agnostic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    model: str
    provider: str
    input_tokens: int = Field(..., ge=0)
    output_tokens: int = Field(..., ge=0)
    finish_reason: FinishReason
    latency_ms: float = Field(..., ge=0.0)
    provider_request_id: str | None = None
    cost_estimate_usd: Decimal | None = None
    #: Tool invocations the model asked for (Story 5.0 AC1). Empty tuple by
    #: default, so every caller predating the tool loop is unaffected — and so
    #: ``finish_reason == "tool_use"`` with an EMPTY tuple stays detectable as
    #: the anomaly it would be, rather than being indistinguishable from a
    #: normal stop.
    tool_calls: tuple[ToolCall, ...] = ()

    @field_serializer("cost_estimate_usd")
    def _serialize_decimal(self, v: Decimal | None) -> str | None:
        return str(v) if v is not None else None
