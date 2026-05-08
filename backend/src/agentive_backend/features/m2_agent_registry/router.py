"""``/api/v1/agents/*`` — Agent Registry endpoints (Story 2.1).

Three endpoints :

* ``GET /agents/archetypes`` — lean list (UX-DR17 ArchetypeSelector).
* ``GET /agents/archetypes/{id}`` — detail with prompt_base + contracts.
* ``POST /agents/templates`` — create a new ``agent_templates`` row from an
  archetype + name. Returns 201 with the freshly committed row.

All endpoints sit behind ``AuthTokenMiddleware`` (Story 1.7). ``AgentiveError``
is raised for domain failures and converted to RFC 7807 by the global handler
in ``app.main``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status

from agentive_backend.features.m2_agent_registry.archetypes import ArchetypeDefinition
from agentive_backend.features.m2_agent_registry.schemas import (
    ArchetypeDetail,
    ArchetypeSummary,
    CreateTemplateRequest,
    CreateTemplateResponse,
)
from agentive_backend.features.m2_agent_registry.service import AgentRegistryService
from agentive_backend.shared.exceptions import DependencyError
from agentive_backend.shared.repositories import AgentTemplateRepo

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
    return AgentRegistryService(registry=registry, template_repo=template_repo)


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


__all__ = ["router"]
