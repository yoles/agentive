"""Unit tests — :class:`WorkflowRepo` and :class:`WorkflowRunRepo`."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from agentive_backend.shared.exceptions import NotFoundError
from agentive_backend.shared.repositories import WorkflowRepo, WorkflowRunRepo

from .conftest import make_session_factory_mock


@pytest.mark.asyncio
async def test_workflow_create_adds_orm_row() -> None:
    factory, session = make_session_factory_mock()
    repo = WorkflowRepo(session_factory=factory)
    await repo.create(name="ingest", dag={"nodes": []})
    session.add.assert_called_once()
    workflow = session.add.call_args.args[0]
    assert workflow.name == "ingest"
    assert workflow.status == "active"


@pytest.mark.asyncio
async def test_workflow_create_in_session_adds_row_without_commit() -> None:
    """Story 4.1 T2.3 — create_in_session ajoute la row dans la session du
    caller (no commit), mirror AgentTemplateRepo.create_in_session."""
    _, session = make_session_factory_mock()
    repo = WorkflowRepo(session_factory=lambda: None)
    await repo.create_in_session(session, name="ingest", dag={"nodes": []})
    session.add.assert_called_once()
    session.flush.assert_awaited_once()
    session.refresh.assert_awaited_once()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
    workflow = session.add.call_args.args[0]
    assert workflow.name == "ingest"
    assert workflow.status == "active"
    assert workflow.version == 1


@pytest.mark.asyncio
async def test_workflow_run_update_status_returns_int_from_rowcount() -> None:
    factory, session = make_session_factory_mock()
    # Make execute return a result whose rowcount is 1.
    result_mock = session.execute.return_value
    result_mock.rowcount = 1
    repo = WorkflowRunRepo(session_factory=factory)
    n = await repo.update_status(uuid4(), status="completed")
    assert n == 1


@pytest.mark.asyncio
async def test_workflow_run_update_status_returns_zero_when_no_rows_match() -> None:
    """Silent failure path documented in the docstring (RLS or missing row)."""
    factory, session = make_session_factory_mock()
    result_mock = session.execute.return_value
    result_mock.rowcount = 0
    repo = WorkflowRunRepo(session_factory=factory)
    n = await repo.update_status(uuid4(), status="completed")
    assert n == 0


# ─── Story 4.2 T2.1 — WorkflowRepo.require_by_id ───────────────────────


@pytest.mark.asyncio
async def test_workflow_require_by_id_returns_row_when_found() -> None:
    factory, session = make_session_factory_mock()
    workflow_id = uuid4()
    session.get.return_value = object()
    repo = WorkflowRepo(session_factory=factory)
    result = await repo.require_by_id(workflow_id)
    assert result is session.get.return_value


@pytest.mark.asyncio
async def test_workflow_require_by_id_raises_not_found_when_absent() -> None:
    """AC1 — ``workflow_id`` unknown ⇒ 404 (id is the URL's primary resource,
    unlike 4.1's create_workflow where it lives in the body)."""
    factory, session = make_session_factory_mock()
    session.get.return_value = None
    workflow_id = uuid4()
    repo = WorkflowRepo(session_factory=factory)
    with pytest.raises(NotFoundError) as exc_info:
        await repo.require_by_id(workflow_id)
    assert exc_info.value.context["workflow_id"] == str(workflow_id)


# ─── Story 4.2 T2.2 — WorkflowRunRepo.update_checkpoint ────────────────


@pytest.mark.asyncio
async def test_workflow_run_update_checkpoint_returns_int_from_rowcount() -> None:
    factory, session = make_session_factory_mock()
    result_mock = session.execute.return_value
    result_mock.rowcount = 1
    repo = WorkflowRunRepo(session_factory=factory)
    n = await repo.update_checkpoint(
        uuid4(),
        checkpoint={"last_node_id": "a"},
        last_checkpoint_at=datetime.now(UTC),
    )
    assert n == 1


@pytest.mark.asyncio
async def test_workflow_run_update_checkpoint_returns_zero_when_no_rows_match() -> None:
    factory, session = make_session_factory_mock()
    result_mock = session.execute.return_value
    result_mock.rowcount = 0
    repo = WorkflowRunRepo(session_factory=factory)
    n = await repo.update_checkpoint(
        uuid4(),
        checkpoint={"last_node_id": "a"},
        last_checkpoint_at=datetime.now(UTC),
    )
    assert n == 0


# ─── Story 4.2 T2.3 — WorkflowRunRepo.claim_stale_running ──────────────


@pytest.mark.asyncio
async def test_workflow_run_claim_stale_running_returns_scalars() -> None:
    factory, session = make_session_factory_mock()
    stale_run = object()
    session.execute.return_value.scalars.return_value.all.return_value = [stale_run]
    repo = WorkflowRunRepo(session_factory=factory)
    result = await repo.claim_stale_running(older_than=datetime.now(UTC))
    assert result == [stale_run]


@pytest.mark.asyncio
async def test_workflow_run_claim_stale_running_uses_no_tenant_scoping() -> None:
    """T2.3 — the recovery worker sweeps every tenant, mirror
    ``MemoryArchivalWorker`` which also has no per-run tenant scoping."""
    factory, session = make_session_factory_mock()
    session.execute.return_value.scalars.return_value.all.return_value = []
    repo = WorkflowRunRepo(session_factory=factory)
    await repo.claim_stale_running(older_than=datetime.now(UTC))
    # `claim_stale_running` accepts no `tenant_id` kwarg at all — a call with
    # one would be a TypeError, enforced structurally by the signature
    # rather than re-asserted here on the mock.
    session.execute.assert_awaited_once()
