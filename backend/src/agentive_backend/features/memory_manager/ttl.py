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
from enum import Enum, auto
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
from agentive_backend.shared.repositories.namespace_repo import NAMESPACE_LISTING_SAFETY_CAP

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

# Ceiling on the per-run set of chunks that failed to archive. Failed ids are
# excluded from subsequent queries of the same run so they never block the
# chunks behind them, but the set feeds a SQL `NOT IN`, so it cannot grow
# without bound. Hitting this cap is the *real* "this phase is stuck" signal
# (code review Story 3.3, P2).
_MAX_FAILED_PER_RUN = 10_000

# Retry cadence after a FAILED pass, exponential and capped — mirror of
# `OutboxWorker._RECONNECT_BACKOFF_S`. Without it, a run that dies on a cold
# pool or a failover leaves the TTL unenforced for a full `interval_s`
# (24 h by default), with a single log line as the only signal
# (code review Story 3.3, P8).
_RETRY_BACKOFF_S: tuple[float, ...] = (60.0, 300.0, 900.0)

# Cooperative window `stop()` grants the loop to unwind on its own before
# cancelling it, then the cap on the cancellation itself. The two together
# stay under the 5 s the lifespan's sibling drain block already allows
# itself, so a hung pass can never delay shutdown indefinitely
# (code review Story 3.3, P5).
_STOP_GRACE_S = 2.0
_STOP_CANCEL_TIMEOUT_S = 3.0

ArchivalReason = Literal["ttl_expired", "archive_after_seconds"]


