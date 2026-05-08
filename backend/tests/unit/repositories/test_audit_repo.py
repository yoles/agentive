"""Unit tests — :class:`AuditEventRepo` immutable surface."""

from __future__ import annotations

from uuid import uuid4

import pytest

from agentive_backend.shared.repositories import AuditEventRepo

from .conftest import make_session_factory_mock


@pytest.mark.asyncio
async def test_record_raises_not_implemented_until_story_9_1() -> None:
    factory, _ = make_session_factory_mock()
    repo = AuditEventRepo(session_factory=factory)
    with pytest.raises(NotImplementedError, match=r"Story 9\.1"):
        await repo.record(actor="x", action="y.z.t", correlation_id=uuid4())


@pytest.mark.asyncio
async def test_record_unsafe_requires_explicit_opt_in_at_construction() -> None:
    """Default construction MUST NOT allow ``_record_unsafe`` to write."""
    factory, _ = make_session_factory_mock()
    repo = AuditEventRepo(session_factory=factory)  # _allow_unsafe_writes defaults to False
    with pytest.raises(RuntimeError, match="_allow_unsafe_writes"):
        await repo._record_unsafe(actor="x", action="y.z.t", correlation_id=uuid4())


@pytest.mark.asyncio
async def test_record_unsafe_proceeds_when_opt_in_flag_is_set() -> None:
    factory, session = make_session_factory_mock()
    repo = AuditEventRepo(session_factory=factory, _allow_unsafe_writes=True)
    await repo._record_unsafe(actor="actor-a", action="test.audit.write", correlation_id=uuid4())
    session.add.assert_called_once()
    event = session.add.call_args.args[0]
    assert event.actor == "actor-a"
    assert event.metadata_ == {}
