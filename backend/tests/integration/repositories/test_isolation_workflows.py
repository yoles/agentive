"""Cross-tenant isolation — ``workflows`` table."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.repositories import WorkflowRepo

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_workflows_isolated_per_tenant(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = WorkflowRepo(session_factory=app_session_factory)
    tenant_a, tenant_b = uuid4(), uuid4()

    wf_a = await repo.create(name="wf-a", dag={"nodes": []}, tenant_id=tenant_a)
    wf_b = await repo.create(name="wf-b", dag={"nodes": []}, tenant_id=tenant_b)

    a_active = await repo.list_active(tenant_id=tenant_a)
    a_ids = {w.id for w in a_active}
    # Strict cardinality: ``clean_repository_tables`` autouse leaves the
    # table empty at test start, so tenant_a sees exactly its own row.
    assert len(a_active) == 1, f"tenant_a should see exactly 1 workflow; got {len(a_active)}"
    assert wf_a.id in a_ids
    assert wf_b.id not in a_ids

    b_active = await repo.list_active(tenant_id=tenant_b)
    b_ids = {w.id for w in b_active}
    assert len(b_active) == 1, f"tenant_b should see exactly 1 workflow; got {len(b_active)}"
    assert wf_b.id in b_ids
    assert wf_a.id not in b_ids
