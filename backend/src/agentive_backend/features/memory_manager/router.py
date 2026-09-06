"""``/api/v1/memory/*`` — Memory Manager endpoints (Stories 3.1, 3.2, 3.4).

Four endpoints:

* ``POST /memory/chunks`` : write a chunk + its embedding (3.1 AC1).
* ``POST /memory/search`` : ANN search over a namespace (3.1 AC2). AC2
  originally specified a GET with ``q`` as a query param; moved to POST with
  ``q`` in the body so the search text never lands in a URL (and therefore
  never in access logs / reverse proxy / APM, code review Story 3.1, BS4).
* ``POST /memory/namespaces`` : create a namespace with a per-type default
  retention (3.2 AC1) and an optional temporal decay policy (3.4 AC1).
* ``GET /memory/namespaces`` : admin listing, all departments, with live
  chunk counts (3.2 AC3).

``POST /memory/chunks`` and ``POST /memory/search`` read the optional
``X-Acting-Department`` header (3.2 AC2) — see
``MemoryManagerService._check_department_access`` for what this does and
does not guarantee. ``POST``/``GET /memory/namespaces`` ignore it (global
administration actions, not subject to department scoping).

All endpoints sit behind ``AuthTokenMiddleware`` (Story 1.7). The global
``AgentiveError`` handler converts domain errors to RFC 7807.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status

from agentive_backend.features.memory_manager.domain.value_objects import (
    DecayPolicy,
    RetentionPolicy,
)
from agentive_backend.features.memory_manager.schemas import (
    CreateMemoryChunkRequest,
    CreateNamespaceRequest,
    DecayPolicyOverride,
    MemoryChunkCreateView,
    MemorySearchResultView,
    NamespaceCreateView,
    NamespaceListItemView,
    NamespaceUpdateView,
    SearchMemoryRequest,
    UpdateNamespaceRequest,
)
from agentive_backend.features.memory_manager.service import MemoryManagerService
from agentive_backend.shared.exceptions import DependencyError, ValidationError
from agentive_backend.shared.repositories import ChunkEmbeddingRepo, MemoryChunkRepo, NamespaceRepo

router = APIRouter(tags=["memory"])

# Story 3.2 AC2 — self-declared, non-authenticating scope header. See
# Dev Notes § Département in the story file for why this is not a real
# security boundary at MVP (single shared static token, no per-user
# session — Growth RBAC, Sprint 4, will replace this).
ACTING_DEPARTMENT_HEADER = "X-Acting-Department"

# Mirrors `CreateNamespaceRequest.department`'s cap (schemas.py) — this
# header is exactly as client-controlled as that body field, and flows
# into `outbox_events` payloads and log lines the same way (code review
# Story 3.2, P3).
ACTING_DEPARTMENT_MAX_LENGTH = 100


def _read_acting_department(request: Request) -> str | None:
    """Read ``X-Acting-Department``, treating an empty header as absent.

    A header present but empty (``X-Acting-Department:``) reads as ``""``,
    not ``None`` — left unhandled, that used to fail
    ``_check_department_access``'s equality check and produce a spurious
    403 plus a persisted audit event (code review Story 3.2, P2).
    """
    value = request.headers.get(ACTING_DEPARTMENT_HEADER)
    if not value:
        return None
    if len(value) > ACTING_DEPARTMENT_MAX_LENGTH:
        raise ValidationError(
            detail=(
                f"{ACTING_DEPARTMENT_HEADER} must not exceed "
                f"{ACTING_DEPARTMENT_MAX_LENGTH} characters"
            ),
        )
    return value


def _build_service(request: Request) -> MemoryManagerService:
    """Wire the MemoryManagerService from ``app.state``."""
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise DependencyError(
            detail="Memory manager not initialised — check lifespan startup logs.",
            context={"missing": ["session_factory"]},
        )
    embedder = getattr(request.app.state, "embedder", None)
    if embedder is None:
        raise DependencyError(
            detail="Memory manager not initialised — check lifespan startup logs.",
            context={"missing": ["embedder"]},
        )
    return MemoryManagerService(
        memory_chunk_repo=MemoryChunkRepo(session_factory=session_factory),
        chunk_embedding_repo=ChunkEmbeddingRepo(session_factory=session_factory),
        namespace_repo=NamespaceRepo(session_factory=session_factory),
        embedder=embedder,
    )


@router.post(
    "/memory/chunks",
    response_model=MemoryChunkCreateView,
    status_code=status.HTTP_201_CREATED,
    summary="Write a memory chunk + its embedding (Story 3.1)",
)
async def create_memory_chunk(
    request: Request,
    body: CreateMemoryChunkRequest,
) -> MemoryChunkCreateView:
    """201 on success.

    Errors:
    * 404 : ``namespace`` does not exist.
    * 403 : the ``X-Acting-Department`` header (if present) differs from the
      namespace's department (Story 3.2 AC2).
    * 422 : Pydantic body validation, or a malformed namespace
      ``retention_policy`` the domain layer refuses.
    * 503 : memory manager not initialised, or the embedder returned no
      vector.

    Embedding-provider failures are relayed with the provider's own status
    from :mod:`agentive_backend.shared.llm.exceptions`, NOT collapsed into
    503: 400 (bad request), 401 (provider auth), 429 (provider rate limit),
    504 (provider timeout), 500 (unclassified). A 401 here means the
    *server's* OpenAI credential failed, never the caller's bearer token
    (code review Story 3.1, P9).
    """
    service = _build_service(request)
    return await service.create_chunk(
        namespace_name=body.namespace,
        content=body.content,
        ttl_seconds=body.ttl,
        tenant_id=None,  # Sprint 1 anti-scope — single-tenant MVP.
        acting_department=_read_acting_department(request),
    )


@router.post(
    "/memory/search",
    response_model=list[MemorySearchResultView],
    summary="ANN vector search over a namespace's chunks (Story 3.1)",
)
async def search_memory(
    request: Request,
    body: SearchMemoryRequest,
) -> list[MemorySearchResultView]:
    """200 on success — empty list ``[]`` if no chunk matches.

    POST (not GET) on purpose : ``q`` in the body never reaches the URL, so
    it never reaches access logs / reverse proxy / APM either (code review
    Story 3.1, BS4).

    ``include_archived: true`` (Story 3.3 AC2) lifts the ``archived_at``
    AND ``expires_at`` filters for audit recovery; each result then carries
    its own ``archived_at`` so the caller can tell a live hit from one
    surfaced only because of this flag.

    Results are ranked by ``score``, which since Story 3.4 AC2 is the
    FINAL score ``clamp(similarity, 0, 1) x decay_factor(age)``. Its two
    terms come back as ``similarity`` and ``decay_factor`` so a caller can
    tell an aged-down result from a genuinely poor match. On a namespace
    with no ``decay_policy`` — every namespace created before Story 3.4 —
    ``decay_factor`` is ``1.0`` and ``score`` equals ``similarity``,
    exactly as before. ``rerank: false`` opts out and ranks on raw
    similarity alone.

    Errors:
    * 404 : ``namespace`` does not exist.
    * 403 : the ``X-Acting-Department`` header (if present) differs from the
      namespace's department (Story 3.2 AC2).
    * 422 : ``top_k`` out of ``[1, 50]`` bounds, or a blank/unstorable ``q``.
    * 503 : memory manager not initialised, or the embedder returned no
      vector.

    Same provider-status relay caveat as :func:`create_memory_chunk`.
    """
    service = _build_service(request)
    return await service.search(
        namespace_name=body.namespace,
        query=body.q,
        top_k=body.top_k,
        tenant_id=None,  # Sprint 1 anti-scope — single-tenant MVP.
        acting_department=_read_acting_department(request),
        include_archived=body.include_archived,
        rerank=body.rerank,
    )


def _to_decay_policy(override: DecayPolicyOverride | None) -> DecayPolicy | None:
    """Map the HTTP override onto the domain value object.

    Shared by the create and update routes so the two cannot drift on which
    parameters they forward — a parameter silently dropped on one path only
    is the exact failure mode this story rejects everywhere else.
    """
    if override is None:
        return None
    return DecayPolicy(
        function=override.function,
        half_life_seconds=override.half_life_seconds,
        horizon_seconds=override.horizon_seconds,
        threshold_seconds=override.threshold_seconds,
        factor=override.factor,
    )


@router.post(
    "/memory/namespaces",
    response_model=NamespaceCreateView,
    status_code=status.HTTP_201_CREATED,
    summary="Create a namespace with a per-type default retention (Story 3.2 AC1)",
)
async def create_namespace(
    request: Request,
    body: CreateNamespaceRequest,
) -> NamespaceCreateView:
    """201 on success. Not subject to ``X-Acting-Department`` (global
    administration action).

    ``decay_policy`` (Story 3.4 AC1) is optional and has NO per-type
    default: omitting it means the namespace's search results are ranked on
    similarity alone, exactly as before 3.4. Its parameters are
    function-scoped and mutually exclusive — a parameter that does not
    belong to the declared ``function`` is a 422, never silently ignored.

    Errors:
    * 409 : ``name`` already exists.
    * 422 : Pydantic body validation (``type`` outside the 4-value enum,
      blank/unstorable ``name``, out-of-bounds ``retention_policy``,
      incoherent ``decay_policy`` function/parameter combination).
    """
    service = _build_service(request)
    return await service.create_namespace(
        name=body.name,
        ns_type=body.type,
        department=body.department,
        project=body.project,
        retention_policy_override=(
            None
            if body.retention_policy is None
            else RetentionPolicy(
                default_ttl_seconds=body.retention_policy.default_ttl_seconds,
                archive_after_seconds=body.retention_policy.archive_after_seconds,
            )
        ),
        decay_policy_override=_to_decay_policy(body.decay_policy),
        tenant_id=None,  # Sprint 1 anti-scope — single-tenant MVP.
    )


@router.patch(
    "/memory/namespaces/{namespace_name}",
    response_model=NamespaceUpdateView,
    summary="Replace a namespace's temporal decay policy (Story 3.4 AC1)",
)
async def update_namespace(
    request: Request,
    namespace_name: str,
    body: UpdateNamespaceRequest,
) -> NamespaceUpdateView:
    """200 on success. Not subject to ``X-Acting-Department`` (global
    administration action, same posture as creation).

    This is what makes decay reachable at all on namespaces created before
    Story 3.4: configuring it at creation time only left every existing
    namespace — i.e. every namespace — stuck on similarity-only ranking, with
    no path to opt in short of hand-editing the JSONB (code review BS1).

    ``decay_policy`` is the only mutable field, and sending ``null`` clears it
    back to "no decay". ``retention_policy`` is not mutable here: changing a
    TTL retroactively decides the fate of chunks already written, which is
    Story 3.3's concern.

    Errors:
    * 404 : no namespace carries that name.
    * 422 : incoherent ``decay_policy`` function/parameter combination.
    """
    service = _build_service(request)
    return await service.update_namespace_decay_policy(
        name=namespace_name,
        decay_policy_override=_to_decay_policy(body.decay_policy),
        tenant_id=None,  # Sprint 1 anti-scope — single-tenant MVP.
    )


@router.get(
    "/memory/namespaces",
    response_model=list[NamespaceListItemView],
    summary="List all namespaces with live chunk counts (Story 3.2 AC3)",
)
async def list_namespaces(request: Request) -> list[NamespaceListItemView]:
    """200 — admin listing, all departments (no ``X-Acting-Department``
    scoping — John, owner, needs to see everything to administer the
    memory system). Grouping by department/type is a frontend concern."""
    service = _build_service(request)
    return await service.list_namespaces(tenant_id=None)


__all__ = ["router"]
