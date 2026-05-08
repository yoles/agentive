"""Cross-tenant isolation — ``memory_chunks`` table.

Two tenants insert chunks under their own UUIDs. Each repo call bound to
tenant A must only see tenant A's chunks.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.repositories import MemoryChunkRepo, NamespaceRepo

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_memory_chunks_isolated_per_tenant(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ns_repo = NamespaceRepo(session_factory=app_session_factory)
    chunk_repo = MemoryChunkRepo(session_factory=app_session_factory)
    tenant_a, tenant_b = uuid4(), uuid4()

    ns_a = await ns_repo.create(name="ns-a", ns_type="metier", tenant_id=tenant_a)
    ns_b = await ns_repo.create(name="ns-b", ns_type="metier", tenant_id=tenant_b)

    await chunk_repo.create(namespace_id=ns_a.id, content="chunk for A", tenant_id=tenant_a)
    await chunk_repo.create(namespace_id=ns_b.id, content="chunk for B", tenant_id=tenant_b)

    # Tenant A bound: sees its own chunks only.
    a_chunks = await chunk_repo.list_by_namespace(ns_a.id, tenant_id=tenant_a)
    assert len(a_chunks) == 1
    assert a_chunks[0].content == "chunk for A"

    # Tenant B bound: sees its own chunks only.
    b_chunks = await chunk_repo.list_by_namespace(ns_b.id, tenant_id=tenant_b)
    assert len(b_chunks) == 1
    assert b_chunks[0].content == "chunk for B"

    # Tenant A querying tenant B's namespace: 0 visible (RLS filters).
    cross_chunks = await chunk_repo.list_by_namespace(ns_b.id, tenant_id=tenant_a)
    assert cross_chunks == []
