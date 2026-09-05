"""Unit tests — :class:`NamespaceRepo`."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from agentive_backend.shared.exceptions import ConflictError, NotFoundError
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


# ─── create / create_in_session (Story 3.2) ───────────────────────


@pytest.mark.asyncio
async def test_create_in_session_translates_integrity_error_to_conflict() -> None:
    factory, session = make_session_factory_mock()
    session.flush.side_effect = IntegrityError("INSERT", {}, Exception("duplicate key"))
    repo = NamespaceRepo(session_factory=factory)
    with pytest.raises(ConflictError, match="dup-ns"):
        await repo.create_in_session(session, name="dup-ns", ns_type="client")


@pytest.mark.asyncio
async def test_create_delegates_to_create_in_session_within_transaction() -> None:
    factory, session = make_session_factory_mock()
    repo = NamespaceRepo(session_factory=factory)
    ns = await repo.create(name="dev-notes", ns_type="metier", department="Dev")
    assert session.add.call_args.args[0] is ns
    assert ns.name == "dev-notes"
    assert ns.department == "Dev"


# ─── list_all (Story 3.2 AC3) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_list_all_emits_select_without_type_predicate() -> None:
    factory, session = make_session_factory_mock()
    repo = NamespaceRepo(session_factory=factory)
    await repo.list_all(limit=500)
    sql_text = str(session.execute.await_args.args[0]).lower()
    assert "namespaces" in sql_text
    assert "where" not in sql_text


@pytest.mark.asyncio
async def test_list_all_orders_by_created_at_then_id() -> None:
    """An unordered `SELECT` has no stable row order across refetches

    (code review Story 3.2, P6).
    """
    factory, session = make_session_factory_mock()
    repo = NamespaceRepo(session_factory=factory)
    await repo.list_all(limit=500)
    sql_text = str(session.execute.await_args.args[0]).lower()
    assert "order by namespaces.created_at asc, namespaces.id asc" in sql_text
