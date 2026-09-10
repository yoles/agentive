"""``/api/v1/workflows`` — Workflow Engine endpoints (Story 4.1).

One endpoint :

* ``POST /workflows`` — create a workflow from a client-submitted DAG
  (``{name, nodes, edges}``). Validates structural integrity, branching
  conditions, and surfaces a non-blocking Contrôleur/Producteur LLM-diversity
  warning (FR15, D84). No DAG-builder UI exists — the body is built by the
  caller (Postman, tests, or a future client, out of scope here).

Sits behind ``AuthTokenMiddleware`` (Story 1.7). ``AgentiveError`` is raised
for domain failures and converted to RFC 7807 by the global handler in
``app.main``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status

from agentive_backend.features.workflow_engine.schemas import (
    CreateWorkflowRequest,
    CreateWorkflowResponse,
)
from agentive_backend.features.workflow_engine.service import WorkflowService
from agentive_backend.shared.exceptions import DependencyError
from agentive_backend.shared.repositories import AgentTemplateRepo, WorkflowRepo

router = APIRouter(tags=["workflows"])


def _build_workflow_service(request: Request) -> WorkflowService:
    """Return a :class:`WorkflowService` wired from ``app.state``.

    No ``archetype_registry`` needed here (unlike ``agent_registry``) — the
    archetype is read from the ``agent_templates.archetype`` DB column, not
    the in-process YAML registry.
    """
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise DependencyError(
            detail="Workflow engine not initialised — check lifespan startup logs.",
            context={"missing": ["session_factory"]},
        )
    return WorkflowService(
        workflow_repo=WorkflowRepo(session_factory=session_factory),
        template_repo=AgentTemplateRepo(session_factory=session_factory),
    )


@router.post(
    "/workflows",
    response_model=CreateWorkflowResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a workflow from a client-submitted DAG (Story 4.1)",
)
async def create_workflow(request: Request, body: CreateWorkflowRequest) -> CreateWorkflowResponse:
    """201 on success — ``warnings`` may be non-empty (never blocking, AC4).

    Errors :
    * 422 — duplicate ``node_id``, dangling edge, unknown ``agent_template_id``,
      cycle, invalid branching-condition syntax, or a condition referencing a
      variable not exposed by the emitting node's ``output_contract.core``
      (RFC 7807).
    * 503 — lifespan state missing (session factory).
    """
    service = _build_workflow_service(request)
    return await service.create_workflow(
        name=body.name,
        nodes=body.nodes,
        edges=body.edges,
        tenant_id=None,  # Story 4.1 anti-scope — single-tenant MVP.
    )


__all__ = ["router"]
