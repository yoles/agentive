"""Unit tests for :class:`BaseRepo.with_tenant` — mocked session.

The async context manager :meth:`BaseRepo.with_tenant` has 4 invariants:

1. ``tenant_id is not None`` → exactly one ``set_config('app.tenant_id', ...)``
   statement is executed before yielding.
2. ``tenant_id is None`` → no ``set_config`` statement is executed (single
   tenant MVP — relies on the ``tenant_id IS NULL`` clause of the RLS
   policy).
3. Successful exit → ``session.commit()`` is awaited exactly once.
4. Exception in the body → ``session.rollback()`` is awaited and the
   exception re-raised.

These tests do not touch a real database — they use ``AsyncMock`` to verify
the call graph. The integration counterpart lives in
``tests/integration/repositories/test_with_tenant_rls.py`` and exercises the
real Postgres RLS policy.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from agentive_backend.shared.repositories.base import BaseRepo


def _make_session_factory_mock() -> tuple[MagicMock, AsyncMock]:
    """Return a pair (session_factory_mock, session_mock).

    The factory's ``__call__`` returns an async context manager that yields
    the session mock. ``session.commit`` / ``rollback`` / ``execute`` are
    AsyncMocks — they record their await calls.
    """
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.execute = AsyncMock()

    # async with factory() as session: ...
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)

    factory = MagicMock(return_value=cm)
    return factory, session


@pytest.mark.asyncio
async def test_with_tenant_when_tenant_id_provided_should_execute_set_config() -> None:
    factory, session = _make_session_factory_mock()
    repo = BaseRepo(session_factory=factory)
    tenant_id = uuid4()

    async with repo.with_tenant(tenant_id) as got_session:
        assert got_session is session

    # set_config + nothing else inside the body
    assert session.execute.await_count == 1
    args, kwargs = session.execute.await_args
    sql_text = str(args[0])
    assert "set_config" in sql_text
    assert "app.tenant_id" in sql_text
    bound_params = args[1] if len(args) > 1 else kwargs.get("params") or {}
    assert bound_params == {"tid": str(tenant_id)}
    session.commit.assert_awaited_once()
    session.rollback.assert_not_called()


@pytest.mark.asyncio
async def test_with_tenant_when_tenant_id_is_none_should_skip_set_config() -> None:
    factory, session = _make_session_factory_mock()
    repo = BaseRepo(session_factory=factory)

    async with repo.with_tenant(None) as got_session:
        assert got_session is session

    session.execute.assert_not_awaited()
    session.commit.assert_awaited_once()
    session.rollback.assert_not_called()


@pytest.mark.asyncio
async def test_with_tenant_when_body_succeeds_should_commit() -> None:
    factory, session = _make_session_factory_mock()
    repo = BaseRepo(session_factory=factory)

    async with repo.with_tenant(None):
        pass

    session.commit.assert_awaited_once()
    session.rollback.assert_not_called()


@pytest.mark.asyncio
async def test_with_tenant_when_body_raises_should_rollback_and_reraise() -> None:
    factory, session = _make_session_factory_mock()
    repo = BaseRepo(session_factory=factory)

    class _BodyError(Exception):
        pass

    with pytest.raises(_BodyError):
        async with repo.with_tenant(None):
            raise _BodyError("boom")

    session.rollback.assert_awaited_once()
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_with_tenant_uuid_string_canonical_form() -> None:
    """The bound parameter must be the canonical 36-char UUID string."""
    factory, session = _make_session_factory_mock()
    repo = BaseRepo(session_factory=factory)
    tenant_id = UUID("01923a8e-7c1d-7e3f-9a4b-0123456789ab")

    async with repo.with_tenant(tenant_id):
        pass

    args, _ = session.execute.await_args
    bound_params = args[1] if len(args) > 1 else {}
    assert bound_params["tid"] == "01923a8e-7c1d-7e3f-9a4b-0123456789ab"
    assert len(bound_params["tid"]) == 36
