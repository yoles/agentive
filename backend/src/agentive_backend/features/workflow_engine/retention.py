"""CheckpointRetentionWorker — purges old checkpoint blobs, alerts on
immortal ``paused`` runs (Story 4.10 AC1/AC2).

Structural mirror of ``MemoryArchivalWorker`` (Story 3.3,
``features/memory_manager/ttl.py``) and ``WorkflowRecoveryWorker`` — the
repo's established shape for "background worker started by lifespan,
``start()``/``stop()`` with grace + cancel bound, immediate pass then
periodic cadence." Deliberately NOT built on ``apscheduler`` (reserved for
Epic 7 M11 Scheduler, decision G2, cf ``ttl.py``'s own docstring) — a daily
loop is exactly what this shape already covers.

Two independent passes per tick, mirroring ``MemoryArchivalWorker``'s own
"two independent selection paths, run every pass" structure:

1. **AC1** — terminal runs whose checkpoint history
   (``checkpoints``/``checkpoint_writes``/``checkpoint_blobs``, migration
   ``20260910_000001``) has not been purged yet and ended before the
   retention window: purge via ``AsyncPostgresSaver.adelete_thread``, then
   stamp ``checkpoint_purged_at``.
2. **AC2** — ``paused`` runs stale beyond the alert threshold: LOGGED, never
   acted on. Decision (T2.1): alert only, not an automatic TTL/cancellation
   — see ``shared/config.py``'s ``workflow_paused_run_alert_after_days``
   for the full reasoning.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from agentive_backend.features.workflow_engine.domain.run_control import TERMINAL_STATUSES
from agentive_backend.shared.logging import get_logger
from agentive_backend.shared.repositories import WorkflowRunRepo

if TYPE_CHECKING:
    from uuid import UUID

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

_log = get_logger(__name__)

# Same shutdown envelope as `WorkflowRecoveryWorker`/`MemoryArchivalWorker` —
# a hung pass gets a cooperative window first, then a bounded cancellation.
_STOP_GRACE_S = 2.0
_STOP_CANCEL_TIMEOUT_S = 3.0

# Runs claimed from the DB per batch. Bounds the working set of one query,
# not the work of one pass — see `_MAX_BATCHES_PER_PASS`.
_PURGE_BATCH_SIZE = 1_000

# Safety cap on BATCHES drained per pass — mirror of
# `MemoryArchivalWorker._MAX_BATCHES_PER_RUN`. Batches, not rows: `interval_s`
# defaults to one day, so a per-pass ROW cap below the daily terminal-run rate
# would let the backlog grow forever, and the purge is the only thing bounding
# `checkpoint_blobs`.
_MAX_BATCHES_PER_PASS = 1_000

# Blast-radius ceiling on the orphan sweep (Story 4.15 AC2, review 4.15
# finding 1). The sweep acts on a NEGATIVE predicate — "no `workflow_runs`
# row is visible for this thread" — and DELETES. Any failure of that
# predicate's premise (RLS hiding rows, a lagging replica snapshot, a future
# policy) turns it from a no-op into a mass delete, and a session cannot
# prove from the inside what is being hidden from it.
#
# So the guard is cause-agnostic rather than RLS-specific: a pass that
# discovers more orphans than this refuses to delete anything and reports.
# Orphans are created by workflow deletions, which are manual today (no
# delete path exists in the app), so a genuine batch of this size is already
# an event worth a human looking at it. A ceiling costs one comparison and
# converts the worst case from "irrecoverable" to "an ERROR line".
_MAX_ORPHANS_PER_PASS = 500

# Ceiling on the per-pass set of runs that failed to purge. They are excluded
# from subsequent claims of the same pass so they never block the backlog
# behind them, which is why the set itself needs a bound — mirror of
# `MemoryArchivalWorker._MAX_FAILED_PER_RUN`.
_MAX_FAILED_PER_PASS = 10_000

# Retry cadence after a FAILED pass, shortened and capped — mirror of
# `MemoryArchivalWorker._RETRY_BACKOFF_S`. At the nominal cadence a single
# transient error would cost a full day of both purging and alerting.
_RETRY_BACKOFF_S: tuple[float, ...] = (60.0, 300.0, 900.0)


@dataclass(frozen=True, slots=True)
class RetentionRunSummary:
    """Result of one :meth:`CheckpointRetentionWorker.run_once` pass."""

    purged_count: int
    purge_failed_count: int
    stale_paused_count: int
    #: Story 4.15 AC2 — threads whose `workflow_runs` row was cascaded away
    #: by a workflow deletion, and which no other discovery path can reach.
    orphan_purged_count: int = 0
    orphan_failed_count: int = 0


class CheckpointRetentionWorker:
    """Background job — checkpoint blob retention (AC1) + stale-`paused`
    alerting (AC2)."""

    def __init__(
        self,
        *,
        checkpointer: AsyncPostgresSaver,
        session_factory: Any,
        interval_s: float,
        retention_days: int,
        paused_alert_after_days: int,
    ) -> None:
        self._checkpointer = checkpointer
        self._interval_s = interval_s
        self._retention_days = retention_days
        self._paused_alert_after_days = paused_alert_after_days
        self._workflow_run_repo = WorkflowRunRepo(session_factory=session_factory)
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    # ─────────────────────────── lifecycle ───────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError(
                "CheckpointRetentionWorker.start() called twice; call stop() first if restarting."
            )
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="checkpoint-retention-worker")
        _log.info("workflow_engine.retention_worker_started", interval_s=self._interval_s)

    async def stop(self) -> None:
        """Idempotent. See `WorkflowRecoveryWorker.stop`'s Story 4.9 AC6/T6.7
        note on why this envelope is 5s worst case (2s grace + 3s cancel),
        not the near-instant shutdown a single `_STOP_GRACE_S` glance
        suggests — this worker has no in-flight sub-tasks to cancel first,
        unlike the recovery worker's resumed runs, so it does NOT pay that
        second `_STOP_CANCEL_TIMEOUT_S`."""
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
            if not finished:
                # ERROR, not WARNING: the loop is still running while the
                # lifespan is about to close the shared checkpointer's pool
                # (`workflow_exit_stack.aclose()` runs right after this
                # returns, and `cancel_inflight_runs()` does not cover this
                # task), so the purge would call `adelete_thread` against a
                # closing pool. The per-item `_stopping` check in
                # `_purge_old_checkpoints` is what prevents it; this says
                # that prevention failed.
                _log.error(
                    "workflow_engine.retention_worker_stop_timeout",
                    timeout_s=_STOP_GRACE_S + _STOP_CANCEL_TIMEOUT_S,
                )
            elif not task.cancelled() and task.exception() is not None:
                _log.error(
                    "workflow_engine.retention_worker_shutdown_error", exc_info=task.exception()
                )
        _log.info("workflow_engine.retention_worker_stopped")

    async def _loop(self) -> None:
        consecutive_failures = 0
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception:
                consecutive_failures += 1
                _log.exception(
                    "workflow_engine.retention_run_failed",
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
        failed one — mirror of ``MemoryArchivalWorker._next_delay``."""
        if consecutive_failures == 0:
            return self._interval_s
        backoff = _RETRY_BACKOFF_S[min(consecutive_failures, len(_RETRY_BACKOFF_S)) - 1]
        # Never wait LONGER than the nominal cadence: with a short
        # `interval_s` (tests, or a future config) an unclamped backoff would
        # make a failing worker slower than a healthy one, which is backwards.
        return min(backoff, self._interval_s)

    # ─────────────────────────── run ──────────────────────────────────

    async def run_once(self) -> RetentionRunSummary:
        """One pass — safe to call directly in tests, never sleeps."""
        # The purge takes a cluster-wide lease; the alert does not. The alert
        # is a read plus a log line, so N replicas emitting it N times is
        # noise, not harm — and suppressing it under the lease would mean a
        # replica that never wins the lease never reports stale runs either.
        async with self._workflow_run_repo.purge_lease() as leased:
            if leased:
                purged_count, purge_failed_count = await self._purge_old_checkpoints()
                # Same lease, deliberately: this pass issues DELETEs too, and
                # N replicas sweeping at once contend on
                # `checkpoint_writes`/`checkpoint_blobs` exactly like the AC1
                # pass would.
                #
                # Second, not first, for a budget reason and NOT a
                # correctness one. An earlier comment here claimed the order
                # kept a normally-purged run from being re-examined as an
                # orphan; that was false (review 4.15, three layers) — the
                # AC1 pass only stamps `checkpoint_purged_at`, the
                # `workflow_runs` row survives, so such a run can never match
                # `NOT EXISTS` at any ordering. The real reason: both passes
                # share one lease and one checkpointer pool, and the AC1 pass
                # is the bounded, marker-driven one, so it gets the budget
                # first.
                orphan_purged_count, orphan_failed_count = await self._purge_orphan_threads()
            else:
                _log.info("workflow_engine.retention_purge_skipped_not_leader")
                purged_count, purge_failed_count = 0, 0
                orphan_purged_count, orphan_failed_count = 0, 0
        stale_paused_count = await self._alert_on_stale_paused_runs()
        summary = RetentionRunSummary(
            purged_count=purged_count,
            purge_failed_count=purge_failed_count,
            stale_paused_count=stale_paused_count,
            orphan_purged_count=orphan_purged_count,
            orphan_failed_count=orphan_failed_count,
        )
        # Without this, a pass where EVERY purge failed emits the same
        # `retention_run_completed` line with `purged_count=0` as a healthy
        # pass with nothing to do — and that line is what the runbook has
        # operators grep as proof the worker is working.
        if purge_failed_count or orphan_failed_count:
            # Story 4.15: `orphan_failed_count` belongs here for the reason
            # the paragraph above gives for its sibling — a pass where every
            # orphan delete failed otherwise emits the same INFO line an
            # operator greps as proof the worker works.
            _log.error(
                "workflow_engine.retention_purge_degraded",
                purged_count=purged_count,
                purge_failed_count=purge_failed_count,
                orphan_purged_count=orphan_purged_count,
                orphan_failed_count=orphan_failed_count,
                total_failure=purged_count == 0 and orphan_purged_count == 0,
            )
        _log.info(
            "workflow_engine.retention_run_completed",
            purged_count=purged_count,
            purge_failed_count=purge_failed_count,
            stale_paused_count=stale_paused_count,
            orphan_purged_count=orphan_purged_count,
            orphan_failed_count=orphan_failed_count,
        )
        return summary

    async def _purge_old_checkpoints(self) -> tuple[int, int]:
        cutoff = datetime.now(UTC) - timedelta(days=self._retention_days)
        purged = 0
        failed_ids: list[UUID] = []
        # Drain in batches until empty. A successful purge stamps
        # `checkpoint_purged_at` and drops out of `list_purgeable`; a failed
        # one does not, hence `failed_ids` — without excluding them a batch
        # of persistent failures is re-claimed every iteration and burns the
        # whole budget.
        previous_batch_ids: set[UUID] = set()
        for _batch in range(_MAX_BATCHES_PER_PASS):
            runs = await self._workflow_run_repo.list_purgeable(
                terminal_statuses=tuple(TERMINAL_STATUSES),
                older_than=cutoff,
                limit=_PURGE_BATCH_SIZE,
                exclude_run_ids=failed_ids,
            )
            if not runs:
                break
            batch_ids = {run.id for run in runs}
            if batch_ids == previous_batch_ids:
                # No forward progress: this claim returned exactly what the
                # last one did. Draining depends on `mark_checkpoint_purged`
                # removing rows from `list_purgeable`'s predicate, so a
                # repeated batch means that contract is not holding (replica
                # lag, a stamp that silently did nothing). Re-purging the
                # same runs up to `_MAX_BATCHES_PER_PASS` times would be the
                # only alternative.
                _log.warning("workflow_engine.retention_purge_no_progress", batch_size=len(runs))
                break
            previous_batch_ids = batch_ids
            for run in runs:
                if self._stopping.is_set():
                    # Unwind on the ITEM, not just the batch: `stop()` grants
                    # 2s before cancelling and the lifespan closes the shared
                    # checkpointer's pool as soon as it returns, which a batch
                    # of up to 1000 runs x 2 round trips cannot fit inside.
                    break
                if run.ended_at is None:
                    # Story 4.15 AC3 — this run is only purgeable through the
                    # `COALESCE` fallback, i.e. it reached a terminal status
                    # without `ended_at` ever being stamped. No application
                    # write site does that, so seeing it means something wrote
                    # a terminal status outside them. Closing the hole must not
                    # make the anomaly invisible.
                    _log.warning(
                        "workflow_engine.retention_terminal_run_without_ended_at",
                        run_id=str(run.id),
                        workflow_id=str(run.workflow_id),
                        status=run.status,
                    )
                try:
                    # Idempotent against a run this codebase never touches
                    # again after purging: a repeat call on an already-empty
                    # `thread_id` is a harmless no-op DELETE (see
                    # `list_purgeable`'s docstring on why no locking is taken).
                    await self._checkpointer.adelete_thread(str(run.id))
                    marked = await self._workflow_run_repo.mark_checkpoint_purged(run.id)
                    if marked == 0:
                        # The run vanished between the claim and the mark
                        # (CASCADE from a deleted workflow). The blobs are
                        # gone either way — it just is not purged WORK.
                        _log.info(
                            "workflow_engine.retention_purge_row_vanished", run_id=str(run.id)
                        )
                        continue
                    purged += 1
                except Exception:
                    # One poisoned run's purge failure must never block its
                    # siblings — mirror every other worker's per-item resilience
                    # in this codebase (`MemoryArchivalWorker`,
                    # `WorkflowRecoveryWorker.run_once`).
                    failed_ids.append(run.id)
                    _log.exception(
                        "workflow_engine.retention_purge_failed",
                        run_id=str(run.id),
                        workflow_id=str(run.workflow_id),
                    )
            if len(failed_ids) >= _MAX_FAILED_PER_PASS:
                _log.error("workflow_engine.retention_failure_cap_reached", failed=len(failed_ids))
                break
            if self._stopping.is_set():
                # Shutdown asked for: stop claiming NEW batches rather than
                # working a day-sized backlog down while the lifespan waits
                # on `stop()`'s envelope.
                break
        else:
            # Every batch consumed and still more to do. Without this a
            # worker permanently behind its ingestion rate is
            # indistinguishable from a healthy one in the logs.
            _log.warning(
                "workflow_engine.retention_purge_saturated",
                batches=_MAX_BATCHES_PER_PASS,
                batch_size=_PURGE_BATCH_SIZE,
                purged_count=purged,
            )
        return purged, len(failed_ids)

    async def _purge_orphan_threads(self) -> tuple[int, int]:
        """Purge LangGraph threads whose ``workflow_runs`` row is gone
        (Story 4.15 AC2).

        ``_purge_old_checkpoints`` cannot see these: it drives
        ``list_purgeable``, which joins on ``workflow_runs``, and a workflow
        deletion cascades its runs away. The blobs then belong to no row and
        no query — unbounded growth through the one door Story 4.10 AC1 does
        not look at.

        **No retention window applies, and that is deliberate.** The window
        exists to keep a run's trace readable for N days; an orphan thread has
        no run to read it against, no status and no ``ended_at``. It is dead
        the instant its row disappears.

        **A blast-radius ceiling stands between the predicate and the
        DELETE**, because this is the only pass in the repo that destroys data
        on the strength of a NEGATIVE predicate — see
        :meth:`WorkflowRunRepo.list_orphan_checkpoint_threads` for why its
        premise cannot be proved from inside the session. More than
        ``_MAX_ORPHANS_PER_PASS`` candidates in one pass is treated as a wrong
        predicate rather than a big backlog: the pass refuses ENTIRELY and
        logs at ERROR, because a partially-applied mass delete is worse than
        none.

        The ceiling does not make the sweep sound under multi-tenant RLS — it
        bounds the damage. Making it sound is a prerequisite of Story 4.9 AC1,
        registered there beside the site inventory.

        Draining differs from the AC1 pass in one way worth stating: there is
        no marker to stamp (the row that would carry it no longer exists), so
        the only evidence of forward progress is that the next claim returns
        something else. Failed threads are excluded from subsequent claims —
        mirror of ``exclude_run_ids`` — so one poisoned thread sorting first
        cannot be re-attempted once per batch for the whole pass.
        """
        purged = 0
        failed = 0
        failed_threads: list[str] = []
        previous_batch: set[str] = set()
        for _batch in range(_MAX_BATCHES_PER_PASS):
            threads = await self._workflow_run_repo.list_orphan_checkpoint_threads(
                limit=_PURGE_BATCH_SIZE, exclude_threads=failed_threads
            )
            if not threads:
                break

            # The gate — a batch this large is a wrong predicate, not a
            # backlog. Cause-agnostic on purpose: it fires on the RLS failure
            # above, on a lagging replica snapshot, on a future policy, and on
            # whatever else hides rows, without having to enumerate them.
            #
            # A "no visible runs at all" gate was tried first and REMOVED: an
            # empty `workflow_runs` is legitimate (a fresh install carrying
            # leftover threads), so it fired on a true negative while adding
            # nothing the ceiling does not already cover — under real hiding,
            # every thread reads as an orphan and the count blows past this
            # ceiling long before anything is deleted.
            if len(threads) > _MAX_ORPHANS_PER_PASS:
                _log.error(
                    "workflow_engine.retention_orphan_refused_blast_radius",
                    candidates=len(threads),
                    ceiling=_MAX_ORPHANS_PER_PASS,
                    visible_runs=await self._workflow_run_repo.count_visible_runs(),
                    reason=(
                        "more orphan candidates than a workflow deletion can "
                        "plausibly produce; treating this as a failed predicate "
                        "(RLS hiding rows, stale snapshot) rather than a backlog"
                    ),
                )
                break

            batch = set(threads)
            if batch == previous_batch:
                _log.warning(
                    "workflow_engine.retention_orphan_no_progress", batch_size=len(threads)
                )
                break
            previous_batch = batch
            for thread_id in threads:
                if self._stopping.is_set():
                    # Unwind on the ITEM, like the AC1 pass: `stop()` grants a
                    # 2s cooperative window and the lifespan closes the shared
                    # checkpointer pool right after.
                    break
                if failed >= _MAX_FAILED_PER_PASS:
                    # Checked INSIDE the loop, not only between batches: a
                    # closed pool makes every call in a 1000-thread batch
                    # raise, and `_log.exception` would write 1000 tracebacks
                    # before the cap was ever read.
                    _log.error(
                        "workflow_engine.retention_orphan_failure_cap_reached", failed=failed
                    )
                    break
                try:
                    await self._checkpointer.adelete_thread(thread_id)
                    purged += 1
                except Exception:
                    failed += 1
                    failed_threads.append(thread_id)
                    _log.exception(
                        "workflow_engine.retention_orphan_purge_failed", thread_id=thread_id
                    )
            if failed >= _MAX_FAILED_PER_PASS or self._stopping.is_set():
                break
        else:
            _log.warning(
                "workflow_engine.retention_orphan_saturated",
                batches=_MAX_BATCHES_PER_PASS,
                purged_count=purged,
            )
        if purged or failed:
            _log.info(
                "workflow_engine.retention_orphan_threads_purged",
                purged_count=purged,
                failed_count=failed,
            )
        return purged, failed

    async def _alert_on_stale_paused_runs(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=self._paused_alert_after_days)
        count, oldest_run_id, oldest_staleness = await self._workflow_run_repo.count_stale_paused(
            older_than=cutoff
        )
        if count > 0:
            # WARNING, not ERROR: nothing is broken — a run sitting `paused`
            # is a legitimate state this codebase itself never expires
            # (Story 4.9 AC6 Dev Notes on `claim_stale_running`'s deliberate
            # `status = 'running'` filter). This is an operator prompt, not
            # an incident.
            _log.warning(
                "workflow_engine.stale_paused_runs_detected",
                count=count,
                oldest_run_id=str(oldest_run_id) if oldest_run_id else None,
                oldest_last_checkpoint_at=(
                    oldest_staleness.isoformat() if oldest_staleness else None
                ),
                alert_after_days=self._paused_alert_after_days,
            )
        return count


__all__ = ["CheckpointRetentionWorker", "RetentionRunSummary"]
