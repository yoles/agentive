"""Unit tests — :class:`MemoryChunkRepo` mock-driven."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from agentive_backend.shared.repositories import MemoryChunkRepo

from .conftest import make_session_factory_mock


@pytest.mark.asyncio
async def test_create_preserves_explicit_empty_metadata_dict() -> None:
    """Regression: ``metadata or {}`` collapsed user-supplied ``{}``."""
    factory, session = make_session_factory_mock()
    repo = MemoryChunkRepo(session_factory=factory)
    explicit_empty: dict = {}
    await repo.create(
        namespace_id=uuid4(),
        content="hello",
        metadata=explicit_empty,
    )
    chunk = session.add.call_args.args[0]
    # Should be the SAME object the caller passed (not a fresh dict).
    assert chunk.metadata_ is explicit_empty


@pytest.mark.asyncio
async def test_create_defaults_metadata_to_empty_dict_when_none() -> None:
    factory, session = make_session_factory_mock()
    repo = MemoryChunkRepo(session_factory=factory)
    await repo.create(namespace_id=uuid4(), content="x", metadata=None)
    chunk = session.add.call_args.args[0]
    assert chunk.metadata_ == {}


@pytest.mark.asyncio
async def test_list_by_namespace_emits_select_with_limit() -> None:
    factory, session = make_session_factory_mock()
    repo = MemoryChunkRepo(session_factory=factory)
    await repo.list_by_namespace(uuid4(), limit=42)
    sql_text = str(session.execute.await_args.args[0])
    assert "memory_chunks" in sql_text.lower()
    assert "limit" in sql_text.lower()


@pytest.mark.asyncio
async def test_create_passes_expires_at_through_to_the_row() -> None:
    """Story 3.1 T2.2 — `create()` used to accept `ttl_seconds` but never
    write `expires_at`; this is the gap-closing regression guard."""
    factory, session = make_session_factory_mock()
    repo = MemoryChunkRepo(session_factory=factory)
    expires_at = datetime(2030, 1, 1, tzinfo=UTC)
    await repo.create(
        namespace_id=uuid4(), content="hello", ttl_seconds=3600, expires_at=expires_at
    )
    chunk = session.add.call_args.args[0]
    assert chunk.expires_at == expires_at


@pytest.mark.asyncio
async def test_create_defaults_expires_at_to_none() -> None:
    factory, session = make_session_factory_mock()
    repo = MemoryChunkRepo(session_factory=factory)
    await repo.create(namespace_id=uuid4(), content="hello")
    chunk = session.add.call_args.args[0]
    assert chunk.expires_at is None


@pytest.mark.asyncio
async def test_delete_by_id_emits_delete_scoped_to_the_id() -> None:
    """IG1 — compensating cleanup for a chunk whose embedding write failed."""
    factory, session = make_session_factory_mock()
    repo = MemoryChunkRepo(session_factory=factory)
    chunk_id = uuid4()

    await repo.delete_by_id(chunk_id)

    sql_text = str(session.execute.await_args.args[0]).lower()
    assert "delete from memory_chunks" in sql_text
    assert "memory_chunks.id = " in sql_text


# ─── count_by_namespace_ids (Story 3.2 AC3) ────────────────────────


@pytest.mark.asyncio
async def test_count_by_namespace_ids_returns_empty_dict_for_empty_input() -> None:
    factory, session = make_session_factory_mock()
    repo = MemoryChunkRepo(session_factory=factory)
    result = await repo.count_by_namespace_ids([])
    assert result == {}
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_count_by_namespace_ids_emits_one_grouped_query() -> None:
    ns_a, ns_b = uuid4(), uuid4()
    factory, session = make_session_factory_mock()
    session.execute.return_value.tuples.return_value.all = lambda: [(ns_a, 3)]
    repo = MemoryChunkRepo(session_factory=factory)

    result = await repo.count_by_namespace_ids([ns_a, ns_b])

    assert session.execute.await_count == 1
    sql_text = str(session.execute.await_args.args[0]).lower()
    assert "group by" in sql_text
    assert "archived_at is null" in sql_text
    assert "expires_at" in sql_text
    assert result == {ns_a: 3}
    assert result.get(ns_b, 0) == 0


@pytest.mark.asyncio
async def test_count_by_namespace_ids_filters_expired_chunks_like_search_ann() -> None:
    """AC3 claims this count is coherent with `search_ann`'s own filter,

    which also excludes expired (not just archived) chunks — this used to
    be false, over-counting chunks a search would never return (code
    review Story 3.2, BS2).
    """
    ns_a = uuid4()
    factory, session = make_session_factory_mock()
    session.execute.return_value.tuples.return_value.all = lambda: []
    repo = MemoryChunkRepo(session_factory=factory)

    await repo.count_by_namespace_ids([ns_a])

    sql_text = str(session.execute.await_args.args[0]).lower()
    assert "expires_at is null" in sql_text
    assert "expires_at >" in sql_text


# ─── find_expired / mark_archived / find_archivable_in_namespace (Story 3.3) ──


@pytest.mark.asyncio
async def test_find_expired_emits_select_matching_the_partial_index() -> None:
    """T1.1 — the predicate must match `ix_memory_chunks_expires_at`
    (``archived_at IS NULL AND expires_at IS NOT NULL``) exactly, plus the
    `expires_at < now` cutoff, ordered and limited."""
    factory, session = make_session_factory_mock()
    session.execute.return_value.scalars.return_value.all = lambda: []
    repo = MemoryChunkRepo(session_factory=factory)
    now = datetime(2030, 1, 1, tzinfo=UTC)

    await repo.find_expired(now, limit=50)

    sql_text = str(session.execute.await_args.args[0]).lower()
    assert "archived_at is null" in sql_text
    assert "expires_at is not null" in sql_text
    assert "expires_at <" in sql_text
    assert "order by" in sql_text
    assert "limit" in sql_text


@pytest.mark.asyncio
async def test_find_archivable_in_namespace_emits_select_with_all_three_filters() -> None:
    """T1.3 — namespace scope, live (not expired), old enough."""
    factory, session = make_session_factory_mock()
    session.execute.return_value.scalars.return_value.all = lambda: []
    repo = MemoryChunkRepo(session_factory=factory)
    namespace_id = uuid4()
    threshold = datetime(2029, 1, 1, tzinfo=UTC)
    now = datetime(2030, 1, 1, tzinfo=UTC)

    await repo.find_archivable_in_namespace(namespace_id, threshold, limit=50, now=now)

    sql_text = str(session.execute.await_args.args[0]).lower()
    assert "memory_chunks.namespace_id = " in sql_text
    assert "memory_chunks.archived_at is null" in sql_text
    assert "memory_chunks.expires_at is null" in sql_text
    assert "memory_chunks.expires_at > " in sql_text
    assert "memory_chunks.created_at <= " in sql_text


@pytest.mark.asyncio
async def test_find_archivable_in_namespace_defaults_now_to_the_current_time() -> None:
    factory, session = make_session_factory_mock()
    session.execute.return_value.scalars.return_value.all = lambda: []
    repo = MemoryChunkRepo(session_factory=factory)

    await repo.find_archivable_in_namespace(uuid4(), datetime(2029, 1, 1, tzinfo=UTC), limit=50)

    stmt = session.execute.await_args.args[0]
    bound_now = stmt.compile().params.get("expires_at_1")
    assert bound_now is not None


@pytest.mark.asyncio
async def test_mark_archived_returns_zero_for_empty_input_without_a_query() -> None:
    factory, session = make_session_factory_mock()
    repo = MemoryChunkRepo(session_factory=factory)

    result = await repo.mark_archived([], archived_at=datetime(2030, 1, 1, tzinfo=UTC))

    assert result == 0
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_archived_emits_update_guarded_by_archived_at_is_null() -> None:
    """T1.2 — the guard avoids clobbering a value already set by a
    concurrent/previous run."""
    factory, session = make_session_factory_mock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    repo = MemoryChunkRepo(session_factory=factory)
    chunk_id = uuid4()
    archived_at = datetime(2030, 1, 1, tzinfo=UTC)

    result = await repo.mark_archived([chunk_id], archived_at=archived_at)

    sql_text = str(session.execute.await_args.args[0]).lower()
    assert "update memory_chunks set" in sql_text
    assert "archived_at is null" in sql_text
    assert result == 1


@pytest.mark.asyncio
async def test_mark_archived_returns_actual_rowcount_not_len_of_input() -> None:
    """A concurrent run may have already archived one of the two ids — the
    caller (the metric, the run summary) must see 1, not 2."""
    factory, session = make_session_factory_mock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    repo = MemoryChunkRepo(session_factory=factory)

    result = await repo.mark_archived(
        [uuid4(), uuid4()], archived_at=datetime(2030, 1, 1, tzinfo=UTC)
    )

    assert result == 1


@pytest.mark.asyncio
async def test_mark_archived_defensively_handles_a_missing_rowcount() -> None:
    """`Result` (the generic type `session.execute()` returns) doesn't
    declare `.rowcount` — a driver/mock that omits it must not crash."""
    factory, session = make_session_factory_mock()
    session.execute = AsyncMock(return_value=MagicMock(spec=[]))
    repo = MemoryChunkRepo(session_factory=factory)

    result = await repo.mark_archived([uuid4()], archived_at=datetime(2030, 1, 1, tzinfo=UTC))

    assert result == 0
