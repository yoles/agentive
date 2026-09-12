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
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from agentive_backend.features.workflow_engine.domain.error_policy import MAX_RUNTIME_RETRIES
from agentive_backend.features.workflow_engine.engine.agent_node import NODE_TIMEOUT_S
from agentive_backend.shared.contracts.events import (
    WorkflowRunFailedEvent,
    WorkflowRunResumedEvent,
)
from agentive_backend.shared.event_bus import notify_best_effort, publish
from agentive_backend.shared.exceptions import NotFoundError
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

# Story 4.3 point 9 / T5.6 — a hybrid-routing decision point (a node with at
# least one conditional outgoing edge) can ALSO escalate to an LLM after the
# node's own completion, and that escalation call goes through
# `LLMRouter.complete` too — so it pays the same per-provider timeout. A
# decision-point node's worst case is therefore its own NODE_TIMEOUT_S PLUS
# one escalation call, each multiplied by the provider chain length. Without
# this term, a workflow with hybrid routing that hits one provider fallback
# on both the node call AND the escalation call would be misclassified
# orphaned at (60 + 15) = 75s short of what it can legitimately take.
#
# Mirrors the settings default (`AGENTIVE_ROUTING_ESCALATION_TIMEOUT_S`,
# `shared/config.py`) as a plain float rather than importing `settings` —
# this is a STATIC fallback for the constructor's own `stale_threshold_s=`
# default, exactly like `NODE_TIMEOUT_S` above it never reads `settings`.
_ROUTING_ESCALATION_TIMEOUT_S_DEFAULT = 15.0

# Story 4.7 T5.6 — `execute_agent_node` now makes a SECOND LLM call after its
# own completion, on every node with a downstream successor: `summarize_handoff`
# (T3.2). It carries no `provider_chain=` (a process-wide call, like the
# escalation above), so it pays the SAME per-provider-in-the-chain timeout,
# once, on top of the node's own worst case. Without this term, a node whose
# handoff summary is legitimately still running — e.g. its first provider in
# the default chain timed out and the router fell back to the second — would
# be classified orphaned and claimed by the sweep while alive: the exact
# double-execution bug T8.4 (Story 4.6) already fixed for retries, reopened
# by a different addition.
#
# Mirrors the settings default (`AGENTIVE_WORKFLOW_HANDOFF_SUMMARY_TIMEOUT_S`,
# `shared/config.py`) as a plain float, same posture as
# `_ROUTING_ESCALATION_TIMEOUT_S_DEFAULT` above — a static fallback for the
# constructor's own default, never a `settings` read from this module.
_HANDOFF_SUMMARY_TIMEOUT_S_DEFAULT = 20.0

# Story 4.6 T8.4/T8.5 — the term this story ADDS, and the reason the whole
# derivation had to be revisited.
#
# `error_policy.on_timeout = "retry_with_backoff"` makes `execute_agent_node`
# re-run the ENTIRE provider chain up to `max_retries` times, with a backoff
# wait in between. Every one of those seconds is wall-clock during which the
# node writes no checkpoint — so the formula below, which assumed a node
# traverses the chain exactly ONCE, stopped describing reality the moment the
# dispatcher landed. Left unchanged, a perfectly healthy node on its third
# retry would be classified orphaned, claimed by the sweep, and executed a
# SECOND time in parallel on the same `thread_id`: duplicate billed LLM
# calls, interleaved checkpoint writes, duplicate events.
#
# WHY 3 AND NOT THE SCHEMA'S 10 (T8.5, the branch taken). Story 2.2 validates
# `max_retries` up to 10. Deriving this threshold against 10 gives
# `((60+15) * 2 * 11 + 181) * 2.5 ≈ 4600 s` — over an hour before a genuinely
# crashed run is even LOOKED at, which defeats the recovery worker. The two
# numbers answer different questions: the schema validates an INTENTION a
# template author may express, this constant guarantees a PROCESS INVARIANT.
# So the runtime caps what it will actually execute, and the cap is the
# number this derivation uses. An assumed divergence, not an oversight —
# `agent_node` enforces it, and a template asking for more is honoured up to
# here and logged. The constant lives in `domain/error_policy.py` so both
# the enforcer and this derivation read the SAME number; a local copy here
# is exactly how the two would silently drift apart.

# Margin over the derived worst case, absorbing checkpoint-write latency and
# event-loop scheduling delay.
_SAFETY_MARGIN = 2.5

#: The three settings defaults this module MIRRORS to size
#: :data:`DEFAULT_STALE_THRESHOLD_S`. Mirrored rather than imported for the
#: same reason as `NODE_TIMEOUT_S` above: this constant is the STATIC
#: fallback used when nobody derives the threshold, and a module that reads
#: `settings` at import time cannot be one. The mirror is not left to
#: vigilance — `test_recovery.py` asserts each against `Settings()`.
_DEFAULT_RETRY_BASE_DELAY_S = 1.0
_DEFAULT_RETRY_MAX_DELAY_S = 30.0


