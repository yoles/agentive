"""Service layer — Memory Manager storage + vector search (Story 3.1).

The service sits between the FastAPI router and the repositories:

1. Resolves the namespace by name (404 if unknown — namespace creation is
   Story 3.2, out of scope here).
2. **AC1** — computes ``expires_at`` via the domain :class:`RetentionPolicy`
   (service triggers, domain decides — audit §4.5), computes the embedding
   FIRST (code review Story 3.1, IG1: a failed embed call must never leave
   a durable chunk row with no vector), then writes the chunk row and
   upserts the embedding, compensating with a hard delete of the chunk if
   the embedding write itself fails. The response's ``ttl_seconds`` is the
   *effective* TTL (override, else the namespace's ``default_ttl_seconds``),
   not the raw override alone, so it never contradicts ``expires_at`` (code
   review Story 3.1, BS3).
3. **AC2** — computes the query embedding and delegates the ANN search to
   :meth:`ChunkEmbeddingRepo.search_ann`.
4. **AC3** — isolation is satisfied by construction: every repo call goes
   through ``with_tenant(tenant_id)``, and Sprint 1 always passes
   ``tenant_id=None`` (mono-tenant MVP) so the ``tenant_isolation`` RLS
   policy matches all global rows. No department/project authorization
   check exists yet — that's Story 3.2's RLS département, which will plug
   in here once ``tenant_id``/department scoping is real.

Only the cloud backend (``text-embedding-3-small``) is wired — no
multi-provider routing (Story 3.6 Embedding Router hybride owns that).
"""

from __future__ import annotations

import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from agentive_backend.features.memory_manager.domain.value_objects import RetentionPolicy
from agentive_backend.features.memory_manager.schemas import (
    MemoryChunkCreateView,
    MemorySearchResultView,
)
from agentive_backend.shared.exceptions import DependencyError, InternalError
from agentive_backend.shared.logging import get_logger

if TYPE_CHECKING:
    from agentive_backend.shared.llm.embedder import Embedder
    from agentive_backend.shared.repositories import (
        ChunkEmbeddingRepo,
        MemoryChunkRepo,
        NamespaceRepo,
    )

_log = get_logger(__name__)

# Hardcoded Sprint 1 (T1.6) — the only model with a migrated partial HNSW
# index that's actually usable today (`chunk_embeddings_openai_hnsw`).
# `namespace.embedding_backend` is read (warning if != "cloud") but not
# branched — Story 3.6 (Embedding Router) wires that dimension.
EMBEDDING_MODEL = "text-embedding-3-small"


def _warn_if_backend_not_cloud(namespace_name: str, embedding_backend: str) -> None:
    """T1.6 — warn (never block) when a namespace asks for a backend we do
    not wire yet. Applies to reads as much as writes: search forces the same
    cloud model, and comparing a cloud query vector against embeddings
    produced by another backend is exactly as wrong (code review Story 3.1,
    P10: this used to fire only on the write path).
    """
    if embedding_backend != "cloud":
        _log.warning(
            "memory_manager.embedding_backend_not_cloud",
            namespace=namespace_name,
            embedding_backend=embedding_backend,
        )


def _first_vector(vectors: list[list[float]], *, namespace_name: str) -> list[float]:
    """Return ``vectors[0]``, or raise 503 rather than ``IndexError``.

    The :class:`Embedder` protocol documents ordering but never cardinality,
    so an adapter returning ``[]`` used to surface as an unclassified 500
    (code review Story 3.1, P5).
    """
    if not vectors:
        raise DependencyError(
            detail="Embedding backend returned no vector for the input text.",
            context={"namespace": namespace_name, "model": EMBEDDING_MODEL},
        )
    return vectors[0]


