"""Public API surface for :class:`ChunkEmbedding`. ALL DB access must go through this class."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from agentive_backend.infra.db.models import ChunkEmbedding
from agentive_backend.shared.repositories.base import BaseRepo


class ChunkEmbeddingRepo(BaseRepo):
    """Public API surface for ChunkEmbedding. ALL DB access must go through this class.

    Primary key is composite ``(chunk_id, model)`` — a chunk can have
    multiple embeddings (one per model). Vector dimension varies per model
    and is enforced by partial HNSW indexes per model in the migration.
    """

    async def get(
        self,
        chunk_id: UUID,
        model: str,
        *,
        tenant_id: UUID | None = None,
    ) -> ChunkEmbedding | None:
        async with self.with_tenant(tenant_id) as session:
            return await session.get(ChunkEmbedding, (chunk_id, model))

    async def list_by_chunk(
        self, chunk_id: UUID, *, tenant_id: UUID | None = None
    ) -> list[ChunkEmbedding]:
        async with self.with_tenant(tenant_id) as session:
            stmt = select(ChunkEmbedding).where(ChunkEmbedding.chunk_id == chunk_id)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def upsert(
        self,
        *,
        chunk_id: UUID,
        model: str,
        embedding: Sequence[float],
        tenant_id: UUID | None = None,
    ) -> None:
        """Insert or update the embedding for ``(chunk_id, model)``.

        Uses ``ON CONFLICT (chunk_id, model) DO UPDATE`` so re-embedding a
        chunk replaces the old vector. ``tenant_id`` is set on insert and
        preserved on update (the WITH CHECK RLS clause prevents cross-tenant
        update, so we never overwrite another tenant's row).
        """
        async with self.with_tenant(tenant_id) as session:
            stmt = pg_insert(ChunkEmbedding).values(
                chunk_id=chunk_id,
                model=model,
                embedding=list(embedding),
                tenant_id=tenant_id,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[ChunkEmbedding.chunk_id, ChunkEmbedding.model],
                set_={"embedding": list(embedding)},
            )
            await session.execute(stmt)
