"""Public API surface for :class:`MemoryChunk`. ALL DB access must go through this class."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

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

    async def create(
        self,
        *,
        namespace_id: UUID,
        content: str,
        metadata: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
        tenant_id: UUID | None = None,
    ) -> MemoryChunk:
        async with self.with_tenant(tenant_id) as session:
            chunk = MemoryChunk(
                namespace_id=namespace_id,
                content=content,
                metadata_=metadata if metadata is not None else {},
                ttl_seconds=ttl_seconds,
                tenant_id=tenant_id,
            )
            session.add(chunk)
            await session.flush()
            await session.refresh(chunk)
            return chunk
