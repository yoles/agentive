"""Integration — ``ChunkEmbeddingRepo.search_ann`` against a real HNSW index
(Story 3.1 T2.3/T6.2).

Proves what the mock-driven unit tests in ``tests/unit/repositories/
test_chunk_embedding_repo.py`` cannot: real cosine ANN ranking, the
per-model partial-index filter, and archived-row exclusion — against the
actual schema (``chunk_embeddings_openai_hnsw``) migrated in Story 1.1.

NOT a re-benchmark: ~10 chunks, not 10k — NFR5 perf is already GO'd by the
Story 1.3 ADR (see Story 3.1 Dev Notes § Latence). This suite only checks
correctness of the applicative query.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.infra.db.models import MemoryChunk
from agentive_backend.shared.llm.testing import MockEmbedder
from agentive_backend.shared.repositories import (
    ChunkEmbeddingRepo,
    MemoryChunkRepo,
    NamespaceRepo,
)

pytestmark = pytest.mark.integration

OPENAI_MODEL = "text-embedding-3-small"
BGE_MODEL = "bge-small-en-v1.5"


def _normalise(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def _vector(text: str, dims: int = 1536) -> list[float]:
    """Deterministic unit-norm vector — reuses the TEST-ONLY hash-derived
    generator so two different `text` inputs are guaranteed to embed to
    different vectors (needed for a meaningful similarity ranking).

    Truncating a 1536-dim unit vector to 384 dims leaves a norm of ~0.5, so
    the result is re-normalised: the docstring promised unit-norm and a
    future score assertion on the bge model would have been silently wrong
    (code review Story 3.1, P14).
    """
    return _normalise(MockEmbedder.vector_for(text)[:dims])


def _blend(base: list[float], other: list[float], weight: float) -> list[float]:
    """Unit vector at a controlled cosine distance from ``base``.

    Lets a test state the EXPECTED similarity analytically instead of
    asserting that a descending sort is descending (P11).
    """
    return _normalise([(1.0 - weight) * b + weight * o for b, o in zip(base, other, strict=True)])


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


@pytest.mark.asyncio
async def test_search_ann_ranks_by_similarity_filters_model_and_excludes_archived(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ns_repo = NamespaceRepo(session_factory=app_session_factory)
    chunk_repo = MemoryChunkRepo(session_factory=app_session_factory)
    embedding_repo = ChunkEmbeddingRepo(session_factory=app_session_factory)

    namespace = await ns_repo.create(name="search-ns", ns_type="metier")

    # Target: identical vector to the query → top-1, score ~1.0.
    query_text = "the quick brown fox"
    target = await chunk_repo.create(namespace_id=namespace.id, content="target chunk")
    await embedding_repo.upsert(
        chunk_id=target.id, model=OPENAI_MODEL, embedding=_vector(query_text)
    )
    # Story 3.1 T2.3 — same chunk ALSO has a bge embedding. The `model`
    # filter must retain only the openai row for an openai-model search.
    await embedding_repo.upsert(
        chunk_id=target.id, model=BGE_MODEL, embedding=_vector(query_text, dims=384)
    )

    # Distractors — distinct content → distinct (less similar) vectors.
    for i in range(3):
        chunk = await chunk_repo.create(namespace_id=namespace.id, content=f"distractor {i}")
        await embedding_repo.upsert(
            chunk_id=chunk.id, model=OPENAI_MODEL, embedding=_vector(f"unrelated topic {i}")
        )

    # Archived — has a matching-model embedding but must be excluded.
    archived = await chunk_repo.create(namespace_id=namespace.id, content="archived chunk")
    await embedding_repo.upsert(
        chunk_id=archived.id, model=OPENAI_MODEL, embedding=_vector(query_text)
    )
    async with app_session_factory() as session:
        await session.execute(
            update(MemoryChunk)
            .where(MemoryChunk.id == archived.id)
            .values(archived_at=datetime.now(UTC))
        )
        await session.commit()

    results = await embedding_repo.search_ann(
        _vector(query_text),
        model=OPENAI_MODEL,
        namespace_id=namespace.id,
        top_k=5,
    )

    # 4 candidates total (target + 3 distractors) — archived excluded.
    assert len(results) == 4
    result_ids = [chunk.id for chunk, _score in results]
    assert archived.id not in result_ids
    assert target.id in result_ids

    # The identical-vector target ranks first with (near-)perfect similarity.
    assert results[0][0].id == target.id
    assert results[0][1] > 0.999


@pytest.mark.asyncio
async def test_search_ann_respects_top_k(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ns_repo = NamespaceRepo(session_factory=app_session_factory)
    chunk_repo = MemoryChunkRepo(session_factory=app_session_factory)
    embedding_repo = ChunkEmbeddingRepo(session_factory=app_session_factory)

    namespace = await ns_repo.create(name="topk-ns", ns_type="metier")
    for i in range(8):
        chunk = await chunk_repo.create(namespace_id=namespace.id, content=f"chunk {i}")
        await embedding_repo.upsert(
            chunk_id=chunk.id, model=OPENAI_MODEL, embedding=_vector(f"chunk {i}")
        )

    results = await embedding_repo.search_ann(
        _vector("chunk 0"), model=OPENAI_MODEL, namespace_id=namespace.id, top_k=3
    )
    assert len(results) == 3


@pytest.mark.asyncio
async def test_search_ann_scoped_to_namespace(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ns_repo = NamespaceRepo(session_factory=app_session_factory)
    chunk_repo = MemoryChunkRepo(session_factory=app_session_factory)
    embedding_repo = ChunkEmbeddingRepo(session_factory=app_session_factory)

    ns_a = await ns_repo.create(name="ns-a", ns_type="metier")
    ns_b = await ns_repo.create(name="ns-b", ns_type="metier")

    chunk_a = await chunk_repo.create(namespace_id=ns_a.id, content="in namespace A")
    await embedding_repo.upsert(chunk_id=chunk_a.id, model=OPENAI_MODEL, embedding=_vector("x"))

    chunk_b = await chunk_repo.create(namespace_id=ns_b.id, content="in namespace B")
    await embedding_repo.upsert(chunk_id=chunk_b.id, model=OPENAI_MODEL, embedding=_vector("x"))

    results = await embedding_repo.search_ann(
        _vector("x"), model=OPENAI_MODEL, namespace_id=ns_a.id, top_k=10
    )
    assert [chunk.id for chunk, _score in results] == [chunk_a.id]


@pytest.mark.slow
@pytest.mark.asyncio
async def test_search_ann_sanity_check_latency_on_200_chunks(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """T6.4 — NOT an NFR5 proof (that's the Story 1.3 ADR, 10k chunks,
    p95=14.52ms). This only catches a gross applicative regression (e.g. an
    unfiltered scan) on a ~200-chunk fixture — a generous 5s ceiling, not a
    tuned perf assertion."""
    import time

    ns_repo = NamespaceRepo(session_factory=app_session_factory)
    chunk_repo = MemoryChunkRepo(session_factory=app_session_factory)
    embedding_repo = ChunkEmbeddingRepo(session_factory=app_session_factory)

    namespace = await ns_repo.create(name="latency-sanity-ns", ns_type="metier")
    for i in range(200):
        chunk = await chunk_repo.create(namespace_id=namespace.id, content=f"chunk {i}")
        await embedding_repo.upsert(
            chunk_id=chunk.id, model=OPENAI_MODEL, embedding=_vector(f"chunk {i}")
        )

    start = time.monotonic()
    results = await embedding_repo.search_ann(
        _vector("chunk 0"), model=OPENAI_MODEL, namespace_id=namespace.id, top_k=5
    )
    elapsed_s = time.monotonic() - start

    assert len(results) == 5
    assert elapsed_s < 5.0


@pytest.mark.asyncio
async def test_search_ann_scores_match_analytic_cosine_similarity(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """P11: the previous suite asserted `scores == sorted(scores, reverse=True)`,
    which is a tautology: `score = 1 - distance` is monotonically decreasing
    after `ORDER BY distance`, so it held for ANY metric (L2, inner product,
    a sign error). Here the expected similarity is computed in Python and
    compared value by value, which only `1 - cosine_distance` satisfies.
    """
    ns_repo = NamespaceRepo(session_factory=app_session_factory)
    chunk_repo = MemoryChunkRepo(session_factory=app_session_factory)
    embedding_repo = ChunkEmbeddingRepo(session_factory=app_session_factory)

    namespace = await ns_repo.create(name="cosine-ns", ns_type="metier")

    query_vec = _vector("the quick brown fox")
    other_vec = _vector("a totally unrelated subject")

    expected: list[tuple[str, float]] = []
    created: dict[str, object] = {}
    for label, weight in (("near", 0.1), ("mid", 0.5), ("far", 0.9)):
        vec = _blend(query_vec, other_vec, weight)
        chunk = await chunk_repo.create(namespace_id=namespace.id, content=label)
        await embedding_repo.upsert(chunk_id=chunk.id, model=OPENAI_MODEL, embedding=vec)
        created[label] = chunk
        expected.append((label, _cosine(query_vec, vec)))

    expected.sort(key=lambda pair: pair[1], reverse=True)
    assert [label for label, _ in expected] == ["near", "mid", "far"], "fixture sanity"

    results = await embedding_repo.search_ann(
        query_vec, model=OPENAI_MODEL, namespace_id=namespace.id, top_k=3
    )

    assert [chunk.content for chunk, _score in results] == ["near", "mid", "far"]
    for (_label, expected_score), (_chunk, actual_score) in zip(expected, results, strict=True):
        assert actual_score == pytest.approx(expected_score, abs=1e-4)


@pytest.mark.asyncio
async def test_search_ann_query_actually_uses_the_hnsw_index(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regression guard for BS1: the HNSW index is declared on the
    EXPRESSION ``(embedding::vector(1536))``, so the query's ORDER BY must
    carry the same cast or Postgres cannot match the two expression trees
    and falls back to a seq scan (making ``SET LOCAL hnsw.ef_search`` a
    no-op). ``ChunkEmbeddingRepo.search_ann`` now casts the column to
    ``vector(len(query_embedding))`` before ``cosine_distance`` for exactly
    this reason. This test does not call ``search_ann`` itself (EXPLAIN
    needs a bare SELECT, not the executed statement); it mirrors the same
    WHERE/ORDER BY shape by hand so it fails loudly if that cast is ever
    dropped."""
    ns_repo = NamespaceRepo(session_factory=app_session_factory)
    chunk_repo = MemoryChunkRepo(session_factory=app_session_factory)
    embedding_repo = ChunkEmbeddingRepo(session_factory=app_session_factory)

    namespace = await ns_repo.create(name="explain-ns", ns_type="metier")
    for i in range(50):
        chunk = await chunk_repo.create(namespace_id=namespace.id, content=f"chunk {i}")
        await embedding_repo.upsert(
            chunk_id=chunk.id, model=OPENAI_MODEL, embedding=_vector(f"chunk {i}")
        )

    literal = "[" + ",".join(repr(v) for v in _vector("chunk 0")) + "]"
    async with app_session_factory() as session:
        # `enable_seqscan = off` is deliberately NOT set: the point is whether
        # the planner CAN match the index, not whether it can be forced to.
        plan_rows = await session.execute(
            sql_text(
                "EXPLAIN SELECT memory_chunks.id "
                "FROM memory_chunks JOIN chunk_embeddings "
                "ON chunk_embeddings.chunk_id = memory_chunks.id "
                "WHERE chunk_embeddings.model = :model "
                "AND memory_chunks.namespace_id = :ns "
                "AND memory_chunks.archived_at IS NULL "
                f"ORDER BY chunk_embeddings.embedding::vector(1536) <=> '{literal}' LIMIT 5"
            ),
            {"model": OPENAI_MODEL, "ns": namespace.id},
        )
        plan = "\n".join(row[0] for row in plan_rows)

    assert "chunk_embeddings_openai_hnsw" in plan, plan


