"""Happy-path CRUD smoke tests — one per concrete repo.

These tests verify that each repo's :meth:`with_tenant` plumbing works
against a real Postgres + RLS by performing a minimum create-then-read
cycle. Business-specific behaviors (vector search, prompt versioning,
etc.) ship in their respective epic stories.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.repositories import (
    AgentInstanceRepo,
    AgentTemplateRepo,
    ChunkEmbeddingRepo,
    FeatureFlagRepo,
    MemoryChunkRepo,
    NamespaceRepo,
    OutboxRepo,
    PromptRepo,
    UserRepo,
    WorkflowRepo,
    WorkflowRunRepo,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_user_repo_create_then_get_by_email(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = UserRepo(session_factory=app_session_factory)
    tenant = uuid4()
    created = await repo.create(
        email="alice@agentive.local",
        name="Alice",
        role="collaborator",
        tenant_id=tenant,
    )
    fetched = await repo.get_by_id(created.id, tenant_id=tenant)
    assert fetched is not None
    assert fetched.email == "alice@agentive.local"
    assert fetched.role == "collaborator"


@pytest.mark.asyncio
async def test_namespace_repo_create_then_list_by_type(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = NamespaceRepo(session_factory=app_session_factory)
    tenant = uuid4()
    created = await repo.create(
        name="ops-ns",
        ns_type="operationnelle",
        department="dev",
        tenant_id=tenant,
    )
    rows = await repo.list_by_type("operationnelle", tenant_id=tenant)
    assert any(r.id == created.id for r in rows)


@pytest.mark.asyncio
async def test_memory_chunk_repo_create_then_list_by_namespace(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ns_repo = NamespaceRepo(session_factory=app_session_factory)
    chunk_repo = MemoryChunkRepo(session_factory=app_session_factory)
    tenant = uuid4()
    ns = await ns_repo.create(name="chunks-ns", ns_type="metier", tenant_id=tenant)
    chunk = await chunk_repo.create(
        namespace_id=ns.id,
        content="hello world",
        metadata={"source": "test"},
        tenant_id=tenant,
    )
    rows = await chunk_repo.list_by_namespace(ns.id, tenant_id=tenant)
    assert any(r.id == chunk.id for r in rows)


@pytest.mark.asyncio
async def test_chunk_embedding_repo_upsert_then_get(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ns_repo = NamespaceRepo(session_factory=app_session_factory)
    chunk_repo = MemoryChunkRepo(session_factory=app_session_factory)
    embed_repo = ChunkEmbeddingRepo(session_factory=app_session_factory)
    tenant = uuid4()
    ns = await ns_repo.create(name="emb-ns", ns_type="metier", tenant_id=tenant)
    chunk = await chunk_repo.create(namespace_id=ns.id, content="emb test", tenant_id=tenant)

    # Use a 384-dim vector to match the bge-small partial HNSW index.
    vector = [0.1] * 384
    await embed_repo.upsert(
        chunk_id=chunk.id,
        model="bge-small-en-v1.5",
        embedding=vector,
        tenant_id=tenant,
    )
    row = await embed_repo.get(chunk.id, "bge-small-en-v1.5", tenant_id=tenant)
    assert row is not None
    assert row.model == "bge-small-en-v1.5"


@pytest.mark.asyncio
async def test_workflow_repo_create_then_list_active(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = WorkflowRepo(session_factory=app_session_factory)
    tenant = uuid4()
    created = await repo.create(name="wf-1", dag={"nodes": []}, tenant_id=tenant)
    active = await repo.list_active(tenant_id=tenant)
    assert any(w.id == created.id for w in active)


@pytest.mark.asyncio
async def test_workflow_run_repo_create_then_update_status(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    wf_repo = WorkflowRepo(session_factory=app_session_factory)
    run_repo = WorkflowRunRepo(session_factory=app_session_factory)
    tenant = uuid4()
    wf = await wf_repo.create(name="wf-2", dag={"nodes": []}, tenant_id=tenant)
    run = await run_repo.create(workflow_id=wf.id, correlation_id=uuid4(), tenant_id=tenant)
    rowcount = await run_repo.update_status(run.id, status="completed", tenant_id=tenant)
    assert rowcount == 1


@pytest.mark.asyncio
async def test_workflow_run_update_status_returns_zero_when_run_not_found(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Cross-tenant or missing run → silent failure path documented in the docstring."""
    run_repo = WorkflowRunRepo(session_factory=app_session_factory)
    rowcount = await run_repo.update_status(
        uuid4(),  # never inserted
        status="completed",
        tenant_id=uuid4(),
    )
    assert rowcount == 0


