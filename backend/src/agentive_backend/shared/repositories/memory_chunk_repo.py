"""Public API surface for :class:`MemoryChunk`. ALL DB access must go through this class."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select

from agentive_backend.infra.db.models import MemoryChunk
from agentive_backend.shared.repositories.base import BaseRepo


class MemoryChunkRepo(BaseRepo):
    """Public API surface for MemoryChunk. ALL DB access must go through this class.

    Vector search and reranking will live in
    ``features.memory_manager.service`` and call this repo's CRUD
    primitives.
    """

    async def get_by_id(
        self, chunk_id: UUID, *, tenant_id: UUID | None = None
    ) -> MemoryChunk | None:
        async with self.with_tenant(tenant_id) as session:
            return await session.get(MemoryChunk, chunk_id)

    async def list_by_namespace(
        self,
        namespace_id: UUID,
        *,
        tenant_id: UUID | None = None,
        limit: int = 100,
    ) -> list[MemoryChunk]:
        async with self.with_tenant(tenant_id) as session:
            stmt = select(MemoryChunk).where(MemoryChunk.namespace_id == namespace_id).limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def count_by_namespace_ids(
        self,
        namespace_ids: Sequence[UUID],
        *,
        tenant_id: UUID | None = None,
        now: datetime | None = None,
    ) -> dict[UUID, int]:
        """Live (non-archived, non-expired) chunk count per namespace — Story 3.2 AC3.

        ONE grouped query for N namespaces, not an N+1 loop (perf pattern
        scrutinized in Story 3.1 code review, BS1/BS2). A namespace with zero
        live chunks is simply absent from the returned dict — callers must
        ``.get(namespace_id, 0)``.

        Filters both ``archived_at`` AND ``expires_at`` — the same pair
        ``ChunkEmbeddingRepo.search_ann`` applies. AC3 states this count is
        "cohérent avec le filtre déjà appliqué par search_ann"; until Story
        3.3's archival job runs, an expired-but-not-yet-archived chunk is
        still searchable-looking here otherwise, over-counting what a
        search would actually return (code review Story 3.2, BS2).
        """
        if not namespace_ids:
            return {}
        now = now if now is not None else datetime.now(UTC)
        async with self.with_tenant(tenant_id) as session:
            stmt = (
                select(MemoryChunk.namespace_id, func.count())
                .where(
                    MemoryChunk.namespace_id.in_(namespace_ids),
                    MemoryChunk.archived_at.is_(None),
                    (MemoryChunk.expires_at.is_(None)) | (MemoryChunk.expires_at > now),
                )
                .group_by(MemoryChunk.namespace_id)
            )
            result = await session.execute(stmt)
            return dict(result.tuples().all())

    async def create(
        self,
        *,
        namespace_id: UUID,
        content: str,
        metadata: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
        expires_at: datetime | None = None,
        tenant_id: UUID | None = None,
    ) -> MemoryChunk:
        async with self.with_tenant(tenant_id) as session:
            chunk = MemoryChunk(
                namespace_id=namespace_id,
                content=content,
                metadata_=metadata if metadata is not None else {},
                ttl_seconds=ttl_seconds,
                expires_at=expires_at,
                tenant_id=tenant_id,
            )
            session.add(chunk)
            await session.flush()
            await session.refresh(chunk)
            return chunk

    async def delete_by_id(self, chunk_id: UUID, *, tenant_id: UUID | None = None) -> None:
        """Hard-delete a chunk row (cascades to ``chunk_embeddings``).

        Compensating action for the service layer (code review Story 3.1,
        IG1): if writing a chunk's embedding fails right after the chunk row
        itself was committed, the caller uses this to remove the now-orphan
        row rather than leave one with no vector — the `INNER JOIN` in
        ``search_ann`` can never surface it, so it would otherwise sit in
        the table forever, invisible and unrecoverable without this.

        Not the same thing as ``archived_at`` — that is a domain lifecycle
        state for chunks that DID succeed. This is "the write never really
        happened", so a hard delete (not a soft-delete flag) is correct.
        """
        async with self.with_tenant(tenant_id) as session:
            await session.execute(delete(MemoryChunk).where(MemoryChunk.id == chunk_id))
