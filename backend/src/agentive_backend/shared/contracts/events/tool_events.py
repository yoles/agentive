"""Tool Hub MCP lifecycle events — Epic M5 (Stories 2.5 + 2.6).

Naming convention follows Story 1.7 pattern ``{module}.{object}.{verb}``.
Events shipped :

* ``tool_hub.tool_server.connected`` — Story 2.5, emitted after a successful
  MCP discovery (server registered + N tools discovered).
* ``tool_hub.tool.discovered`` — Story 2.5, emitted per tool, one row per
  discovered tool in the same transaction as the parent
  ``tool_server.connected``.
* ``tool_hub.tool.invoked`` — Story 2.6, emitted after a runtime tool call
  (success / timeout / error) with ``duration_ms`` + ``sandbox_backend``
  + ``args_redacted`` (P-03 secret-safety).
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ToolServerConnectedEvent(BaseModel):
    """Published after a new ``tool_servers`` row is durably committed
    along with its discovered tools (Story 2.5 AC1).

    Same audit-bypass story as the Story 2.1+2.2+2.4 events — lives in
    ``outbox_events`` until Story 9.1 wires :class:`AuditEventRepo`.
    """

    event_type: ClassVar[str] = "tool_hub.tool_server.connected"

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

    event_type: ClassVar[str] = "tool_hub.tool.discovered"

    tool_id: UUID
    server_id: UUID
    tool_name: str = Field(min_length=1)
    actor: str = Field(default="system", description="user_id or 'system' (D1 defer Story 9.1)")
    tenant_id: UUID | None = None


class ToolInvokedEvent(BaseModel):
    """Published after a runtime MCP tool call completes (Story 2.6 AC5).

    Emitted in a SINGLE transaction with the response timing (success /
    timeout / error) so we never log an invocation that never happened
    AND never lose the trace of one that did.

    ``args_redacted`` is the input arguments with secret-named keys
    (Authorization, token, password, api_key, secret, env, headers) masked
    via the P-03 redaction helper (Story 2.5). The RAW arguments are NEVER
    persisted to the audit trail.

    ``result`` is NOT included in the event payload — tool outputs may
    contain secrets or PII, and storing them would violate NFR6 (at-rest
    encryption deferred to Story 9.2). The caller (workflow_engine Story
    4.x or playground Story 2.7) is responsible for handling the result.
    """

    event_type: ClassVar[str] = "tool_hub.tool.invoked"

    tool_id: UUID
    server_id: UUID
    agent_template_id: UUID | None = None
    tool_name: str = Field(min_length=1)
    args_redacted: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int = Field(ge=0)
    status: Literal["success", "timeout", "error"]
    sandbox_backend: Literal["bwrap", "setrlimit"]
    actor: str = Field(default="system", description="user_id or 'system' (D1 defer Story 9.1)")
    tenant_id: UUID | None = None


__all__ = ["ToolDiscoveredEvent", "ToolInvokedEvent", "ToolServerConnectedEvent"]
