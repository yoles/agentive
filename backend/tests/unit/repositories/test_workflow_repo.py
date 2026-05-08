"""Unit tests — :class:`WorkflowRepo` and :class:`WorkflowRunRepo`."""

from __future__ import annotations

from uuid import uuid4

import pytest

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