@pytest.mark.asyncio
async def test_agent_template_repo_create_then_get_by_name_version(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = AgentTemplateRepo(session_factory=app_session_factory)
    tenant = uuid4()
    template = await repo.create(
        name="dev-lead",
        archetype="orchestrateur",
        config={"model": "claude-opus"},
        tenant_id=tenant,
    )
    fetched = await repo.get_by_name_version("dev-lead", 1, tenant_id=tenant)
    assert fetched is not None
    assert fetched.id == template.id


@pytest.mark.asyncio
async def test_agent_instance_repo_create_then_get_by_id(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    template_repo = AgentTemplateRepo(session_factory=app_session_factory)
    instance_repo = AgentInstanceRepo(session_factory=app_session_factory)
    tenant = uuid4()
    template = await template_repo.create(
        name="researcher", archetype="chercheur", config={}, tenant_id=tenant
    )
    instance = await instance_repo.create(
        template_id=template.id,
        template_version=template.version,
        snapshot={"frozen": True},
        tenant_id=tenant,
    )
    fetched = await instance_repo.get_by_id(instance.id, tenant_id=tenant)
    assert fetched is not None
    assert fetched.template_id == template.id


@pytest.mark.asyncio
async def test_prompt_repo_create_then_get_by_template_version(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    template_repo = AgentTemplateRepo(session_factory=app_session_factory)
    prompt_repo = PromptRepo(session_factory=app_session_factory)
    tenant = uuid4()
    template = await template_repo.create(
        name="reviewer", archetype="controleur", config={}, tenant_id=tenant
    )
    prompt = await prompt_repo.create(
        agent_template_id=template.id,
        version=1,
        content="You are a reviewer.",
        tenant_id=tenant,
    )
    fetched = await prompt_repo.get_by_template_version(template.id, 1, tenant_id=tenant)
    assert fetched is not None
    assert fetched.id == prompt.id


@pytest.mark.asyncio
async def test_outbox_repo_insert_get_unprocessed_mark_processed(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = OutboxRepo(session_factory=app_session_factory)
    tenant = uuid4()
    event_id = uuid4()
    await repo.insert(
        event_id=event_id,
        correlation_id=uuid4(),
        event_type="m_test.entity.created",
        payload={"hello": "world"},
        tenant_id=tenant,
    )
    pending = await repo.get_unprocessed(limit=10, tenant_id=tenant)
    assert any(e.id == event_id for e in pending)

    rowcount = await repo.mark_processed(event_id, tenant_id=tenant)
    assert rowcount == 1

    pending_after = await repo.get_unprocessed(limit=10, tenant_id=tenant)
    assert all(e.id != event_id for e in pending_after)

    # Re-mark the same event → idempotent: no rows match the
    # ``processed_at IS NULL`` filter, returns 0.
    second = await repo.mark_processed(event_id, tenant_id=tenant)
    assert second == 0


@pytest.mark.asyncio
async def test_feature_flag_repo_set_then_get(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = FeatureFlagRepo(session_factory=app_session_factory)
    await repo.set(
        name="experimental_router",
        enabled=True,
        rollout_percentage=50,
        description="Hybrid router escalation",
    )
    flag = await repo.get("experimental_router")
    assert flag is not None
    assert flag.enabled is True
    assert flag.rollout_percentage == 50