class MemoryManagerService:
    """Orchestrate memory chunk storage + vector search (Story 3.1)."""

    def __init__(
        self,
        *,
        memory_chunk_repo: MemoryChunkRepo,
        chunk_embedding_repo: ChunkEmbeddingRepo,
        namespace_repo: NamespaceRepo,
        embedder: Embedder,
    ) -> None:
        self._memory_chunk_repo = memory_chunk_repo
        self._chunk_embedding_repo = chunk_embedding_repo
        self._namespace_repo = namespace_repo
        self._embedder = embedder

    async def create_chunk(
        self,
        *,
        namespace_name: str,
        content: str,
        ttl_seconds: int | None,
        tenant_id: UUID | None = None,
    ) -> MemoryChunkCreateView:
        """Write a chunk + its embedding — Story 3.1 AC1.

        Raises
        ------
        NotFoundError
            ``namespace_name`` does not exist.
        """
        start = time.monotonic()
        namespace = await self._namespace_repo.require_by_name(namespace_name, tenant_id=tenant_id)
        _warn_if_backend_not_cloud(namespace_name, namespace.embedding_backend)

        # T3.2 — `now` captured once, before the insert, fed to both the
        # domain rule and the repo write. The row's real `created_at`
        # (server_default=func.now()) may differ by a few ms — negligible
        # at TTL scale (seconds/days).
        now = datetime.now(UTC)
        # `namespaces.retention_policy` is free-shape JSONB with no CHECK
        # constraint, so a hand-seeded row can carry a string TTL, a negative
        # value, or not be an object at all. Each used to escape as a raw 500
        # (code review Story 3.1, P4). Surface a proper RFC 7807 instead.
        try:
            policy = RetentionPolicy.from_mapping(namespace.retention_policy)
            expires_at = policy.expires_at(created_at=now, ttl_override_seconds=ttl_seconds)
            # BS3 : the response's `ttl_seconds` must agree with `expires_at`.
            # `ttl_seconds` alone used to echo back the raw client override
            # (often `None`) even when a namespace `default_ttl_seconds`
            # made the chunk expire anyway, so a caller reading
            # `ttl_seconds: null` wrongly concluded "never expires". This is
            # exactly the value `policy.expires_at` used internally to
            # compute `expires_at`, reused here rather than re-derived from
            # a datetime subtraction (which would drift by the same few-ms
            # clock skew noted above for `now` vs `chunk.created_at`).
            effective_ttl_seconds = (
                ttl_seconds if ttl_seconds is not None else policy.default_ttl_seconds
            )
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            _log.error(
                "memory_manager.retention_policy_invalid",
                namespace=namespace_name,
                error=str(exc),
            )
            raise InternalError(
                detail=f"Namespace {namespace_name!r} has a malformed retention_policy.",
                context={"namespace": namespace_name},
            ) from exc

        # Compute the embedding BEFORE writing anything (code review Story
        # 3.1, IG1). `embed()` is a network call to an external provider
        # that can rate-limit, time out, or 5xx; doing it AFTER the chunk
        # insert used to leave a durably committed row with no embedding —
        # invisible to `search_ann`'s INNER JOIN and unrecoverable (nothing
        # in this story re-embeds a chunk after the fact). Ordering it first
        # means that failure mode writes nothing at all.
        #
        # The two DB writes below are still two separate transactions (no
        # cross-repo unit-of-work exists in this codebase — `BaseRepo` never
        # exposes a session across repos by design), so a narrower window
        # remains between them. It is now two fast, same-process Postgres
        # calls instead of one Postgres call plus an external HTTP round
        # trip, so the residual risk is a DB-level failure (lost connection,
        # constraint violation), not provider flakiness — and it is
        # compensated below rather than left as an orphan.
        vectors = await self._embedder.embed([content], model=EMBEDDING_MODEL)
        embedding = _first_vector(vectors, namespace_name=namespace_name)
        source = getattr(self._embedder, "provider_name", None)

        chunk = await self._memory_chunk_repo.create(
            namespace_id=namespace.id,
            content=content,
            ttl_seconds=ttl_seconds,
            expires_at=expires_at,
            tenant_id=tenant_id,
        )
        try:
            await self._chunk_embedding_repo.upsert(
                chunk_id=chunk.id,
                model=EMBEDDING_MODEL,
                embedding=embedding,
                tenant_id=tenant_id,
                source=source,
            )
        except Exception:
            _log.error(
                "memory_manager.embedding_upsert_failed_compensating",
                namespace=namespace_name,
                chunk_id=str(chunk.id),
            )
            with suppress(Exception):
                await self._memory_chunk_repo.delete_by_id(chunk.id, tenant_id=tenant_id)
            raise

        _log.info(
            "memory_manager.chunk_created",
            namespace=namespace_name,
            chunk_id=str(chunk.id),
            duration_ms=int((time.monotonic() - start) * 1000),
        )

        return MemoryChunkCreateView(
            chunk_id=chunk.id,
            namespace=namespace_name,
            content=chunk.content,
            ttl_seconds=effective_ttl_seconds,
            expires_at=chunk.expires_at,
            embedding_model=EMBEDDING_MODEL,
            created_at=chunk.created_at,
        )

    async def search(
        self,
        *,
        namespace_name: str,
        query: str,
        top_k: int,
        tenant_id: UUID | None = None,
    ) -> list[MemorySearchResultView]:
        """ANN search over a namespace's chunks — Story 3.1 AC2.

        Raises
        ------
        NotFoundError
            ``namespace_name`` does not exist.
        """
        start = time.monotonic()
        namespace = await self._namespace_repo.require_by_name(namespace_name, tenant_id=tenant_id)
        _warn_if_backend_not_cloud(namespace_name, namespace.embedding_backend)

        vectors = await self._embedder.embed([query], model=EMBEDDING_MODEL)
        rows = await self._chunk_embedding_repo.search_ann(
            _first_vector(vectors, namespace_name=namespace_name),
            model=EMBEDDING_MODEL,
            namespace_id=namespace.id,
            top_k=top_k,
            tenant_id=tenant_id,
            now=datetime.now(UTC),
        )

        _log.info(
            "memory_manager.search_executed",
            namespace=namespace_name,
            top_k=top_k,
            result_count=len(rows),
            duration_ms=int((time.monotonic() - start) * 1000),
        )

        return [
            MemorySearchResultView(
                chunk_id=chunk.id,
                content=chunk.content,
                score=score,
                namespace=namespace_name,
                created_at=chunk.created_at,
            )
            for chunk, score in rows
        ]


__all__ = ["EMBEDDING_MODEL", "MemoryManagerService"]