class _ArchiveOutcome(Enum):
    """Result of one :meth:`MemoryArchivalWorker._archive_one` attempt.

    Three distinct states that an ``int`` return used to collapse into ``0``,
    which made a benign concurrent race indistinguishable from a hard failure
    and aborted the whole phase (code review Story 3.3, P1).
    """

    ARCHIVED = auto()
    #: Another run (or another replica) got there first. Nominal, not an error:
    #: `archived_at` is now set, so the row drops out of the next query by
    #: itself and the loop keeps making progress.
    ALREADY_ARCHIVED = auto()
    FAILED = auto()


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
        """Stop the loop task, under a hard time cap. Idempotent.

        Cooperative first: ``run_once`` checks ``_stopping`` between batches
        (T7) so a healthy pass unwinds without a cancellation landing in the
        middle of a transaction. Cancellation is the fallback, and it too is
        capped: ``await self._task`` used to be unbounded, so a cancellation
        delivered during a rollback on a dead connection froze the whole
        shutdown (code review Story 3.3, P5).
        """
        self._stopping.set()
        task, self._task = self._task, None
        if task is not None:
            finished = task.done()
            if not finished:
                done, _pending = await asyncio.wait({task}, timeout=_STOP_GRACE_S)
                if not done:
                    task.cancel()
                    done, _pending = await asyncio.wait({task}, timeout=_STOP_CANCEL_TIMEOUT_S)
                finished = bool(done)
            # Reported even when the task was ALREADY done on entry: a loop
            # that died on its own means the worker had silently stopped
            # archiving, and shutdown is the last chance to say so.
            self._report_shutdown(task, finished=finished)
        _log.info("memory_manager.archival_worker_stopped")

    @staticmethod
    def _report_shutdown(task: asyncio.Task[None], *, finished: bool) -> None:
        """Surface how the loop task ended, without ever raising.

        `contextlib.suppress(Exception)` was the wrong instrument here twice
        over: it does not catch `CancelledError` (a `BaseException`), and it
        hides genuine programmer errors raised during shutdown. Mirror
        `OutboxWorker.stop`'s posture instead — expected cancellation is
        silent, anything else is logged (code review Story 3.3, P5).
        """
        if not finished:
            _log.warning(
                "memory_manager.archival_worker_stop_timeout",
                timeout_s=_STOP_GRACE_S + _STOP_CANCEL_TIMEOUT_S,
            )
            return
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _log.error("memory_manager.archival_worker_shutdown_error", exc_info=error)

    async def _loop(self) -> None:
        consecutive_failures = 0
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception:
                consecutive_failures += 1
                _log.exception(
                    "memory_manager.archival_run_failed",
                    consecutive_failures=consecutive_failures,
                )
            else:
                consecutive_failures = 0
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._next_delay(consecutive_failures)
                )

    def _next_delay(self, consecutive_failures: int) -> float:
        """Nominal cadence after a healthy pass, shortened backoff after a
        failed one (code review Story 3.3, P8)."""
        if consecutive_failures == 0:
            return self._interval_s
        backoff = _RETRY_BACKOFF_S[min(consecutive_failures, len(_RETRY_BACKOFF_S)) - 1]
        # Never wait LONGER than the nominal cadence: with a short
        # `interval_s` (tests, or a future config) an unclamped backoff would
        # make a failing worker slower than a healthy one, which is backwards.
        return min(backoff, self._interval_s)

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
        if len(namespaces) >= NAMESPACE_LISTING_SAFETY_CAP:
            # Same signal `MemoryManagerService.list_namespaces` raises — the
            # Story 3.2 review (BS3) requires a truncation never be silent.
            # Here it matters twice over: a truncated map means AC3 skips the
            # namespaces past the cap entirely, and AC1 cannot resolve their
            # chunks' namespace name (code review Story 3.3, P3).
            _log.warning(
                "memory_manager.archival_namespace_listing_hit_safety_cap",
                cap=NAMESPACE_LISTING_SAFETY_CAP,
            )
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

    def _interrupted(self, phase: str, archived_count: int) -> bool:
        """True once ``stop()`` has been requested — checked between batches
        and between chunks so a long pass unwinds on its own.

        Without this, ``stop()`` had no choice but to cancel abruptly,
        possibly mid-transaction. Archival is resumable by construction (the
        next pass re-selects whatever is still unarchived), so abandoning a
        partially drained queue costs nothing (code review Story 3.3, P7).
        """
        if not self._stopping.is_set():
            return False
        _log.info("memory_manager.archival_run_interrupted", phase=phase, archived=archived_count)
        return True

    # ─────────────────────────── AC1 — TTL expiry ─────────────────────

    async def _archive_expired(
        self,
        now: datetime,
        *,
        namespace_by_id: dict[UUID, Namespace],
        correlation_id: UUID,
    ) -> int:
        archived_count = 0
        failed_ids: set[UUID] = set()
        for _ in range(_MAX_BATCHES_PER_RUN):
            chunks = await self._memory_chunk_repo.find_expired(
                now, limit=self._batch_size, exclude_ids=failed_ids
            )
            if not chunks:
                break
            for chunk in chunks:
                if self._stopping.is_set():
                    break
                outcome = await self._archive_one(
                    chunk,
                    now=now,
                    reason="ttl_expired",
                    namespace_by_id=namespace_by_id,
                    correlation_id=correlation_id,
                )
                if outcome is _ArchiveOutcome.ARCHIVED:
                    archived_count += 1
                elif outcome is _ArchiveOutcome.FAILED:
                    failed_ids.add(chunk.id)
            if self._interrupted("ttl_expired", archived_count):
                break
            if len(failed_ids) >= _MAX_FAILED_PER_RUN:
                _log.error(
                    "memory_manager.archival_failure_cap_reached",
                    phase="ttl_expired",
                    failed=len(failed_ids),
                )
                break
            if len(chunks) < self._batch_size:
                # Short batch: the queue is drained. Breaking here (rather
                # than looping for one more empty fetch) also keeps the
                # `else` branch below honest — it must mean "we really ran
                # out of batches", not "the last batch happened to be full"
                # (code review Story 3.3, P14).
                break
        else:
            _log.warning(
                "memory_manager.archival_max_batches_reached",
                phase="ttl_expired",
                max_batches=_MAX_BATCHES_PER_RUN,
            )
        if failed_ids:
            _log.warning(
                "memory_manager.archival_chunks_failed",
                phase="ttl_expired",
                failed=len(failed_ids),
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
            if self._interrupted("archive_after_seconds", archived_count):
                break
            # The guard wraps the WHOLE per-namespace body, not just
            # `from_mapping`. `timedelta()` raises `OverflowError` on an
            # absurd `archive_after_seconds`, and the DB call below raises on
            # any transient error — both used to escape to `_loop` and kill
            # the AC3 phase for every remaining namespace, for 24h
            # (code review Story 3.3, P4).
            try:
                policy = RetentionPolicy.from_mapping(namespace.retention_policy)
            except AttributeError, TypeError, ValueError:
                # A corrupt `retention_policy` JSONB is a configuration
                # problem, not an incident: warn without a traceback and move
                # on. (`OverflowError` is deliberately NOT listed — it cannot
                # come from `from_mapping`, which only does `raw.get()` plus
                # two comparisons. Its real source is the `timedelta()` below,
                # now covered by the outer guard.)
                _log.warning(
                    "memory_manager.archival_namespace_retention_policy_invalid",
                    namespace=namespace.name,
                )
                continue
            if policy.archive_after_seconds is None:
                continue
            try:
                threshold = now - timedelta(seconds=policy.archive_after_seconds)
                archived_count += await self._archive_namespace_by_age(
                    namespace, threshold, now=now, correlation_id=correlation_id
                )
            except Exception:
                _log.exception(
                    "memory_manager.archival_namespace_failed",
                    namespace=namespace.name,
                )
                continue
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
        failed_ids: set[UUID] = set()
        for _ in range(_MAX_BATCHES_PER_RUN):
            chunks = await self._memory_chunk_repo.find_archivable_in_namespace(
                namespace.id, threshold, limit=self._batch_size, now=now, exclude_ids=failed_ids
            )
            if not chunks:
                break
            for chunk in chunks:
                if self._stopping.is_set():
                    break
                outcome = await self._archive_one(
                    chunk,
                    now=now,
                    reason="archive_after_seconds",
                    namespace_by_id=namespace_by_id,
                    correlation_id=correlation_id,
                )
                if outcome is _ArchiveOutcome.ARCHIVED:
                    archived_count += 1
                elif outcome is _ArchiveOutcome.FAILED:
                    failed_ids.add(chunk.id)
            if self._stopping.is_set():
                # The caller (`_archive_by_age`) logs the interruption once for
                # the whole phase — breaking silently here avoids one line per
                # namespace still queued.
                break
            if len(failed_ids) >= _MAX_FAILED_PER_RUN:
                _log.error(
                    "memory_manager.archival_failure_cap_reached",
                    phase="archive_after_seconds",
                    namespace=namespace.name,
                    failed=len(failed_ids),
                )
                break
            if len(chunks) < self._batch_size:
                break
        else:
            _log.warning(
                "memory_manager.archival_max_batches_reached",
                phase="archive_after_seconds",
                namespace=namespace.name,
                max_batches=_MAX_BATCHES_PER_RUN,
            )
        if failed_ids:
            _log.warning(
                "memory_manager.archival_chunks_failed",
                phase="archive_after_seconds",
                namespace=namespace.name,
                failed=len(failed_ids),
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
    ) -> _ArchiveOutcome:
        """Archive a single chunk; never raises (T5.3 — per-chunk resilience).

        The caller distinguishes the three outcomes: only ``ARCHIVED`` counts
        towards the run summary and the Prometheus counter, only ``FAILED``
        excludes the chunk from the rest of the run.
        """
        try:
            namespace = await self._resolve_namespace(chunk, namespace_by_id)
            if namespace is None:
                return _ArchiveOutcome.FAILED
            async with self._memory_chunk_repo.with_tenant(None) as session:
                archived = await self._memory_chunk_repo.mark_archived_in_session(
                    session, [chunk.id], archived_at=now
                )
                if archived == 0:
                    return _ArchiveOutcome.ALREADY_ARCHIVED
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
            return _ArchiveOutcome.ARCHIVED
        except Exception:
            _log.exception(
                "memory_manager.archive_chunk_failed",
                chunk_id=str(chunk.id),
                namespace_id=str(chunk.namespace_id),
                reason=reason,
            )
            return _ArchiveOutcome.FAILED

    async def _resolve_namespace(
        self, chunk: MemoryChunk, namespace_by_id: dict[UUID, Namespace]
    ) -> Namespace | None:
        """Resolve the chunk's namespace, falling back to a direct lookup.

        The run-level map is built from ``list_all()``, which is capped at
        ``NAMESPACE_LISTING_SAFETY_CAP``. A subscript used to raise ``KeyError``
        past that cap, which the caller's blanket ``except`` swallowed into an
        undiagnosable "archive failed" — so those chunks were never archived,
        run after run. The name is only needed for the event payload, and
        `MemoryChunkArchivedEvent.namespace` is `min_length=1`, so there is no
        safe placeholder: look it up and cache it (code review Story 3.3, P3).
        """
        namespace = namespace_by_id.get(chunk.namespace_id)
        if namespace is not None:
            return namespace
        namespace = await self._namespace_repo.get_by_id(chunk.namespace_id)
        if namespace is None:
            _log.error(
                "memory_manager.archival_namespace_missing",
                chunk_id=str(chunk.id),
                namespace_id=str(chunk.namespace_id),
            )
            return None
        namespace_by_id[namespace.id] = namespace
        return namespace


__all__ = ["ArchivalRunSummary", "MemoryArchivalWorker"]
