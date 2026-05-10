"""Tool Hub MCP lifecycle events — Epic M5 (Story 2.5).

Naming convention follows Story 1.7 pattern ``{module}.{object}.{verb}``.
Events shipped Story 2.5 :

* ``m5.tool_server.connected`` — emitted after a successful MCP discovery
  (server registered + N tools discovered).
* ``m5.tool.discovered`` — emitted per tool, one row per discovered tool
  in the same transaction as the parent ``tool_server.connected``.

Defer Story 2.6 : ``m5.tool.executed`` / ``m5.tool.failed`` (when runtime
execution lands with the bwrap sandbox).
"""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, Field


class ToolServerConnectedEvent(BaseModel):
    """Published after a new ``tool_servers`` row is durably committed
    along with its discovered tools (Story 2.5 AC1).

    Same audit-bypass story as the Story 2.1+2.2+2.4 events — lives in
    ``outbox_events`` until Story 9.1 wires :class:`AuditEventRepo`.
    """

    event_type: ClassVar[str] = "m5.tool_server.connected"

    server_id: UUID
    name: str = Field(min_length=1, max_length=255)
    transport: str = Field(min_length=1)  # 'stdio' | 'sse' (CHECK constraint enforced DB-side)
    tools_count: int = Field(ge=0)
    actor: str = Field(default="system", description="user_id or 'system' (D1 defer Story 9.1)")
    tenant_id: UUID | None = None


class ToolDiscoveredEvent(BaseModel):
    """Published once per tool discovered from an MCP server (Story 2.5 AC1).

    Emitted in the SAME transaction as the parent
    :class:`ToolServerConnectedEvent` and the row INSERTs.
    """

    event_type: ClassVar[str] = "m5.tool.discovered"

    tool_id: UUID
    server_id: UUID
    tool_name: str = Field(min_length=1)
    actor: str = Field(default="system", description="user_id or 'system' (D1 defer Story 9.1)")
    tenant_id: UUID | None = None


__all__ = ["ToolDiscoveredEvent", "ToolServerConnectedEvent"]
