"""WorkflowRecoveryWorker — resumes orphaned runs after a crash (Story 4.2 AC3).

Structural mirror of ``MemoryArchivalWorker`` (Story 3.3,
``features/memory_manager/ttl.py``) and ``OutboxWorker`` — the repo's THIRD
instance of "background worker started by lifespan, ``start()``/``stop()``
with grace + cancel bound, immediate pass then periodic cadence." Not a new
design, a replica of an already-validated pattern.

Only the "reprendre l'exécution" step delegates to
:class:`~agentive_backend.features.workflow_engine.service.WorkflowExecutionService`
(which already holds ``llm_router``/``checkpointer`` — resources this worker
doesn't need to duplicate). Everything else — detecting orphaned runs,
loading the workflow + templates, publishing the ``resumed`` event — is
self-contained, constructing its own repos from ``session_factory`` (mirror
``MemoryArchivalWorker.__init__``, NOT injected repos).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from agentive_backend.features.workflow_engine.engine.agent_node import NODE_TIMEOUT_S
from agentive_backend.shared.contracts.events import (
    WorkflowRunFailedEvent,
    WorkflowRunResumedEvent,
)
from agentive_backend.shared.event_bus import notify_best_effort, publish
from agentive_backend.shared.logging import get_logger
from agentive_backend.shared.repositories import AgentTemplateRepo, WorkflowRepo, WorkflowRunRepo
from agentive_backend.shared.repositories.workflow_repo import RECOVERY_ATTEMPTS_KEY

if TYPE_CHECKING:
    from agentive_backend.features.workflow_engine.service import WorkflowExecutionService
    from agentive_backend.infra.db.models import AgentTemplate, WorkflowRun

_log = get_logger(__name__)

# Same shutdown envelope as `MemoryArchivalWorker` (Story 3.3, P5) — a hung
# pass gets a cooperative window first, then a bounded cancellation, both
# comfortably under the lifespan's own 5s shutdown drain budget.
_STOP_GRACE_S = 2.0
_STOP_CANCEL_TIMEOUT_S = 3.0

# A run is only "orphaned" once it has been silent for LONGER than a single
# node can legitimately take. `NODE_TIMEOUT_S` is handed to
# `LLMRouter.complete`, which applies it PER PROVIDER ATTEMPT (see the
# `for index, provider_name in enumerate(chain)` loop in `shared/llm/router.py`)
# — so with the production chain `["anthropic", "openai"]` a single healthy
# node can legitimately run for 2 x NODE_TIMEOUT_S = 120s before writing its
# checkpoint. The previous 120.0 default sat EXACTLY on that boundary: any
# run that hit one provider fallback was classified orphaned while still
# perfectly alive. The x2.5 margin below also absorbs checkpoint-write
# latency and event-loop scheduling delay.
_MAX_PROVIDER_CHAIN_LEN = 2
DEFAULT_STALE_THRESHOLD_S = NODE_TIMEOUT_S * _MAX_PROVIDER_CHAIN_LEN * 2.5

# A run whose resume keeps dying before completing a single node is a poison
# run, not a transient crash. `claim_stale_running` bumps
# `checkpoint.recovery_attempts` on each claim (and `_sync_checkpoint` resets
# it as soon as a node lands), so this is a cap on CONSECUTIVE fruitless
# resumes, not on lifetime resumes.
MAX_RECOVERY_ATTEMPTS = 3


def _recovery_attempts(checkpoint: dict[str, Any] | None) -> int:
    """Read back the attempt counter stamped by ``claim_stale_running``."""
    if not isinstance(checkpoint, dict):
        return 0
    value = checkpoint.get(RECOVERY_ATTEMPTS_KEY)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


@dataclass(frozen=True, slots=True)
class RecoveryRunSummary:
    """Result of one :meth:`WorkflowRecoveryWorker.run_once` pass.

    The field is named ``resume_triggered_count``, not ``resumed_count``,
    because that is all a sweep can honestly report: the execution is spawned
    fire-and-forget, so this pass returns long before the resumed run
    succeeds or fails. Under the old name, an operator watching AC3's
    recovery metric would have read "9 runs recovered" from a sweep where all
    nine went on to die — and `MAX_RECOVERY_ATTEMPTS` exists precisely
    because that happens.

    ``abandoned_count`` counts runs marked ``error`` for exceeding
    :data:`MAX_RECOVERY_ATTEMPTS`; ``failed_count`` counts runs this pass
    could not even dispatch.
    """

    resume_triggered_count: int
    failed_count: int
    abandoned_count: int = 0


def _resumed_from_node_id(checkpoint: dict[str, Any] | None) -> str | None:
    """Best-effort (T6.3) — read the applicative checkpoint summary's
    ``last_node_id`` (written by ``WorkflowExecutionService._sync_checkpoint``
    after every completed node). ``None`` for a run that crashed before its
    first checkpoint sync ever landed, or whose ``checkpoint`` is malformed —
    never raises."""
    if not isinstance(checkpoint, dict):
        return None
    value = checkpoint.get("last_node_id")
    return value if isinstance(value, str) else None


class WorkflowRecoveryWorker:
    """Background job — resumes ``workflow_runs`` orphaned by a process crash."""

    def __init__(
        self,
        *,
        workflow_execution_service: WorkflowExecutionService,
        session_factory: Any,
        interval_s: float = 30.0,
        stale_threshold_s: float = DEFAULT_STALE_THRESHOLD_S,
    ) -> None:
        self._workflow_execution_service = workflow_execution_service
        self._interval_s = interval_s
        self._stale_threshold_s = stale_threshold_s
        self._workflow_run_repo = WorkflowRunRepo(session_factory=session_factory)
        self._workflow_repo = WorkflowRepo(session_factory=session_factory)
        self._template_repo = AgentTemplateRepo(session_factory=session_factory)
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        # Kept alive so a spawned resume task isn't GC'd mid-await — a
        # separate set from `workflow_engine/service.py::_background_tasks`
        # (that one tracks fresh `start_run` tasks, this one resumed ones).
        self._resume_tasks: set[asyncio.Task[None]] = set()

    # ─────────────────────────── lifecycle ───────────────────────────

    async def start(self) -> None:
        """Spawn the loop task (immediate pass, then a periodic cadence).

        Not idempotent — mirrors ``OutboxWorker.start()``/``MemoryArchivalWorker
        .start()``'s posture so a misuse can't leak a duplicate task.
        """
        if self._task is not None:
            raise RuntimeError(
                "WorkflowRecoveryWorker.start() called twice; call stop() first if restarting."
            )
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="workflow-recovery-worker")
        _log.info("workflow_engine.recovery_worker_started", interval_s=self._interval_s)

    async def stop(self) -> None:
        """Stop the loop task AND cancel every resume it spawned, under a hard
        time cap. Idempotent.

        Cancelling ``_resume_tasks`` is what makes T5.4's "an interrupted run
        IS the AC3 scenario" true rather than aspirational:
        ``asyncio.CancelledError`` derives from ``BaseException``, so
        ``WorkflowExecutionService._execute``'s ``except Exception`` does NOT
        catch it — the run is left in ``running`` for the next process's
        recovery sweep instead of being buried in the terminal ``error``
        status that no sweep ever revisits. Without this, the lifespan closed
        the checkpointer under these very tasks and every one of them died on
        a closed connection, i.e. as ``error``.
        """
        self._stopping.set()
        await self._cancel_resume_tasks()
        task, self._task = self._task, None
        if task is not None:
            finished = task.done()
            if not finished:
                done, _pending = await asyncio.wait({task}, timeout=_STOP_GRACE_S)
                if not done:
                    task.cancel()
                    done, _pending = await asyncio.wait({task}, timeout=_STOP_CANCEL_TIMEOUT_S)
                finished = bool(done)
            self._report_shutdown(task, finished=finished)
        _log.info("workflow_engine.recovery_worker_stopped")

    async def _cancel_resume_tasks(self) -> None:
        """Cancel in-flight resumes and wait (briefly) for the cancellations
        to actually land, so the caller can close the checkpointer safely."""
        pending = [task for task in self._resume_tasks if not task.done()]
        if not pending:
            return
        _log.info("workflow_engine.recovery_cancelling_resumes", count=len(pending))
        for task in pending:
            task.cancel()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=_STOP_CANCEL_TIMEOUT_S,
            )

    @staticmethod
    def _report_shutdown(task: asyncio.Task[None], *, finished: bool) -> None:
        if not finished:
            _log.warning(
                "workflow_engine.recovery_worker_stop_timeout",
                timeout_s=_STOP_GRACE_S + _STOP_CANCEL_TIMEOUT_S,
            )
            return
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _log.error("workflow_engine.recovery_worker_shutdown_error", exc_info=error)

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception:
                _log.exception("workflow_engine.recovery_run_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_s)

    # ─────────────────────────── run ──────────────────────────────────

    async def run_once(self) -> RecoveryRunSummary:
        """One sweep — safe to call directly in tests (T7.4), never sleeps.

        AC3: CLAIMS ``workflow_runs`` with ``status="running"`` AND
        ``now() - COALESCE(last_checkpoint_at, started_at) > stale_threshold_s``
        (T2.3) — a run's own natural boot-time first pass is the trigger for
        the "process crashed then restarted" scenario, since an orphaned run
        left by the previous process is picked up on the very first tick of
        the new one.

        Claiming (not merely listing) is what keeps this sweep idempotent:
        without it, the run stayed stale until its resume completed a node,
        so every subsequent tick spawned yet another execution on the same
        ``thread_id``. See :meth:`WorkflowRunRepo.claim_stale_running`.
        """
        stale_before = datetime.now(UTC) - timedelta(seconds=self._stale_threshold_s)
        runs = await self._workflow_run_repo.claim_stale_running(older_than=stale_before)

        resume_triggered_count = 0
        failed_count = 0
        abandoned_count = 0
        for run in runs:
            attempts = _recovery_attempts(run.checkpoint)
            try:
                if attempts > MAX_RECOVERY_ATTEMPTS:
                    await self._abandon_one(run, attempts=attempts)
                    abandoned_count += 1
                else:
                    await self._resume_one(run, attempts=attempts)
                    resume_triggered_count += 1
            except Exception:
                # One orphaned run's resolution failure (e.g. its workflow
                # was somehow deleted) must never block the rest of the
                # sweep — mirror `MemoryArchivalWorker`'s per-item resilience.
                failed_count += 1
                _log.exception(
                    "workflow_engine.recovery_resume_failed",
                    run_id=str(run.id),
                    workflow_id=str(run.workflow_id),
                )

        summary = RecoveryRunSummary(
            resume_triggered_count=resume_triggered_count,
            failed_count=failed_count,
            abandoned_count=abandoned_count,
        )
        _log.info(
            "workflow_engine.recovery_run_completed",
            resume_triggered_count=resume_triggered_count,
            failed_count=failed_count,
            abandoned_count=abandoned_count,
            stale_threshold_s=self._stale_threshold_s,
        )
        return summary

    async def _abandon_one(self, run: WorkflowRun, *, attempts: int) -> None:
        """Mark a poison run ``error`` instead of resuming it again (AC3 cap).

        A run that has been claimed :data:`MAX_RECOVERY_ATTEMPTS` times
        without a single node landing in between is not recovering — it is
        looping. Left alone it would be re-resumed every
        ``stale_threshold_s`` forever, each attempt spending real LLM budget.
        """
        error_summary = (
            f"Recovery abandoned after {attempts} consecutive attempts with no node progress."
        )
        checkpoint = dict(run.checkpoint) if isinstance(run.checkpoint, dict) else {}
        checkpoint["last_error"] = error_summary
        ended_at = datetime.now(UTC)

        rowcount = await self._workflow_run_repo.update_status(
            run.id, status="error", ended_at=ended_at, only_if_status="running"
        )
        if rowcount == 0:
            # A resume spawned by an earlier sweep finished between this
            # sweep's claim and now — the run is already terminal, and
            # publishing `failed` for it would contradict the row.
            _log.info("workflow_engine.recovery_abandon_skipped", run_id=str(run.id))
            return
        await self._workflow_run_repo.update_checkpoint(
            run.id, checkpoint=checkpoint, last_checkpoint_at=ended_at
        )

        event_type = WorkflowRunFailedEvent.event_type
        async with self._workflow_run_repo.with_tenant(None) as session:
            event = WorkflowRunFailedEvent(
                run_id=run.id,
                workflow_id=run.workflow_id,
                failed_node_id=_resumed_from_node_id(run.checkpoint),
                error_summary=error_summary,
            )
            event_id = await publish(
                event_type, event, session=session, correlation_id=run.correlation_id
            )
        await notify_best_effort(event_id, event_type)

        _log.error(
            "workflow_engine.recovery_abandoned",
            run_id=str(run.id),
            workflow_id=str(run.workflow_id),
            attempts=attempts,
        )

    async def _resume_one(self, run: WorkflowRun, *, attempts: int) -> None:
        """Publish ``resumed`` BEFORE relaunching (AC3 literal ordering),
        then spawn the resume execution fire-and-forget — same anti-drain
        posture as ``WorkflowExecutionService._drive_run`` (T5.4): a
        restart must never be blocked by an in-flight resume.

        ``run`` arrives already claimed by :meth:`run_once`, so no other
        sweep — in this process or another replica — can dispatch it again
        before ``stale_threshold_s`` elapses.
        """
        workflow = await self._workflow_repo.require_by_id(run.workflow_id)
        templates = await self._load_templates(workflow.dag)

        event_type = WorkflowRunResumedEvent.event_type
        resumed_from_node_id = _resumed_from_node_id(run.checkpoint)
        async with self._workflow_run_repo.with_tenant(None) as session:
            event = WorkflowRunResumedEvent(
                run_id=run.id,
                workflow_id=run.workflow_id,
                resumed_from_node_id=resumed_from_node_id,
            )
            event_id = await publish(
                event_type, event, session=session, correlation_id=run.correlation_id
            )
        await notify_best_effort(event_id, event_type)

        task = asyncio.create_task(
            self._workflow_execution_service._resume_run(run.id, workflow, templates),
            name=f"workflow-resume-{run.id}",
        )
        self._resume_tasks.add(task)
        task.add_done_callback(self._resume_tasks.discard)

        _log.info(
            "workflow_engine.recovery_resume_triggered",
            run_id=str(run.id),
            workflow_id=str(run.workflow_id),
            resumed_from_node_id=resumed_from_node_id,
            attempts=attempts,
        )

    async def _load_templates(self, dag_payload: dict[str, Any]) -> dict[str, AgentTemplate]:
        """Mirror ``WorkflowExecutionService._load_templates`` — duplicated
        rather than shared, this worker intentionally builds its own repos
        rather than reusing the execution service's (cf class docstring)."""
        templates: dict[str, AgentTemplate] = {}
        for node in dag_payload.get("nodes", []):
            template_id = UUID(node["agent_template_id"])
            templates[node["node_id"]] = await self._template_repo.require_by_id(template_id)
        return templates


__all__ = [
    "DEFAULT_STALE_THRESHOLD_S",
    "MAX_RECOVERY_ATTEMPTS",
    "RecoveryRunSummary",
    "WorkflowRecoveryWorker",
]
