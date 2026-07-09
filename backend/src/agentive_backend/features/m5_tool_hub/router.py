"""``/api/v1/tools/*`` — Tool Hub endpoints (Story 2.5).

Three endpoints :

* ``POST /tools/servers`` — register a new MCP server + discover its
  tools atomically (1 server + N tools + 1+N audit events single-tx).
* ``GET /tools/servers`` — list all registered servers (lean view +
  tools count).
* ``GET /tools/servers/{server_id}`` — detail of one server (with tools).

All endpoints sit behind ``AuthTokenMiddleware`` (Story 1.7). The global
``AgentiveError`` handler converts domain errors to RFC 7807.
"""

from __future__ import annotations

import time
from uuid import UUID

from fastapi import APIRouter, Request, status

from agentive_backend.features.m5_tool_hub.schemas import (
    CreateToolServerRequest,
    InvokeToolRequest,
    InvokeToolResponse,
    ToolServerDetailView,
    ToolServerView,
)
from agentive_backend.features.m5_tool_hub.service import ToolHubService
from agentive_backend.infra.mcp.sandbox import detect_sandbox_backend
from agentive_backend.shared.config import settings
from agentive_backend.shared.exceptions import DependencyError, ForbiddenError
from agentive_backend.shared.repositories import AgentTemplateRepo, ToolRepo, ToolServerRepo

router = APIRouter(tags=["tools"])


def _build_service(request: Request) -> ToolHubService:
    """Wire the ToolHubService from ``app.state``.

    P-16 (Story 2.4 CR) — the 2 repos must share ``app.state.session_factory``
    so the atomicity P-02 invariant (template SELECT + tool INSERT + outbox
    publish in same transaction) holds. Assertion below verrouille le wiring.
    """
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise DependencyError(
            detail="Tool hub not initialised — check lifespan startup logs.",
            context={"missing": ["session_factory"]},
        )
    server_repo = ToolServerRepo(session_factory=session_factory)
    tool_repo = ToolRepo(session_factory=session_factory)
    template_repo = AgentTemplateRepo(session_factory=session_factory)
    assert (
        server_repo._session_factory is tool_repo._session_factory is template_repo._session_factory
    ), "ToolHubService wiring violation : repos must share session_factory."
    return ToolHubService(server_repo=server_repo, tool_repo=tool_repo, template_repo=template_repo)


@router.post(
    "/tools/servers",
    response_model=ToolServerDetailView,
    status_code=status.HTTP_201_CREATED,
    summary="Register an MCP server + discover its tools (Story 2.5)",
)
async def create_tool_server(
    request: Request,
    body: CreateToolServerRequest,
) -> ToolServerDetailView:
    """201 on success.

    Errors :
    * 403 — registration disabled by ``AGENTIVE_ALLOW_MCP_REGISTRATION=false``
            (default — P-23 admin-gate, RCE/SSRF surface until Story 2.6 sandbox).
    * 409 — a server with this ``name`` already exists.
    * 422 — Pydantic body validation (transport not in {stdio,sse}, etc.).
    * 503 — MCP discovery timeout (10s) OR connection_config invalid.
    """
    if not settings.mcp_allow_registration:
        raise ForbiddenError(
            detail=(
                "MCP server registration is disabled. Set "
                "AGENTIVE_ALLOW_MCP_REGISTRATION=true to enable. "
                "Note: this endpoint accepts arbitrary subprocess commands and SSE URLs "
                "without sandboxing — sandbox arrives in Story 2.6."
            ),
            context={"flag": "AGENTIVE_ALLOW_MCP_REGISTRATION"},
        )
    service = _build_service(request)
    return await service.connect_server(
        name=body.name,
        transport=body.transport,
        connection_config=body.connection_config,
        tenant_id=None,  # Story 2.5 anti-scope — single-tenant MVP.
    )


@router.get(
    "/tools/servers",
    response_model=list[ToolServerView],
    summary="List all registered MCP servers (Story 2.5)",
)
async def list_tool_servers(request: Request) -> list[ToolServerView]:
    """200 on success — empty list ``[]`` if no server registered yet."""
    service = _build_service(request)
    return await service.list_servers(tenant_id=None)


@router.get(
    "/tools/servers/{server_id}",
    response_model=ToolServerDetailView,
    summary="Get one server with its full tools list (Story 2.5)",
)
async def get_tool_server(
    request: Request,
    server_id: UUID,
) -> ToolServerDetailView:
    """200 on success.

    Errors :
    * 404 — server not found.
    * 422 — server_id not a UUID.
    """
    service = _build_service(request)
    return await service.get_server_detail(server_id, tenant_id=None)


@router.post(
    "/tools/servers/{server_id}/tools/{tool_id}/invoke",
    response_model=InvokeToolResponse,
    summary="Invoke an MCP tool inside the sandbox (Story 2.6)",
)
async def invoke_tool(
    request: Request,
    server_id: UUID,
    tool_id: UUID,
    body: InvokeToolRequest,
) -> InvokeToolResponse:
    """200 on success.

    Errors :
    * 403 — registration/execution disabled by
            ``AGENTIVE_ALLOW_MCP_REGISTRATION=false`` (P-23 Story 2.5 gate
            extended to runtime — same RCE/SSRF risk surface as registration).
    * 404 — server / tool not found, OR tool returned ``isError=True``.
    * 422 — body validation (timeout out of range, etc.).
    * 503 — sandbox subprocess crash OR ``timeout_seconds`` exceeded.

    Note Sprint 1 : NO allowlist check "is tool in agent_template's
    assigned_tools" — deferred to Story 4.x (workflow_engine, D60).
    """
    if not settings.mcp_allow_registration:
        raise ForbiddenError(
            detail=(
                "MCP tool execution is disabled. Set "
                "AGENTIVE_ALLOW_MCP_REGISTRATION=true to enable. "
                "Note: this endpoint runs the MCP server inside a sandbox "
                "(bwrap or setrlimit fallback) — the flag still gates execution "
                "because the registration surface and the execution surface share "
                "the same RCE/SSRF risk model until Story 2.6 sandbox is "
                "production-validated."
            ),
            context={"flag": "AGENTIVE_ALLOW_MCP_REGISTRATION"},
        )
    service = _build_service(request)
    # P-03 (CR 2026-05-11) — resolve sandbox_backend ONCE (caller's
    # app.state cache or auto-detect) and pass the same value through
    # service + response, so the HTTP response and the audit event agree
    # on the active sandbox mode.
    backend = getattr(request.app.state, "mcp_sandbox_backend", None)
    if backend is None:
        backend = detect_sandbox_backend()

    start = time.monotonic()
    result = await service.invoke_tool(
        server_id=server_id,
        tool_id=tool_id,
        arguments=body.arguments,
        agent_template_id=body.agent_template_id,
        timeout=body.timeout_seconds,
        sandbox_backend=backend,
        tenant_id=None,
    )
    duration_ms = int((time.monotonic() - start) * 1000)
    # P-14 (CR 2026-05-11) — increment AC3 counter post-success. (Error
    # paths bail out before this line ; the audit event records failure
    # status with sandbox_backend regardless.)
    counters = getattr(request.app.state, "mcp_sandbox_invocations", None)
    if isinstance(counters, dict):
        counters[backend] = counters.get(backend, 0) + 1
    return InvokeToolResponse(
        result=result,
        duration_ms=duration_ms,
        sandbox_backend=backend,
    )


__all__ = ["router"]
