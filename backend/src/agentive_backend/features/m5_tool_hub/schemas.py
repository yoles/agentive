"""Pydantic v2 schemas — request/response models for ``/api/v1/tools/*``
(Story 2.5).

All request models use ``ConfigDict(extra="forbid")`` (anti prompt-
injection on free-shape JSONB fields). Response models use
``extra="ignore"`` (P-05 Story 2.4 CR — defensive against ORM drift).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# Whitelist Sprint 1 — extensible Story 2.6+ if a new transport lands.
Transport = Literal["stdio", "sse"]
ToolServerStatus = Literal["active", "inactive"]


class CreateToolServerRequest(BaseModel):
    """Body of ``POST /api/v1/tools/servers`` (Story 2.5 AC1).

    The ``connection_config`` JSONB shape depends on ``transport`` :
    - stdio : ``{"command": str, "args": list[str], "env"?: dict}``
    - sse   : ``{"url": str, "headers"?: dict[str, str]}``

    Sprint 1 = NO Pydantic validation of the inner shape (the MCP server
    connection params are validated downstream by the MCP SDK at discovery
    time ; mismatches surface as ``MCPDiscoveryTimeoutError`` or SDK
    exceptions which the service translates to ``DependencyError`` 503).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    transport: Transport
    connection_config: dict[str, Any]


class ToolView(BaseModel):
    """A single tool, used in list and detail responses."""

    model_config = ConfigDict(extra="ignore")

    tool_id: UUID
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None


class ToolServerView(BaseModel):
    """List item for ``GET /tools/servers``. Lean (no connection_config or
    tools array) to keep the list endpoint cheap."""

    model_config = ConfigDict(extra="ignore")

    server_id: UUID
    name: str
    transport: Transport
    status: ToolServerStatus
    tools_count: int = Field(ge=0)
    discovered_at: datetime


class ToolServerDetailView(BaseModel):
    """Detail response for ``GET /tools/servers/{id}`` AND ``POST /tools/
    servers`` (after a successful discovery — same shape).

    Includes ``connection_config`` (Sprint 1 = clear, defer Fernet to
    Story 9.2) and the full ``tools`` array.
    """

    model_config = ConfigDict(extra="ignore")

    server_id: UUID
    name: str
    transport: Transport
    status: ToolServerStatus
    connection_config: dict[str, Any]
    discovered_at: datetime
    tools: list[ToolView]


__all__ = [
    "CreateToolServerRequest",
    "ToolServerDetailView",
    "ToolServerStatus",
    "ToolServerView",
    "ToolView",
    "Transport",
]
