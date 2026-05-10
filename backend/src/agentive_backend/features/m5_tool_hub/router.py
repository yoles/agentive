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

from uuid import UUID

from fastapi import APIRouter, Request, status

from agentive_backend.features.m5_tool_hub.schemas import (
    CreateToolServerRequest,
    ToolServerDetailView,
    ToolServerView,
)
from agentive_backend.features.m5_tool_hub.service import ToolHubService
from agentive_backend.shared.config import settings
from agentive_backend.shared.exceptions import DependencyError, ForbiddenError
from agentive_backend.shared.repositories import ToolRepo, ToolServerRepo

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
    assert server_repo._session_factory is tool_repo._session_factory, (
        "ToolHubService wiring violation : repos must share session_factory."
    )
    return ToolHubService(server_repo=server_repo, tool_repo=tool_repo)


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


__all__ = ["router"]
