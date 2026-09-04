"""Unit tests — :class:`NamespaceRepo`."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agentive_backend.shared.exceptions import NotFoundError
from agentive_backend.shared.repositories import NamespaceRepo

from .conftest import make_session_factory_mock


@pytest.mark.asyncio
async def test_create_persists_namespace_with_defaults() -> None:
    factory, session = make_session_factory_mock()
    repo = NamespaceRepo(session_factory=factory)
    await repo.create(name="ops-ns", ns_type="operationnelle")
    ns = session.add.call_args.args[0]
    assert ns.name == "ops-ns"
    assert ns.type == "operationnelle"
    assert ns.embedding_backend == "cloud"


@pytest.mark.asyncio
async def test_list_by_type_emits_select_with_type_predicate() -> None:
    factory, session = make_session_factory_mock()
    repo = NamespaceRepo(session_factory=factory)
    await repo.list_by_type("metier", limit=20)
    sql_text = str(session.execute.await_args.args[0]).lower()
    assert "namespaces" in sql_text
    assert "limit" in sql_text


@pytest.mark.asyncio
async def test_get_by_name_executes_select_on_namespaces() -> None:
    factory, session = make_session_factory_mock()
    repo = NamespaceRepo(session_factory=factory)
    await repo.get_by_name("client-acme")
    sql_text = str(session.execute.await_args.args[0]).lower()
    assert "namespaces" in sql_text


@pytest.mark.asyncio
async def test_require_by_name_returns_entity_when_found() -> None:
    factory, session = make_session_factory_mock()
    sentinel = MagicMock()
    session.execute.return_value.scalar_one_or_none = MagicMock(return_value=sentinel)
    repo = NamespaceRepo(session_factory=factory)
    result = await repo.require_by_name("client-acme")
    assert result is sentinel


@pytest.mark.asyncio
async def test_require_by_name_raises_not_found_when_missing() -> None:
    factory, session = make_session_factory_mock()
    session.execute.return_value.scalar_one_or_none = MagicMock(return_value=None)
    repo = NamespaceRepo(session_factory=factory)
    with pytest.raises(NotFoundError):
        await repo.require_by_name("ghost-namespace")
