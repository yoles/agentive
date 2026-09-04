"""Unit tests — :class:`ChunkEmbeddingRepo` mock-driven.

``search_ann`` (Story 3.1 T2.3) is exercised at the call-shape level here
(SQL text assertions, no real Postgres) — the ANN ranking itself is proven
against a real HNSW index in
``tests/integration/repositories/test_chunk_embedding_search.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from agentive_backend.shared.repositories import ChunkEmbeddingRepo

from .conftest import make_session_factory_mock


@pytest.mark.asyncio
async def test_upsert_emits_insert_on_conflict_do_update() -> None:
    factory, session = make_session_factory_mock()
    repo = ChunkEmbeddingRepo(session_factory=factory)
    await repo.upsert(chunk_id=uuid4(), model="bge-m3", embedding=[0.1, 0.2, 0.3])
    sql_text = str(session.execute.await_args.args[0]).upper()
    assert "INSERT INTO" in sql_text
    assert "ON CONFLICT" in sql_text


@pytest.mark.asyncio
async def test_upsert_writes_source_on_insert_and_on_conflict_update() -> None:
    """IG3 — provenance must be set on first insert AND refreshed on
    re-embedding (`ON CONFLICT DO UPDATE`), not just at creation time."""
    factory, session = make_session_factory_mock()
    repo = ChunkEmbeddingRepo(session_factory=factory)
    await repo.upsert(chunk_id=uuid4(), model="bge-m3", embedding=[0.1, 0.2, 0.3], source="mock")

    stmt = session.execute.await_args.args[0]
    assert stmt.compile().params["source"] == "mock"
    # The ON CONFLICT SET clause must also carry `source`, not just the
    # values() passed to the initial INSERT — otherwise re-embedding a
    # chunk with a real provider would silently leave a stale "mock" label.
    sql_text = str(stmt).lower()
    assert "do update set embedding = " in sql_text
    assert ", source = " in sql_text.split("do update set")[1]


@pytest.mark.asyncio
async def test_upsert_source_defaults_to_none() -> None:
    factory, session = make_session_factory_mock()
    repo = ChunkEmbeddingRepo(session_factory=factory)
    await repo.upsert(chunk_id=uuid4(), model="bge-m3", embedding=[0.1, 0.2, 0.3])
    stmt = session.execute.await_args.args[0]
    assert stmt.compile().params["source"] is None


@pytest.mark.asyncio
async def test_get_uses_composite_pk() -> None:
    factory, session = make_session_factory_mock()
    repo = ChunkEmbeddingRepo(session_factory=factory)
    chunk_id = uuid4()
    await repo.get(chunk_id, "bge-m3")
    session.get.assert_awaited_once()
    args = session.get.await_args.args
    assert args[1] == (chunk_id, "bge-m3")


@pytest.mark.asyncio
async def test_search_ann_sets_ef_search_inline_then_selects_filtered_by_model() -> None:
    factory, session = make_session_factory_mock()
    # search_ann calls execute() three times: two SET LOCAL, then the
    # select. Configure `.all()` on the third call's result so the list
    # comprehension doesn't choke on an unconfigured MagicMock.
    session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    repo = ChunkEmbeddingRepo(session_factory=factory)
    namespace_id = uuid4()

    await repo.search_ann(
        [0.1, 0.2, 0.3],
        model="text-embedding-3-small",
        namespace_id=namespace_id,
        top_k=5,
        ef_search=100,
    )

    assert session.execute.await_count == 3
    set_local_sql = str(session.execute.await_args_list[0].args[0])
    # Story 1.3 Debug Log #2 — inlined value, no bind placeholder.
    assert "SET LOCAL hnsw.ef_search = 100" in set_local_sql

    # BS2: without this, namespace_id/archived_at/expires_at (post-index
    # filters, not part of the partial index predicate) can silently
    # under-fill top_k once the HNSW candidate window is exhausted.
    iterative_scan_sql = str(session.execute.await_args_list[1].args[0])
    assert "SET LOCAL hnsw.iterative_scan = 'strict_order'" in iterative_scan_sql

    # Assert the PREDICATES, not the mere presence of the words. `"model" in
    # sql` stayed true even with the `model` filter deleted (the column shows
    # up in the projection and the join), so it proved nothing. Code review
    # Story 3.1, P11.
    select_sql = str(session.execute.await_args_list[2].args[0]).lower()
    assert "from memory_chunks join chunk_embeddings" in select_sql
    assert "chunk_embeddings.model = " in select_sql
    assert "memory_chunks.namespace_id = " in select_sql
    assert "memory_chunks.archived_at is null" in select_sql
    # P7: deterministic tie-break, not just `ORDER BY distance`.
    assert "order by distance, memory_chunks.created_at desc, memory_chunks.id" in select_sql
    assert "limit" in select_sql
    # IG2: a TTL'd chunk must not stay searchable forever.
    assert "memory_chunks.expires_at is null" in select_sql
    assert "memory_chunks.expires_at >" in select_sql


@pytest.mark.asyncio
async def test_search_ann_defaults_now_to_the_current_time_when_omitted() -> None:
    """IG2 — callers that don't pass `now` (mostly tests) still get a real,
    live expiry cutoff bound into the query, not an unbounded filter."""
    factory, session = make_session_factory_mock()
    session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    repo = ChunkEmbeddingRepo(session_factory=factory)

    await repo.search_ann([0.1], model="text-embedding-3-small", namespace_id=uuid4(), top_k=5)

    stmt = session.execute.await_args_list[2].args[0]
    bound_now = stmt.compile().params.get("expires_at_1")
    assert bound_now is not None


@pytest.mark.asyncio
async def test_search_ann_uses_the_provided_now() -> None:
    from datetime import UTC, datetime

    factory, session = make_session_factory_mock()
    session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    repo = ChunkEmbeddingRepo(session_factory=factory)
    fixed_now = datetime(2030, 1, 1, tzinfo=UTC)

    await repo.search_ann(
        [0.1], model="text-embedding-3-small", namespace_id=uuid4(), top_k=5, now=fixed_now
    )

    stmt = session.execute.await_args_list[2].args[0]
    params = stmt.compile().params
    assert fixed_now in params.values()


@pytest.mark.parametrize(
    "bad_ef_search",
    [
        pytest.param("100; DROP TABLE chunk_embeddings", id="sql-injection-string"),
        # `bool` is an `int` subclass, so the original isinstance guard let it
        # through and Postgres rejected `SET LOCAL ... = True` (P6).
        pytest.param(True, id="bool-is-an-int-subclass"),
    ],
)
@pytest.mark.asyncio
async def test_search_ann_rejects_non_int_ef_search(bad_ef_search: object) -> None:
    factory, _session = make_session_factory_mock()
    repo = ChunkEmbeddingRepo(session_factory=factory)
    with pytest.raises(TypeError):
        await repo.search_ann(
            [0.1],
            model="text-embedding-3-small",
            namespace_id=uuid4(),
            top_k=5,
            ef_search=bad_ef_search,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("bad_ef_search", [0, -5, 1001])
@pytest.mark.asyncio
async def test_search_ann_rejects_out_of_range_ef_search(bad_ef_search: int) -> None:
    """P6: pgvector accepts [1, 1000]; outside that Postgres rejects the
    SET LOCAL with an error nothing maps to RFC 7807."""
    factory, _session = make_session_factory_mock()
    repo = ChunkEmbeddingRepo(session_factory=factory)
    with pytest.raises(ValueError, match=r"ef_search must be in \[1, 1000\]"):
        await repo.search_ann(
            [0.1],
            model="text-embedding-3-small",
            namespace_id=uuid4(),
            top_k=5,
            ef_search=bad_ef_search,
        )


@pytest.mark.asyncio
async def test_search_ann_maps_rows_to_score() -> None:
    factory, session = make_session_factory_mock()
    chunk = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[(chunk, 0.25)])))
    repo = ChunkEmbeddingRepo(session_factory=factory)

    results = await repo.search_ann(
        [0.1], model="text-embedding-3-small", namespace_id=uuid4(), top_k=5
    )

    assert results == [(chunk, 0.75)]  # score = 1.0 - distance