@pytest.mark.asyncio
async def test_search_ann_excludes_chunks_past_their_expiry(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """IG2 — the TTL written on AC1 must actually gate AC2's search results.
    Without this, a chunk created with `ttl=1` stayed fully searchable
    forever."""
    ns_repo = NamespaceRepo(session_factory=app_session_factory)
    chunk_repo = MemoryChunkRepo(session_factory=app_session_factory)
    embedding_repo = ChunkEmbeddingRepo(session_factory=app_session_factory)

    namespace = await ns_repo.create(name="ttl-search-ns", ns_type="metier")
    now = datetime.now(UTC)

    expired = await chunk_repo.create(
        namespace_id=namespace.id,
        content="expired chunk",
        expires_at=now - timedelta(seconds=1),
    )
    await embedding_repo.upsert(
        chunk_id=expired.id, model=OPENAI_MODEL, embedding=_vector("expired chunk")
    )

    alive = await chunk_repo.create(
        namespace_id=namespace.id,
        content="alive chunk",
        expires_at=now + timedelta(days=1),
    )
    await embedding_repo.upsert(
        chunk_id=alive.id, model=OPENAI_MODEL, embedding=_vector("alive chunk")
    )

    never_expires = await chunk_repo.create(namespace_id=namespace.id, content="no ttl chunk")
    await embedding_repo.upsert(
        chunk_id=never_expires.id, model=OPENAI_MODEL, embedding=_vector("no ttl chunk")
    )

    results = await embedding_repo.search_ann(
        _vector("expired chunk"),
        model=OPENAI_MODEL,
        namespace_id=namespace.id,
        top_k=10,
        now=now,
    )

    result_ids = {chunk.id for chunk, _score in results}
    assert expired.id not in result_ids
    assert alive.id in result_ids
    assert never_expires.id in result_ids


@pytest.mark.asyncio
async def test_search_ann_uses_the_now_it_is_given_not_the_wall_clock(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A chunk that WILL expire soon must still be returned for a `now`
    that predates its expiry — proves the parameter is actually threaded
    through, not just defaulted."""
    ns_repo = NamespaceRepo(session_factory=app_session_factory)
    chunk_repo = MemoryChunkRepo(session_factory=app_session_factory)
    embedding_repo = ChunkEmbeddingRepo(session_factory=app_session_factory)

    namespace = await ns_repo.create(name="ttl-now-ns", ns_type="metier")
    expires_at = datetime.now(UTC) + timedelta(seconds=5)
    chunk = await chunk_repo.create(
        namespace_id=namespace.id, content="soon", expires_at=expires_at
    )
    await embedding_repo.upsert(chunk_id=chunk.id, model=OPENAI_MODEL, embedding=_vector("soon"))

    past_now = expires_at - timedelta(seconds=1)
    future_now = expires_at + timedelta(seconds=1)

    still_alive = await embedding_repo.search_ann(
        _vector("soon"), model=OPENAI_MODEL, namespace_id=namespace.id, top_k=10, now=past_now
    )
    already_expired = await embedding_repo.search_ann(
        _vector("soon"), model=OPENAI_MODEL, namespace_id=namespace.id, top_k=10, now=future_now
    )

    assert chunk.id in {c.id for c, _ in still_alive}
    assert chunk.id not in {c.id for c, _ in already_expired}


@pytest.mark.asyncio
async def test_upsert_persists_source_and_refreshes_it_on_re_embed(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """IG3 — provenance survives a round trip through Postgres, and
    re-embedding with a different source overwrites the old one (a
    namespace re-embedded with a real provider stops reading as mock)."""
    ns_repo = NamespaceRepo(session_factory=app_session_factory)
    chunk_repo = MemoryChunkRepo(session_factory=app_session_factory)
    embedding_repo = ChunkEmbeddingRepo(session_factory=app_session_factory)

    namespace = await ns_repo.create(name="source-ns", ns_type="metier")
    chunk = await chunk_repo.create(namespace_id=namespace.id, content="hello")

    await embedding_repo.upsert(
        chunk_id=chunk.id, model=OPENAI_MODEL, embedding=_vector("hello"), source="mock"
    )
    async with app_session_factory() as session:
        from agentive_backend.infra.db.models import ChunkEmbedding

        row = await session.get(ChunkEmbedding, (chunk.id, OPENAI_MODEL))
        assert row is not None
        assert row.source == "mock"

    await embedding_repo.upsert(
        chunk_id=chunk.id, model=OPENAI_MODEL, embedding=_vector("hello"), source="openai"
    )
    async with app_session_factory() as session:
        from agentive_backend.infra.db.models import ChunkEmbedding

        row = await session.get(ChunkEmbedding, (chunk.id, OPENAI_MODEL))
        assert row is not None
        assert row.source == "openai"
