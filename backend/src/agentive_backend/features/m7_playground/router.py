"""``/api/v1/playground/*`` — Agent Playground endpoints (Story 2.7).

Single endpoint :
* ``POST /playground/agents/{template_id}/run`` — run an agent in
  isolation (no agent_instance row, no memory_chunks writes, no events
  beyond ``m7.playground.run_completed``).

Gated by ``AGENTIVE_ALLOW_MCP_REGISTRATION`` (P-23 Story 2.5 alignment :
the Playground may dispatch tool calls via ``call_tool`` whose
RCE/SSRF surface is identical to registration's, so the same admin-gate
applies until Story 2.6 sandbox is production-validated on this host).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request

from agentive_backend.features.m7_playground.schemas import (
    RunPlaygroundRequest,
    RunPlaygroundResponse,
)
from agentive_backend.features.m7_playground.service import PlaygroundService
from agentive_backend.shared.config import settings
from agentive_backend.shared.exceptions import DependencyError, ForbiddenError
from agentive_backend.shared.repositories import (
    AgentTemplateRepo,
    AgentTemplateToolRepo,
    ToolRepo,
)

router = APIRouter(tags=["playground"])


def _build_service(request: Request) -> PlaygroundService:
    """Wire the PlaygroundService from ``app.state``.

    Pattern P-09 Story 2.4 CR — the 4 repos must share
    ``app.state.session_factory`` for atomicity. The LLMRouter is picked
    up from ``app.state.llm_router`` (built by ``app.lifespan``).
    """
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise DependencyError(
            detail="Playground not initialised — check lifespan startup logs.",
            context={"missing": ["session_factory"]},
        )
    llm_router = getattr(request.app.state, "llm_router", None)
    if llm_router is None:
        raise DependencyError(
            detail="LLM router not initialised — check lifespan startup logs.",
            context={"missing": ["llm_router"]},
        )

    template_repo = AgentTemplateRepo(session_factory=session_factory)
    tool_repo = ToolRepo(session_factory=session_factory)
    assignment_repo = AgentTemplateToolRepo(session_factory=session_factory)
    assert (
        template_repo._session_factory
        is tool_repo._session_factory
        is assignment_repo._session_factory
    ), "PlaygroundService wiring violation : repos must share session_factory."
    return PlaygroundService(
        template_repo=template_repo,
        tool_repo=tool_repo,
        assignment_repo=assignment_repo,
        llm_router=llm_router,
    )


@router.post(
    "/playground/agents/{template_id}/run",
    response_model=RunPlaygroundResponse,
    summary="Run an agent in the Playground (Story 2.7, FR48)",
)
async def run_playground(
    request: Request,
    template_id: UUID,
    body: RunPlaygroundRequest,
) -> RunPlaygroundResponse:
    """200 on success.

    Errors :
    * 403 — disabled by ``AGENTIVE_ALLOW_MCP_REGISTRATION=false`` (P-23
            alignment — Playground may call ``call_tool`` whose surface is
            identical to registration).
    * 404 — ``template_id`` not found.
    * 422 — body validation OR ``enabled_tool_ids`` references unassigned
            tools OR ``system_prompt`` references a variable missing from
            ``arguments``.
    * 503 — LLM provider failure (timeout, rate limit, classifier
            DependencyError per Story 1.6 error_classifier).
    """
    if not settings.mcp_allow_registration:
        raise ForbiddenError(
            detail=(
                "Playground is disabled. Set AGENTIVE_ALLOW_MCP_REGISTRATION=true "
                "to enable. The Playground may invoke MCP tools whose RCE/SSRF "
                "surface is identical to MCP server registration until Story 2.6 "
                "sandbox is production-validated on this host."
            ),
            context={"flag": "AGENTIVE_ALLOW_MCP_REGISTRATION"},
        )
    service = _build_service(request)
    return await service.run(
        template_id=template_id,
        arguments=body.arguments,
        enabled_tool_ids=body.enabled_tool_ids,
        timeout_seconds=body.timeout_seconds,
        tenant_id=None,  # Sprint 1 — multi-tenant Story 12.
    )


__all__ = ["router"]