def _worst_case_backoff_s(*, base_delay_s: float, max_delay_s: float) -> float:
    """Total backoff a node can accumulate across its runtime retries.

    ``MAX_RUNTIME_RETRIES`` waits, each ``base * 2**attempt`` clamped to
    ``max_delay_s`` — the ``exponential`` strategy, which is the worst case
    of the three ``backoff_delay_s`` implements: it equals ``linear`` at
    attempts 0 and 1 and exceeds it from attempt 2 on, and dominates
    ``constant`` everywhere. Bounding the worst one bounds all three.
    """
    return float(
        sum(min(base_delay_s * 2**attempt, max_delay_s) for attempt in range(MAX_RUNTIME_RETRIES))
    )


def derive_stale_threshold_s(
    *,
    base_delay_s: float,
    max_delay_s: float,
    escalation_timeout_s: float,
    handoff_summary_timeout_s: float,
) -> float:
    """Worst-case wall-clock for ONE node that writes no checkpoint throughout.

    ::

        (node call + its routing escalation)   -> NODE_TIMEOUT_S + escalation
        x every provider in the chain          -> _MAX_PROVIDER_CHAIN_LEN
        x every attempt (first + retries)      -> 1 + MAX_RUNTIME_RETRIES
        + the backoff waits between them       -> _worst_case_backoff_s(...)
        + its handoff summary, once, across
          every provider in the chain          -> handoff_summary_timeout_s
                                                   x _MAX_PROVIDER_CHAIN_LEN
        x margin for checkpoint-write latency
          and event-loop scheduling            -> _SAFETY_MARGIN

    ``handoff_summary_timeout_s`` is REQUIRED, like its three siblings.
    Review of 2026-09-12 (P-15): it shipped with a default, and a default on
    an argument of this function is precisely the failure it exists to
    prevent — a future caller who forgets to wire it silently derives a
    window from 20.0 s instead of the deployed value, and a window that is
    too short is the double-execution bug this whole derivation was written
    for (Story 4.6 T8.4). The compatibility argument does not hold either:
    the only production caller (``app/lifespan.py``) was edited in the same
    change that introduced the default.

    **A function, not a constant, because the inputs are deployment-tunable
    and the invariant is not** (Story 4.6, review lot 7 / P-P). The three
    arguments are `AGENTIVE_WORKFLOW_RETRY_BASE_DELAY_S`,
    `...MAX_DELAY_S` and `AGENTIVE_ROUTING_ESCALATION_TIMEOUT_S`; the
    previous version hardcoded all three at their defaults and told the
    operator, in a comment, that "a deployment that raises those delays must
    raise ``stale_threshold_s=`` with them".

    That instruction could not be followed: `app.lifespan` was the only
    construction site in `src/` and passed no `stale_threshold_s=`, so the
    parameter the comment pointed at was never plumbed from config at all.

    What that cost, measured rather than asserted:

    * The two RETRY DELAYS stayed covered at their ``le`` ceilings — worst
      case 1020 s against a 1517.5 s window — but only because the x2.5
      margin absorbed them. That margin exists for checkpoint-write latency
      and scheduling delay, and at the ceilings it is down to x1.49. Nobody
      chose 60.0/300.0 against this formula; the fit was luck.
    * `AGENTIVE_ROUTING_ESCALATION_TIMEOUT_S` had NO ceiling, and it is
      multiplied here by chain length x attempts x margin. Past ~130 s the
      window no longer covers the node it describes; at 600 s the worst case
      is 5287 s against the same 1517.5 s. A healthy node was then claimed by
      the sweep and executed a SECOND time in parallel on the same
      `thread_id` — duplicate billed LLM calls, interleaved checkpoint
      writes, duplicate events, reachable by one env var.

    Reading the numbers here makes the invariant true by construction
    instead of by vigilance, at every legal value of all three. `settings`
    is still not imported: the caller that already holds it passes them in,
    the same posture `available` and `model_owner` take in
    `domain/provider_chain.py`.
    """
    # P-15 — the invariant this function exists to make true by construction
    # is only true over a sane domain. `Settings` carries `gt=0`/
    # `allow_inf_nan=False` on all four inputs, but this is a PUBLIC function
    # called directly (including from tests), and a negative value here yields
    # a NEGATIVE threshold: every `running` run is instantly classified
    # orphaned and re-claimed, i.e. generalised double execution — silently.
    for name, value in (
        ("base_delay_s", base_delay_s),
        ("max_delay_s", max_delay_s),
        ("escalation_timeout_s", escalation_timeout_s),
        ("handoff_summary_timeout_s", handoff_summary_timeout_s),
    ):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and >= 0, got {value!r}")

    return (
        (NODE_TIMEOUT_S + escalation_timeout_s)
        * _MAX_PROVIDER_CHAIN_LEN
        * (1 + MAX_RUNTIME_RETRIES)
        + _worst_case_backoff_s(base_delay_s=base_delay_s, max_delay_s=max_delay_s)
        + handoff_summary_timeout_s * _MAX_PROVIDER_CHAIN_LEN
    ) * _SAFETY_MARGIN


