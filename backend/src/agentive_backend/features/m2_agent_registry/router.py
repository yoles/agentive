"""``/api/v1/agents/*`` — Agent Registry endpoints (Story 2.1 + 2.2).

Five endpoints :

* ``GET /agents/archetypes`` — lean list (UX-DR17 ArchetypeSelector).
* ``GET /agents/archetypes/{id}`` — detail with prompt_base + contracts.
* ``POST /agents/templates`` — create a new ``agent_templates`` row from an
  archetype + name. Returns 201 with the freshly committed row.
* ``GET /agents/templates/{template_id}`` — Story 2.2 detail.
* ``PUT /agents/templates/{template_id}`` — Story 2.2 PATCH-like update with
  prompt versioning when ``system_prompt`` is included.

All endpoints sit behind ``AuthTokenMiddleware`` (Story 1.7). ``AgentiveError``
is raised for domain failures and converted to RFC 7807 by the global handler
in ``app.main``.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, status

from agentive_backend.features.m2_agent_registry.archetypes import ArchetypeDefinition
from agentive_backend.features.m2_agent_registry.schemas import (
    AgentInstanceDetailResponse,
    ArchetypeDetail,
    ArchetypeSummary,
    CreateTemplateRequest,
    CreateTemplateResponse,
    InstantiateTemplateRequest,
    InstantiateTemplateResponse,
    TemplateDetailResponse,
    UpdateTemplateRequest,
    UpdateTemplateResponse,
)
from agentive_backend.features.m2_agent_registry.service import AgentRegistryService
from agentive_backend.shared.exceptions import DependencyError
from agentive_backend.shared.repositories import (
    AgentInstanceRepo,
    AgentTemplateRepo,
    PromptRepo,
    WorkflowRunRepo,
)

router = APIRouter(tags=["agents"])


def _build_service(request: Request) -> AgentRegistryService:
    """Wire the service from ``app.state``.

    Story 2.1 P-08 — explicit guard for missing lifespan state. If the
    archetype registry or session factory are absent (e.g. lifespan was
    skipped, or a future regression made ``load_registry`` non-fatal),
    the request returns RFC 7807 503 instead of an opaque ``AttributeError``
    leading to a 500.
    """
    registry: dict[str, ArchetypeDefinition] | None = getattr(
        request.app.state, "archetype_registry", None
    )
    session_factory = getattr(request.app.state, "session_factory", None)

    if registry is None or session_factory is None:
        raise DependencyError(
            detail="Agent registry not initialised — check lifespan startup logs.",
            context={
                "missing": [
                    k
                    for k, v in (
                        ("archetype_registry", registry),
                        ("session_factory", session_factory),
                    )
                    if v is None
                ]
            },
        )

    template_repo = AgentTemplateRepo(session_factory=session_factory)
    prompt_repo = PromptRepo(session_factory=session_factory)
    instance_repo = AgentInstanceRepo(session_factory=session_factory)
    workflow_run_repo = WorkflowRunRepo(session_factory=session_factory)
    return AgentRegistryService(
        registry=registry,
        template_repo=template_repo,
        prompt_repo=prompt_repo,
        instance_repo=instance_repo,
        workflow_run_repo=workflow_run_repo,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Archetype endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.get(
    "/agents/archetypes",
    response_model=list[ArchetypeSummary],
    summary="List the 8 universal archetypes (lean summaries)",
)
async def list_archetypes(request: Request) -> list[ArchetypeSummary]:
    """Returns the 8 archetypes ordered as defined in the YAML registry."""
    service = _build_service(request)
    return service.list_archetypes()


@router.get(
    "/agents/archetypes/{archetype_id}",
    response_model=ArchetypeDetail,
    summary="Detail of one archetype (prompt_base + contracts)",
)
async def get_archetype(request: Request, archetype_id: str) -> ArchetypeDetail:
    """Detailed view used by the ArchetypeSelector preview pane.

    404 RFC 7807 if ``archetype_id`` is not one of the 8 known slugs.
    """
    service = _build_service(request)
    return service.get_archetype(archetype_id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Template endpoint
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.post(
    "/agents/templates",
    response_model=CreateTemplateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new agent-template from an archetype",
)
async def create_template(request: Request, body: CreateTemplateRequest) -> CreateTemplateResponse:
    """201 on success.

    Errors :
    * 422 — Pydantic body validation OR unknown archetype (RFC 7807).
    * 409 — ``(name, version=1, tenant_id)`` already exists.
    * 503 — lifespan state missing (registry / session factory).
    """
    service = _build_service(request)
    return await service.create_template(
        name=body.name,
        archetype_id=body.archetype,
        tenant_id=None,  # Story 2.1 anti-scope — single-tenant MVP.
    )


@router.get(
    "/agents/templates/{template_id}",
    response_model=TemplateDetailResponse,
    summary="Get one agent-template (Story 2.2 — feeds the edit page)",
)
async def get_template(request: Request, template_id: UUID) -> TemplateDetailResponse:
    """200 on success. ``template_id`` must be a valid UUID (FastAPI Path).

    Errors :
    * 404 — template not found (RFC 7807 ``/errors/not-found``).
    * 422 — ``template_id`` is not a UUID (FastAPI auto-validates ``UUID``).
    * 503 — lifespan state missing.
    """
    service = _build_service(request)
    return await service.get_template_by_id(template_id, tenant_id=None)


@router.put(
    "/agents/templates/{template_id}",
    response_model=UpdateTemplateResponse,
    summary="Update an agent-template (PATCH-like — Story 2.2)",
)
async def update_template(
    request: Request,
    template_id: UUID,
    body: UpdateTemplateRequest,
) -> UpdateTemplateResponse:
    """200 on success. PATCH-like semantics — see ``UpdateTemplateRequest``.

    A new ``prompts`` row (and a ``version`` bump on ``agent_templates``) is
    persisted only when ``system_prompt`` is included in the body.

    Errors :
    * 404 — template not found.
    * 422 — Pydantic body validation OR ``template_id`` not a UUID.
    * 503 — lifespan state missing.
    """
    service = _build_service(request)
    return await service.update_template(template_id, body, tenant_id=None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Agent instance endpoints (Story 2.4 — distinction template vs instance)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.post(
    "/agents/templates/{template_id}/instances",
    response_model=InstantiateTemplateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Instantiate an agent from a template (Story 2.4 — frozen snapshot)",
)
async def instantiate_template(
    request: Request,
    template_id: UUID,
    body: InstantiateTemplateRequest,
) -> InstantiateTemplateResponse:
    """201 on success.

    Creates a new ``agent_instances`` row with a frozen snapshot of the
    template config at this exact moment. Modifications to the template
    AFTER this call do NOT propagate to the instance (FR12, AC2).

    Errors :
    * 404 — template_id not found OR workflow_run_id (if provided) not found.
    * 422 — template_id not a UUID, or body Pydantic validation.
    * 503 — lifespan state missing.
    """
    service = _build_service(request)
    return await service.instantiate_from_template(
        template_id=template_id,
        workflow_run_id=body.workflow_run_id,
        tenant_id=None,  # Story 2.4 anti-scope — single-tenant MVP.
    )


@router.get(
    "/agents/instances/{instance_id}",
    response_model=AgentInstanceDetailResponse,
    summary="Get one agent-instance with its frozen snapshot (Story 2.4)",
)
async def get_instance(
    request: Request,
    instance_id: UUID,
) -> AgentInstanceDetailResponse:
    """200 on success.

    Errors :
    * 404 — instance not found.
    * 422 — instance_id not a UUID.
    * 503 — lifespan state missing.
    """
    service = _build_service(request)
    return await service.get_instance_by_id(instance_id, tenant_id=None)


@router.get(
    "/workflows/runs/{run_id}/instances",
    response_model=list[AgentInstanceDetailResponse],
    summary="List all agent-instances rattached to a workflow run (Story 2.4)",
)
async def list_instances_by_run(
    request: Request,
    run_id: UUID,
) -> list[AgentInstanceDetailResponse]:
    """200 on success — empty list ``[]`` if the run has no instances yet.

    Sprint 1 hosting — this endpoint lives in ``m2_agent_registry/router.py``
    as it owns the ``agent_instances`` table. Story 4.1 may move it to
    ``m3_workflow_engine/router.py`` when the workflow_engine becomes the
    owner of the ``workflow_run`` lifecycle (cf Story 2.4 §"Décisions
    intégrées" #14).

    Errors :
    * 404 — workflow_run not found (strict — NOT an empty list).
    * 422 — run_id not a UUID.
    * 503 — lifespan state missing.
    """
    service = _build_service(request)
    return await service.list_instances_by_workflow_run(run_id, tenant_id=None)


__all__ = ["router"]
