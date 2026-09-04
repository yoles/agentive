"""``/api/v1/memory/*`` — Memory Manager endpoints (Story 3.1).

Two endpoints:

* ``POST /memory/chunks`` : write a chunk + its embedding (AC1).
* ``POST /memory/search`` : ANN search over a namespace (AC2). AC2
  originally specified a GET with ``q`` as a query param; moved to POST with
  ``q`` in the body so the search text never lands in a URL (and therefore
  never in access logs / reverse proxy / APM, code review Story 3.1, BS4).

All endpoints sit behind ``AuthTokenMiddleware`` (Story 1.7). The global
``AgentiveError`` handler converts domain errors to RFC 7807.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status

from agentive_backend.features.memory_manager.schemas import (
    CreateMemoryChunkRequest,
    MemoryChunkCreateView,
    MemorySearchResultView,
    SearchMemoryRequest,
)
from agentive_backend.features.memory_manager.service import MemoryManagerService
from agentive_backend.shared.exceptions import DependencyError
from agentive_backend.shared.repositories import ChunkEmbeddingRepo, MemoryChunkRepo, NamespaceRepo

router = APIRouter(tags=["memory"])


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
    * 404 : ``namespace`` does not exist (creation is Story 3.2).
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

    Errors:
    * 404 : ``namespace`` does not exist.
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
    )


__all__ = ["router"]