# ≈ 1518 s at the default settings. Detection of a genuinely crashed run is
# therefore slower than the 375 s of Story 4.2 — that is the unavoidable
# price of retries existing at all, and it is bounded, derived and
# re-asserted by `test_recovery.py`'s invariants rather than guessed.
#
# This is the FALLBACK, for a caller that derives nothing (tests, and the
# constructor's own default). The running process gets a threshold derived
# from its own settings — cf `app.lifespan`.
DEFAULT_STALE_THRESHOLD_S = derive_stale_threshold_s(
    base_delay_s=_DEFAULT_RETRY_BASE_DELAY_S,
    max_delay_s=_DEFAULT_RETRY_MAX_DELAY_S,
    escalation_timeout_s=_ROUTING_ESCALATION_TIMEOUT_S_DEFAULT,
    handoff_summary_timeout_s=_HANDOFF_SUMMARY_TIMEOUT_S_DEFAULT,
)

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
            run.id,
            status="error",
            ended_at=ended_at,
            only_if_status="running",
            # Terminal transition — it consumes any control request the run
            # was still carrying, so an abandoned run does not advertise a
            # pause that will never happen (Story 4.6 T1.4).
            clear_control=True,
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
                # Story 4.6 AC3 gave this event an `error_type` so an
                # alerting consumer can filter without parsing prose — and
                # `_mark_failed` sets it while THIS publisher, the second of
                # the two, left it null (review of 2026-09-12). Abandoning a
                # poison run after `MAX_RECOVERY_ATTEMPTS` is arguably the
                # most alert-worthy `failed` in the system, and it was the
                # one an `error_type` filter missed. A sentinel rather than
                # an exception class name, because there is no exception
                # here: nothing raised, the worker gave up.
                error_type="RecoveryAbandoned",
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
        """Mirror ``service._load_templates`` — duplicated rather than
        shared, this worker intentionally builds its own repos rather than
        reusing the execution service's (cf class docstring).

        The name it mirrors moved: Story 4.4 T3.1 promoted
        ``WorkflowExecutionService._load_templates`` to a module-level
        function in ``service.py`` so ``DryRunService`` could reuse it, and
        this docstring kept pointing at a method that no longer exists
        (review fix P14). Note this copy is NOT identical to that one — it
        calls ``require_by_id`` (404 on a missing template) where the shared
        function raises ``InternalError`` (500). Deliberate: a recovery
        worker has no HTTP caller to mislead.

        Story 4.8 AC3, extended in code review (BS1). The story's Dev Notes
        called the N+1 debt "double" and closed two sites; this was the
        third, and AC3's *When* — "les templates référencés sont résolus" —
        names no site. It is also the one where N+1 hurts most: ``run_once``
        sweeps runs SEQUENTIALLY, so a 100-node stale run spent 100 round
        trips blocking every other run behind it.

        What the story treated as the blocker was the ``NotFoundError`` /
        ``InternalError`` divergence above. That blocks FOLDING the three
        copies into one, which is still out of scope and still deliberate —
        but it never blocked BATCHING, which is what AC3 asks for. The two
        were separable, and only the second is done here: this copy keeps
        raising ``NotFoundError``, on the first missing template in the
        stored DAG's declaration order, exactly as before.
        """
        nodes = dag_payload.get("nodes", [])
        node_ids = [UUID(node["agent_template_id"]) for node in nodes]
        resolved = await self._template_repo.list_by_ids(node_ids)
        # Walk the DAG in declaration order, not the resolved map: which node
        # is reported must not depend on Postgres' return order.
        templates: dict[str, AgentTemplate] = {}
        for node, template_id in zip(nodes, node_ids, strict=True):
            template = resolved.get(template_id)
            if template is None:
                # Same message and `context` shape `require_by_id` produced
                # through `BaseRepo._require_found` (audit A-07's single
                # spelling of lookup-or-404). Re-spelled here rather than
                # reaching into a private repo helper from feature code; the
                # shape is pinned by a test so the two cannot drift.
                raise NotFoundError(
                    detail=f"Agent template '{template_id}' not found",
                    context={"template_id": str(template_id)},
                )
            templates[node["node_id"]] = template
        return templates


__all__ = [
    "DEFAULT_STALE_THRESHOLD_S",
    "MAX_RECOVERY_ATTEMPTS",
    "RecoveryRunSummary",
    "WorkflowRecoveryWorker",
    "derive_stale_threshold_s",
]
