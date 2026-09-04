"""Public API surface for :class:`ChunkEmbedding`. ALL DB access must go through this class."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import cast, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from agentive_backend.infra.db.models import ChunkEmbedding, MemoryChunk
from agentive_backend.shared.repositories.base import BaseRepo

# pgvector accepts `hnsw.ef_search` in [1, 1000]; anything outside makes
# Postgres reject the SET LOCAL with an unmapped error.
EF_SEARCH_MIN = 1
EF_SEARCH_MAX = 1000


class ChunkEmbeddingRepo(BaseRepo):
    """Public API surface for ChunkEmbedding. ALL DB access must go through this class.

    Primary key is composite ``(chunk_id, model)`` — a chunk can have
    multiple embeddings (one per model). Vector dimension varies per model
    and is enforced by partial HNSW indexes per model in the migration.
    """

    async def get(
        self,
        chunk_id: UUID,
        model: str,
        *,
        tenant_id: UUID | None = None,
    ) -> ChunkEmbedding | None:
        async with self.with_tenant(tenant_id) as session:
            return await session.get(ChunkEmbedding, (chunk_id, model))

    async def list_by_chunk(
        self, chunk_id: UUID, *, tenant_id: UUID | None = None
    ) -> list[ChunkEmbedding]:
        async with self.with_tenant(tenant_id) as session:
            stmt = select(ChunkEmbedding).where(ChunkEmbedding.chunk_id == chunk_id)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def upsert(
        self,
        *,
        chunk_id: UUID,
        model: str,
        embedding: Sequence[float],
        tenant_id: UUID | None = None,
        source: str | None = None,
    ) -> None:
        """Insert or update the embedding for ``(chunk_id, model)``.

        Uses ``ON CONFLICT (chunk_id, model) DO UPDATE`` so re-embedding a
        chunk replaces the old vector. ``tenant_id`` is set on insert and
        preserved on update (the WITH CHECK RLS clause prevents cross-tenant
        update, so we never overwrite another tenant's row).

        ``source`` is the producing :attr:`Embedder.provider_name`
        ("openai", "mock", ...) — a provenance trail, not a search filter
        (code review Story 3.1, IG3). Re-embedding also refreshes it, so a
        namespace re-embedded with a real provider stops reading as mock.
        """
        async with self.with_tenant(tenant_id) as session:
            stmt = pg_insert(ChunkEmbedding).values(
                chunk_id=chunk_id,
                model=model,
                embedding=list(embedding),
                tenant_id=tenant_id,
                source=source,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[ChunkEmbedding.chunk_id, ChunkEmbedding.model],
                set_={"embedding": list(embedding), "source": source},
            )
            await session.execute(stmt)

    async def search_ann(
        self,
        query_embedding: Sequence[float],
        *,
        model: str,
        namespace_id: UUID,
        top_k: int,
        tenant_id: UUID | None = None,
        ef_search: int = 100,
        now: datetime | None = None,
    ) -> list[tuple[MemoryChunk, float]]:
        """ANN search over ``chunk_embeddings`` via the HNSW cosine index (Story 3.1 T2.3).

        Excludes archived chunks AND expired ones (``expires_at`` in the
        past relative to ``now``, default :func:`datetime.now`; code review
        Story 3.1, IG2). A TTL'd chunk otherwise stayed fully searchable
        forever; this only hides it from search results, it does not
        reclaim storage (that's still Story 3.3's job).

        Returns ``(chunk, score)`` pairs sorted by descending similarity,
        where ``score = 1.0 - cosine_distance`` (cosine distance range
        ``[0, 2]`` : see Story 1.3 Dev Notes § Recherche ANN).

        The comparison casts the (dimension-less) ``embedding`` column to
        ``vector(len(query_embedding))`` before computing the distance
        (code review Story 3.1, BS1). Each HNSW index is declared on the
        expression ``(embedding::vector(N))`` (migration
        ``20260419_000000_initial``); Postgres only matches an index to a
        query when the two expression trees are syntactically identical
        after parse analysis, and a bare ``embedding <=> $1`` never carries
        that cast, so the planner fell back to a sequential scan on every
        call (confirmed via ``EXPLAIN``, both empirically here and by the
        Acceptance Auditor). Deriving the cast width from the query vector
        itself, rather than a separate per-model dimension table, keeps a
        single source of truth: it is exactly the dimension the caller
        already committed to by choosing ``model`` and building
        ``query_embedding`` for it.

        ``ef_search`` is ALWAYS an internal ``int`` set by the caller
        (never an HTTP input in this story) — inlined into the SQL string
        because Postgres ``SET LOCAL`` rejects bind placeholders for its
        value (Story 1.3 Debug Log #2). The guards below are the only thing
        standing between this and a SQL-injectable string if a future caller
        ever threads a request-derived value through, so they cover more
        than the original ``isinstance`` did (code review Story 3.1, P6):

        * ``bool`` is an ``int`` subclass, and ``SET LOCAL ... = True`` is
          not valid Postgres;
        * ``0`` and negatives are ``int`` but outside pgvector's range;
        * an ``int`` subclass overriding ``__str__`` would inject through
          the f-string, so the value is re-coerced with ``int()`` at the
          interpolation site rather than trusted.

        ``hnsw.iterative_scan`` is set to ``'strict_order'`` (code review
        Story 3.1, BS2). ``namespace_id``, ``archived_at`` and ``expires_at``
        are plain WHERE filters, not part of the partial index predicate
        (only ``model`` is), so pgvector applies them AFTER the HNSW graph
        walk: with iterative scan off, a namespace that is a small slice of
        the model's overall corpus can see its matches filtered out of the
        ``ef_search`` candidate window entirely, silently returning fewer
        than ``top_k`` rows even though enough exist elsewhere in the table
        (confirmed empirically: an index scan that finds 0 of 5 matching
        rows within its candidate window). ``strict_order`` makes pgvector
        keep expanding the graph walk until the filtered result count
        satisfies the limit or ``hnsw.max_scan_tuples`` (pgvector default
        20000) is hit, without relaxing the exact-distance ordering this
        module's tests rely on (``relaxed_order`` trades that ordering
        guarantee for speed, which is not this story's requirement). This
        is a mitigation, not a hard guarantee : a namespace that is a
        vanishing fraction of an enormous shared-model corpus can still be
        under-filled once the scan budget is exhausted. Closing that
        residual gap would need either raising ``hnsw.max_scan_tuples``
        per call or an exact-search fallback keyed on whether the ANN pass
        under-filled ; deliberately not done here, kept as a documented
        limitation rather than added complexity for a case not observed at
        this story's scale (see Story 1.3 ADR).
        """
        if isinstance(ef_search, bool) or not isinstance(ef_search, int):
            raise TypeError(f"ef_search must be an int, got {type(ef_search).__name__}")
        if not EF_SEARCH_MIN <= ef_search <= EF_SEARCH_MAX:
            raise ValueError(
                f"ef_search must be in [{EF_SEARCH_MIN}, {EF_SEARCH_MAX}], got {ef_search}"
            )
        now = now if now is not None else datetime.now(UTC)

        async with self.with_tenant(tenant_id) as session:
            await session.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))
            await session.execute(text("SET LOCAL hnsw.iterative_scan = 'strict_order'"))
            embedding_expr = cast(ChunkEmbedding.embedding, Vector(len(query_embedding)))
            distance = embedding_expr.cosine_distance(list(query_embedding))
            stmt = (
                select(MemoryChunk, distance.label("distance"))
                .join(ChunkEmbedding, ChunkEmbedding.chunk_id == MemoryChunk.id)
                .where(
                    ChunkEmbedding.model == model,
                    MemoryChunk.namespace_id == namespace_id,
                    MemoryChunk.archived_at.is_(None),
                    # A TTL'd chunk otherwise stays fully searchable forever
                    # — nothing else in this story enforces expiry at read
                    # time (code review Story 3.1, IG2). Physical reclamation
                    # of expired rows is still Story 3.3's job; this only
                    # hides them from search.
                    (MemoryChunk.expires_at.is_(None)) | (MemoryChunk.expires_at > now),
                )
                # Tie-break: identical content embeds to an identical vector,
                # so `distance` alone leaves the order up to the plan and two
                # identical requests could return different rows at the same
                # `top_k` (code review Story 3.1, P7).
                .order_by("distance", MemoryChunk.created_at.desc(), MemoryChunk.id)
                .limit(top_k)
            )
            result = await session.execute(stmt)
            return [(chunk, 1.0 - distance) for chunk, distance in result.all()]
