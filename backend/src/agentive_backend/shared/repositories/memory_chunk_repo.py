"""Public API surface for :class:`MemoryChunk`. ALL DB access must go through this class."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agentive_backend.infra.db.models import MemoryChunk
from agentive_backend.shared.repositories.base import BaseRepo


class MemoryChunkRepo(BaseRepo):
    """Public API surface for MemoryChunk. ALL DB access must go through this class.

    Vector search and reranking will live in
    ``features.memory_manager.service`` and call this repo's CRUD
    primitives.
    """

    async def get_by_id(
        self, chunk_id: UUID, *, tenant_id: UUID | None = None
    ) -> MemoryChunk | None:
        async with self.with_tenant(tenant_id) as session:
            return await session.get(MemoryChunk, chunk_id)

    async def list_by_namespace(
        self,
        namespace_id: UUID,
        *,
        tenant_id: UUID | None = None,
        include_archived: bool = True,
        content_contains: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryChunk]:
        """List a namespace's chunks, newest first — Story 3.6 T7.8 (AC5).

        ``include_archived=True`` is the pre-3.6 default (this method had
        no filter at all before), kept for every existing caller. The admin
        chunk-listing UI (AC5) opts INTO the narrower ``include_archived=False``
        explicitly, mirroring ``count_by_namespace_ids``'s own live-chunk
        filter rather than inventing a second convention.

        ``content_contains`` is a case-insensitive substring match
        (``ILIKE``), ``autoescape=True`` so a caller-supplied ``%``/``_``
        is matched literally rather than as a SQL wildcard — this is the
        "tag" filter of AC5 (no real tag concept exists on ``MemoryChunk``,
        see this story's Dev Notes § Interprétation "tag").
        """
        async with self.with_tenant(tenant_id) as session:
            stmt = select(MemoryChunk).where(MemoryChunk.namespace_id == namespace_id)
            if not include_archived:
                stmt = stmt.where(MemoryChunk.archived_at.is_(None))
            if content_contains:
                stmt = stmt.where(MemoryChunk.content.icontains(content_contains, autoescape=True))
            if created_after is not None:
                stmt = stmt.where(MemoryChunk.created_at >= created_after)
            if created_before is not None:
                stmt = stmt.where(MemoryChunk.created_at <= created_before)
            stmt = stmt.order_by(MemoryChunk.created_at.desc()).limit(limit).offset(offset)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def count_by_namespace_ids(
        self,
        namespace_ids: Sequence[UUID],
        *,
        tenant_id: UUID | None = None,
        now: datetime | None = None,
    ) -> dict[UUID, int]:
        """Live (non-archived, non-expired) chunk count per namespace — Story 3.2 AC3.

        ONE grouped query for N namespaces, not an N+1 loop (perf pattern
        scrutinized in Story 3.1 code review, BS1/BS2). A namespace with zero
        live chunks is simply absent from the returned dict — callers must
        ``.get(namespace_id, 0)``.

        Filters both ``archived_at`` AND ``expires_at`` — the same pair
        ``ChunkEmbeddingRepo.search_ann`` applies. AC3 states this count is
        "cohérent avec le filtre déjà appliqué par search_ann"; until Story
        3.3's archival job runs, an expired-but-not-yet-archived chunk is
        still searchable-looking here otherwise, over-counting what a
        search would actually return (code review Story 3.2, BS2).
        """
        if not namespace_ids:
            return {}
        now = now if now is not None else datetime.now(UTC)
        async with self.with_tenant(tenant_id) as session:
            stmt = (
                select(MemoryChunk.namespace_id, func.count())
                .where(
                    MemoryChunk.namespace_id.in_(namespace_ids),
                    MemoryChunk.archived_at.is_(None),
                    (MemoryChunk.expires_at.is_(None)) | (MemoryChunk.expires_at > now),
                )
                .group_by(MemoryChunk.namespace_id)
            )
            result = await session.execute(stmt)
            return dict(result.tuples().all())

    async def find_expired(
        self,
        now: datetime,
        *,
        limit: int,
        tenant_id: UUID | None = None,
        exclude_ids: Collection[UUID] | None = None,
    ) -> list[MemoryChunk]:
        """Live chunks whose TTL has passed — Story 3.3 T1.1 (AC1).

        Exploits the partial index ``ix_memory_chunks_expires_at``
        (``WHERE archived_at IS NULL AND expires_at IS NOT NULL``, migrated
        since the initial schema) — the predicate below is written to match
        it exactly rather than adding a new index.

        ``exclude_ids`` lets the caller skip chunks it already failed on
        during this run. Without it the ``ORDER BY expires_at`` always
        re-serves the same head of the queue, so a chunk that fails
        deterministically blocks every chunk behind it, forever (code review
        Story 3.3, P2).

        The cutoff is ``<=``, not ``<``. The domain owns the boundary
        semantics (``MemoryChunk.is_expired`` is ``now >= expires_at``) and
        ``search_ann`` treats a chunk as live only while ``expires_at > now``,
        so a strict ``<`` here left a chunk sitting exactly on the instant
        invisible to search AND never selected for archival. The three
        predicates are now complementary (code review Story 3.3, P9).
        """
        async with self.with_tenant(tenant_id) as session:
            stmt = (
                select(MemoryChunk)
                .where(
                    MemoryChunk.archived_at.is_(None),
                    MemoryChunk.expires_at.is_not(None),
                    MemoryChunk.expires_at <= now,
                )
                .order_by(MemoryChunk.expires_at)
                .limit(limit)
            )
            if exclude_ids:
                stmt = stmt.where(MemoryChunk.id.not_in(exclude_ids))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def find_archivable_in_namespace(
        self,
        namespace_id: UUID,
        threshold: datetime,
        *,
        limit: int,
        tenant_id: UUID | None = None,
        now: datetime | None = None,
        exclude_ids: Collection[UUID] | None = None,
    ) -> list[MemoryChunk]:
        """Live, non-expired chunks old enough for ``archive_after_seconds``
        — Story 3.3 T1.3 (AC3).

        ``threshold`` is ``now - archive_after_seconds``, computed by the
        caller (:class:`MemoryArchivalWorker`) from the namespace's
        :class:`RetentionPolicy` — this repo stays agnostic of the domain
        layer, same as every other method here. ``now`` defaults to the
        current time (mirrors ``search_ann``/``count_by_namespace_ids``)
        but should be passed explicitly by the worker so every chunk of a
        single run is evaluated against one consistent instant.

        ``exclude_ids`` serves the same head-of-line purpose as in
        :meth:`find_expired` (code review Story 3.3, P2).
        """
        now = now if now is not None else datetime.now(UTC)
        async with self.with_tenant(tenant_id) as session:
            stmt = (
                select(MemoryChunk)
                .where(
                    MemoryChunk.namespace_id == namespace_id,
                    MemoryChunk.archived_at.is_(None),
                    (MemoryChunk.expires_at.is_(None)) | (MemoryChunk.expires_at > now),
                    MemoryChunk.created_at <= threshold,
                )
                .order_by(MemoryChunk.created_at)
                .limit(limit)
            )
            if exclude_ids:
                stmt = stmt.where(MemoryChunk.id.not_in(exclude_ids))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def mark_archived(
        self,
        chunk_ids: Sequence[UUID],
        *,
        archived_at: datetime,
        tenant_id: UUID | None = None,
    ) -> int:
        """Convenience wrapper — self-managed transaction (Story 3.3 T1.2).

        Use :meth:`mark_archived_in_session` from inside an existing
        transaction when the UPDATE must be atomic with publishing
        ``MemoryChunkArchivedEvent`` (mirror ``NamespaceRepo.create`` /
        ``.create_in_session``, Story 3.2 AC1).
        """
        async with self.with_tenant(tenant_id) as session:
            return await self.mark_archived_in_session(session, chunk_ids, archived_at=archived_at)

    async def mark_archived_in_session(
        self,
        session: AsyncSession,
        chunk_ids: Sequence[UUID],
        *,
        archived_at: datetime,
    ) -> int:
        """UPDATE inside the caller's transaction — caller owns commit.

        ``AND archived_at IS NULL`` avoids clobbering a value already set by
        a concurrent/previous run. The returned count is rows ACTUALLY
        updated (not ``len(chunk_ids)``) so a caller feeding the archival
        metric stays exact under that race (Story 3.3 T1.2).
        """
        if not chunk_ids:
            return 0
        stmt = (
            update(MemoryChunk)
            .where(MemoryChunk.id.in_(chunk_ids), MemoryChunk.archived_at.is_(None))
            .values(archived_at=archived_at)
        )
        result = await session.execute(stmt)
        # `getattr` rather than a direct `.rowcount` access — `Result` (the
        # generic return type of `session.execute()`) doesn't declare it,
        # only the concrete `CursorResult` a DML statement actually returns
        # does (same defensive access as `OutboxWorker._mark_processed`).
        rowcount = getattr(result, "rowcount", None)
        return rowcount if isinstance(rowcount, int) else 0

    async def create(
        self,
        *,
        namespace_id: UUID,
        content: str,
        metadata: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
        expires_at: datetime | None = None,
        tenant_id: UUID | None = None,
    ) -> MemoryChunk:
        async with self.with_tenant(tenant_id) as session:
            chunk = MemoryChunk(
                namespace_id=namespace_id,
                content=content,
                metadata_=metadata if metadata is not None else {},
                ttl_seconds=ttl_seconds,
                expires_at=expires_at,
                tenant_id=tenant_id,
            )
            session.add(chunk)
            await session.flush()
            await session.refresh(chunk)
            return chunk

    async def delete_by_id(self, chunk_id: UUID, *, tenant_id: UUID | None = None) -> None:
        """Hard-delete a chunk row (cascades to ``chunk_embeddings``).

        Compensating action for the service layer (code review Story 3.1,
        IG1): if writing a chunk's embedding fails right after the chunk row
        itself was committed, the caller uses this to remove the now-orphan
        row rather than leave one with no vector — the `INNER JOIN` in
        ``search_ann`` can never surface it, so it would otherwise sit in
        the table forever, invisible and unrecoverable without this.

        Not the same thing as ``archived_at`` — that is a domain lifecycle
        state for chunks that DID succeed. This is "the write never really
        happened", so a hard delete (not a soft-delete flag) is correct.
        """
        async with self.with_tenant(tenant_id) as session:
            await session.execute(delete(MemoryChunk).where(MemoryChunk.id == chunk_id))
