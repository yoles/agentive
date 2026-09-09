"""Unit tests — :class:`NamespaceRepo`."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from agentive_backend.shared.exceptions import ConflictError, NotFoundError
from agentive_backend.shared.repositories import NamespaceRepo
from agentive_backend.shared.repositories.namespace_repo import NAMESPACE_LISTING_SAFETY_CAP

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
async def test_create_persists_a_non_cloud_embedding_backend() -> None:
    """Story 3.6 T13.4 — the signature already supported `embedding_backend`
    (Story 3.1); this confirms `"local"`/`"voyage"` persist as plain
    strings, no new repo code required."""
    factory, session = make_session_factory_mock()
    repo = NamespaceRepo(session_factory=factory)
    await repo.create(name="local-ns", ns_type="metier", embedding_backend="local")
    ns = session.add.call_args.args[0]
    assert ns.embedding_backend == "local"


@pytest.mark.asyncio
async def test_create_in_session_persists_a_non_cloud_embedding_backend() -> None:
    factory, session = make_session_factory_mock()
    repo = NamespaceRepo(session_factory=factory)
    await repo.create_in_session(
        session, name="voyage-ns", ns_type="metier", embedding_backend="voyage"
    )
    ns = session.add.call_args.args[0]
    assert ns.embedding_backend == "voyage"


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


@pytest.mark.asyncio
async def test_list_all_default_limit_is_a_generous_safety_net() -> None:
    """AC3 promises "tous les namespaces" — the old default of 500 was a

    silent, undocumented ceiling. Namespaces are only ever admin-created
    (unlike `memory_chunks`), so a much higher default is safe (code
    review Story 3.2, BS3).
    """
    factory, session = make_session_factory_mock()
    repo = NamespaceRepo(session_factory=factory)
    await repo.list_all()
    stmt = session.execute.await_args.args[0]
    assert stmt.compile().params["param_1"] == NAMESPACE_LISTING_SAFETY_CAP


@pytest.mark.asyncio
async def test_create_defaults_decay_policy_to_empty_mapping() -> None:
    """Story 3.4 T6.1 — `{}` is `DecayFunction.NONE`, i.e. the pre-3.4

    ranking. A namespace created without an explicit policy must not decay.
    """
    factory, session = make_session_factory_mock()
    repo = NamespaceRepo(session_factory=factory)
    await repo.create(name="no-decay-ns", ns_type="metier")
    assert session.add.call_args.args[0].decay_policy == {}


@pytest.mark.asyncio
async def test_create_persists_an_explicit_decay_policy() -> None:
    factory, session = make_session_factory_mock()
    repo = NamespaceRepo(session_factory=factory)
    policy = {"function": "exponential", "half_life_seconds": 2_592_000}
    await repo.create(name="decay-ns", ns_type="metier", decay_policy=policy)
    assert session.add.call_args.args[0].decay_policy == policy


@pytest.mark.asyncio
async def test_create_in_session_persists_decay_policy() -> None:
    factory, session = make_session_factory_mock()
    repo = NamespaceRepo(session_factory=factory)
    policy = {"function": "linear", "horizon_seconds": 86_400}
    async with repo.with_tenant(None) as opened:
        await repo.create_in_session(
            opened, name="decay-ns-2", ns_type="metier", decay_policy=policy
        )
    assert session.add.call_args.args[0].decay_policy == policy
