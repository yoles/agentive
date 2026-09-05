"""MemoryArchivalWorker — automatic chunk archival (Story 3.3 AC1/AC3).

Structural mirror of :class:`~agentive_backend.shared.event_bus.outbox.OutboxWorker`
(the repo's only precedent for a background worker): ``start()``/``stop()``,
an ``asyncio`` loop (immediate pass, then ``asyncio.sleep(interval_s)``),
structlog, per-item resilience. Deliberately NOT built on ``apscheduler``
(present in ``pyproject.toml`` but reserved for Epic 7 M11 Scheduler,
architecture.md:2086, decision G2) — a daily loop is exactly what
``OutboxWorker``'s shape already covers without a second scheduling
mechanism in the codebase.

Two independent selection paths, run every pass:

1. **AC1** — chunks whose ``expires_at`` has passed
   (:meth:`MemoryChunkRepo.find_expired`), reason ``"ttl_expired"``.
2. **AC3** — chunks not yet expired but older than their namespace's
   ``archive_after_seconds`` (:meth:`MemoryChunkRepo.find_archivable_in_namespace`),
   reason ``"archive_after_seconds"``. No namespace has a default for this
   field (cf. ``RetentionPolicy.default_for_type``'s explicit "Story 3.3
   owns picking its own default" — this story deliberately picks none
   either), so this path only activates for a namespace an operator
   configured explicitly via ``POST /memory/namespaces``.

Each chunk is archived in its own transaction — the ``archived_at`` UPDATE
and the ``MemoryChunkArchivedEvent`` publish share one transaction (mirror
``MemoryManagerService.create_namespace``'s atomicity, Story 3.2 AC1) — so a
single poison chunk's failure never rolls back its siblings, and never
leaves an archived row without its audit event either.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from agentive_backend.features.memory_manager.domain.value_objects import RetentionPolicy
from agentive_backend.features.memory_manager.metrics import (
    MEMORY_MANAGER_CHUNKS_ARCHIVED_TOTAL,
)
from agentive_backend.shared.contracts.events import MemoryChunkArchivedEvent
from agentive_backend.shared.event_bus import notify_best_effort, publish
from agentive_backend.shared.logging import get_logger
from agentive_backend.shared.repositories import MemoryChunkRepo, NamespaceRepo

if TYPE_CHECKING:
    # `AsyncSession`/`async_sessionmaker` are NOT imported here on purpose —
    # `import-linter` Contract 3 forbids `features/*` from importing
    # `sqlalchemy` even in a `TYPE_CHECKING` block (it's a static AST edge,
    # not a runtime one). `session_factory` below is `Any`; the concrete
    # type lives one layer down, at `shared.repositories.BaseRepo.__init__`,
    # which this worker forwards it to unchanged.
    from agentive_backend.infra.db.models import MemoryChunk, Namespace

_log = get_logger(__name__)

# Safety cap on the number of batches drained per selection path, per run —
# same defensive posture as `NamespaceRepo.NAMESPACE_LISTING_SAFETY_CAP`
# (Story 3.2 BS3): a run that would otherwise process millions of rows in
# one pass is capped, with a warning, rather than left unbounded.
_MAX_BATCHES_PER_RUN = 1_000

ArchivalReason = Literal["ttl_expired", "archive_after_seconds"]


@dataclass(frozen=True, slots=True)
class ArchivalRunSummary:
    """Result of one :meth:`MemoryArchivalWorker.run_once` pass."""

    ttl_expired_count: int
    archive_after_seconds_count: int


class MemoryArchivalWorker:
    """Background job — archives expired / aged-out chunks (never hard-deletes)."""

    def __init__(
        self,
        session_factory: Any,
        *,
        interval_s: float = 86_400.0,
        batch_size: int = 1_000,
    ) -> None:
        self._interval_s = interval_s
        self._batch_size = batch_size
        self._memory_chunk_repo = MemoryChunkRepo(session_factory=session_factory)
        self._namespace_repo = NamespaceRepo(session_factory=session_factory)
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    # ─────────────────────────── lifecycle ───────────────────────────

    async def start(self) -> None:
        """Spawn the loop task (immediate pass, then a daily cadence).

        Not idempotent — mirrors ``OutboxWorker.start()``'s posture so a
        misuse can't leak a duplicate task.
        """
        if self._task is not None:
            raise RuntimeError(
                "MemoryArchivalWorker.start() called twice; call stop() first if restarting."
            )
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="memory-archival-worker")
        _log.info("memory_manager.archival_worker_started", interval_s=self._interval_s)

    async def stop(self) -> None:
        """Cancel the loop task. Idempotent."""
        self._stopping.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        _log.info("memory_manager.archival_worker_stopped")

    async def _loop(self) -> None:
        try:
            while not self._stopping.is_set():
                try:
                    await self.run_once()
                except Exception:
                    _log.exception("memory_manager.archival_run_failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_s)
        except asyncio.CancelledError:
            raise

    # ─────────────────────────── run ──────────────────────────────────

    async def run_once(self, now: datetime | None = None) -> ArchivalRunSummary:
        """Run both selection paths once. Safe to call directly in tests —
        never sleeps, never touches ``start()``/``stop()`` state."""
        now = now if now is not None else datetime.now(UTC)
        start = time.monotonic()
        correlation_id = uuid4()

        # T1.4 — one `list_all()` call serves both selection paths: it
        # resolves `namespace_id -> name` for AC1's event payload (below)
        # AND finds the (few) namespaces with `archive_after_seconds` set
        # for AC3. Namespace volume stays low (cf.
        # `NAMESPACE_LISTING_SAFETY_CAP`), so one Python-side pass over it
        # is cheaper than a per-chunk namespace lookup.
        namespaces = await self._namespace_repo.list_all()
        namespace_by_id: dict[UUID, Namespace] = {ns.id: ns for ns in namespaces}

        ttl_expired_count = await self._archive_expired(
            now, namespace_by_id=namespace_by_id, correlation_id=correlation_id
        )
        archive_after_seconds_count = await self._archive_by_age(
            now, namespaces=namespaces, correlation_id=correlation_id
        )

        summary = ArchivalRunSummary(
            ttl_expired_count=ttl_expired_count,
            archive_after_seconds_count=archive_after_seconds_count,
        )
        _log.info(
            "memory_manager.archival_run_completed",
            ttl_expired_count=ttl_expired_count,
            archive_after_seconds_count=archive_after_seconds_count,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        return summary

    # ─────────────────────────── AC1 — TTL expiry ─────────────────────

    async def _archive_expired(
        self,
        now: datetime,
        *,
        namespace_by_id: dict[UUID, Namespace],
        correlation_id: UUID,
    ) -> int:
        archived_count = 0
        for _ in range(_MAX_BATCHES_PER_RUN):
            chunks = await self._memory_chunk_repo.find_expired(now, limit=self._batch_size)
            if not chunks:
                break
            batch_archived = 0
            for chunk in chunks:
                batch_archived += await self._archive_one(
                    chunk,
                    now=now,
                    reason="ttl_expired",
                    namespace_by_id=namespace_by_id,
                    correlation_id=correlation_id,
                )
            archived_count += batch_archived
            if batch_archived == 0:
                # Every chunk in this batch failed to archive (see
                # `_archive_one`'s per-chunk try/except) — the same batch
                # would just be re-fetched (still `archived_at IS NULL`)
                # next iteration. Stop now rather than spin until the cap.
                _log.error(
                    "memory_manager.archival_batch_stuck",
                    phase="ttl_expired",
                    batch_size=len(chunks),
                )
                break
        else:
            _log.warning(
                "memory_manager.archival_max_batches_reached",
                phase="ttl_expired",
                max_batches=_MAX_BATCHES_PER_RUN,
            )
        return archived_count

    # ─────────────────────────── AC3 — age-based ──────────────────────

    async def _archive_by_age(
        self,
        now: datetime,
        *,
        namespaces: list[Namespace],
        correlation_id: UUID,
    ) -> int:
        archived_count = 0
        for namespace in namespaces:
            try:
                policy = RetentionPolicy.from_mapping(namespace.retention_policy)
            except AttributeError, TypeError, ValueError, OverflowError:
                _log.warning(
                    "memory_manager.archival_namespace_retention_policy_invalid",
                    namespace=namespace.name,
                )
                continue
            if policy.archive_after_seconds is None:
                continue
            threshold = now - timedelta(seconds=policy.archive_after_seconds)
            archived_count += await self._archive_namespace_by_age(
                namespace, threshold, now=now, correlation_id=correlation_id
            )
        return archived_count

    async def _archive_namespace_by_age(
        self,
        namespace: Namespace,
        threshold: datetime,
        *,
        now: datetime,
        correlation_id: UUID,
    ) -> int:
        namespace_by_id = {namespace.id: namespace}
        archived_count = 0
        for _ in range(_MAX_BATCHES_PER_RUN):
            chunks = await self._memory_chunk_repo.find_archivable_in_namespace(
                namespace.id, threshold, limit=self._batch_size, now=now
            )
            if not chunks:
                break
            batch_archived = 0
            for chunk in chunks:
                batch_archived += await self._archive_one(
                    chunk,
                    now=now,
                    reason="archive_after_seconds",
                    namespace_by_id=namespace_by_id,
                    correlation_id=correlation_id,
                )
            archived_count += batch_archived
            if batch_archived == 0:
                _log.error(
                    "memory_manager.archival_batch_stuck",
                    phase="archive_after_seconds",
                    namespace=namespace.name,
                    batch_size=len(chunks),
                )
                break
        else:
            _log.warning(
                "memory_manager.archival_max_batches_reached",
                phase="archive_after_seconds",
                namespace=namespace.name,
                max_batches=_MAX_BATCHES_PER_RUN,
            )
        return archived_count

    # ─────────────────────────── per-chunk unit ───────────────────────

    async def _archive_one(
        self,
        chunk: MemoryChunk,
        *,
        now: datetime,
        reason: ArchivalReason,
        namespace_by_id: dict[UUID, Namespace],
        correlation_id: UUID,
    ) -> int:
        """Archive a single chunk; never raises (T5.3 — per-chunk resilience).

        Returns 1 if this call actually archived the chunk, 0 otherwise
        (already archived by a concurrent/previous run, missing namespace,
        or any other failure) — the caller sums this directly into both
        the run summary and the Prometheus counter.
        """
        try:
            namespace = namespace_by_id[chunk.namespace_id]
            async with self._memory_chunk_repo.with_tenant(None) as session:
                archived = await self._memory_chunk_repo.mark_archived_in_session(
                    session, [chunk.id], archived_at=now
                )
                if archived == 0:
                    return 0
                event = MemoryChunkArchivedEvent(
                    chunk_id=chunk.id,
                    namespace_id=chunk.namespace_id,
                    namespace=namespace.name,
                    reason=reason,
                )
                event_id = await publish(
                    MemoryChunkArchivedEvent.event_type,
                    event,
                    session=session,
                    correlation_id=correlation_id,
                )
                # Commit happens at `with_tenant`'s `__aexit__`.
            await notify_best_effort(event_id, MemoryChunkArchivedEvent.event_type)
            MEMORY_MANAGER_CHUNKS_ARCHIVED_TOTAL.labels(reason=reason).inc()
            return 1
        except Exception:
            _log.exception(
                "memory_manager.archive_chunk_failed",
                chunk_id=str(chunk.id),
                reason=reason,
            )
            return 0


__all__ = ["ArchivalRunSummary", "MemoryArchivalWorker"]
