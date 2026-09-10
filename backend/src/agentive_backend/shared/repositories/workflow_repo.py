"""Public API surface for :class:`Workflow` and :class:`WorkflowRun`.

ALL DB access must go through these classes — features must never import
``AsyncSession`` directly (enforced by ``import-linter`` Contract 3).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Integer, case, cast, func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB, array
from sqlalchemy.ext.asyncio import AsyncSession

from agentive_backend.infra.db.models import Workflow, WorkflowRun
from agentive_backend.shared.repositories.base import BaseRepo

# Key inside the applicative ``checkpoint`` JSONB counting how many times the
# recovery worker has claimed a run (Story 4.2 AC3). Deliberately stored in
# the existing JSONB rather than a dedicated column: ``_sync_checkpoint``
# rewrites the whole dict after every completed node, so a run that actually
# makes progress resets its own counter for free.
RECOVERY_ATTEMPTS_KEY = "recovery_attempts"


class WorkflowRepo(BaseRepo):
    """Public API surface for Workflow. ALL DB access must go through this class."""

    async def get_by_id(
        self, workflow_id: UUID, *, tenant_id: UUID | None = None
    ) -> Workflow | None:
        async with self.with_tenant(tenant_id) as session:
            return await session.get(Workflow, workflow_id)

    async def require_by_id(self, workflow_id: UUID, *, tenant_id: UUID | None = None) -> Workflow:
        """Fetch by id or raise :class:`NotFoundError` (lookup-or-404, audit A-07).

        Story 4.2 T2.1 — mirror ``AgentTemplateRepo.require_by_id``, NOT
        ``get_by_id`` + a manual ``ValidationError`` (unlike 4.1's
        ``create_workflow``, where ``workflow_id`` never appears in a URL).
        ``POST /workflows/{workflow_id}/runs`` has ``workflow_id`` as the
        URL's primary resource, so an unknown id is a literal 404 (cf Dev
        Notes § "404 vs 422 — symétrique inverse de la décision 4.1").
        """
        return self._require_found(
            await self.get_by_id(workflow_id, tenant_id=tenant_id),
            label="Workflow",
            entity_id=workflow_id,
            context_key="workflow_id",
        )

    async def list_active(
        self, *, tenant_id: UUID | None = None, limit: int = 100
    ) -> list[Workflow]:
        async with self.with_tenant(tenant_id) as session:
            stmt = select(Workflow).where(Workflow.status == "active").limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def create(
        self,
        *,
        name: str,
        dag: dict[str, Any],
        version: int = 1,
        status: str = "active",
        tenant_id: UUID | None = None,
    ) -> Workflow:
        """Convenience wrapper — self-managed transaction.

        Use :meth:`create_in_session` from inside an existing transaction
        when you need to compose the INSERT with another write (e.g.
        publishing an outbox event atomically, Story 4.1 T2).
        """
        async with self.with_tenant(tenant_id) as session:
            return await self.create_in_session(
                session,
                name=name,
                dag=dag,
                version=version,
                status=status,
                tenant_id=tenant_id,
            )

    async def create_in_session(
        self,
        session: AsyncSession,
        *,
        name: str,
        dag: dict[str, Any],
        version: int = 1,
        status: str = "active",
        tenant_id: UUID | None = None,
    ) -> Workflow:
        """INSERT inside the caller's transaction — caller owns commit.

        Story 4.1 T2 — mirror exact of ``AgentTemplateRepo.create_in_session``.
        Used by ``WorkflowService.create_workflow`` to publish
        ``workflow_engine.workflow.created`` in the same transaction as the
        row INSERT (atomicity with the outbox pattern, Story 1.4).
        """
        workflow = Workflow(
            name=name,
            version=version,
            dag=dag,
            status=status,
            tenant_id=tenant_id,
        )
        session.add(workflow)
        await session.flush()
        await session.refresh(workflow)
        return workflow


class WorkflowRunRepo(BaseRepo):
    """Public API surface for WorkflowRun. ALL DB access must go through this class."""

    async def get_by_id(self, run_id: UUID, *, tenant_id: UUID | None = None) -> WorkflowRun | None:
        async with self.with_tenant(tenant_id) as session:
            return await session.get(WorkflowRun, run_id)

    async def get_by_id_in_session(
        self,
        session: AsyncSession,
        run_id: UUID,
    ) -> WorkflowRun | None:
        """SELECT by id inside the caller's transaction (Story 2.4).

        Used by ``AgentRegistryService.instantiate_from_template`` to
        validate the optional ``workflow_run_id`` FK before the instance
        INSERT.

        Note (P-02 CR 2026-05-10) — This validation is NOT race-free under
        concurrent DELETE. ``session.get()`` does not pose a SHARE lock
        under the default ``READ COMMITTED`` isolation, so a concurrent
        transaction can DELETE the workflow_run between this SELECT and
        the instance INSERT. The actual safety net is the FK constraint
        ``agent_instances.workflow_run_id REFERENCES workflow_runs(id)
        ON DELETE SET NULL`` (Story 1.5) : if the run vanishes mid-flight
        the INSERT either succeeds (if the DELETE has not commit yet —
        we read the old snapshot) or the FK CASCADE applies SET NULL
        post-commit. To close the window entirely, switch to
        ``with_for_update(read=True)`` (deferred to Story 4.x when the
        workflow_engine becomes the owner of the run lifecycle and the
        contention pattern is known — D42).
        """
        return await session.get(WorkflowRun, run_id)

    async def require_by_id_in_session(self, session: AsyncSession, run_id: UUID) -> WorkflowRun:
        """In-session fetch by id or raise :class:`NotFoundError` (audit A-07)."""
        return self._require_found(
            await self.get_by_id_in_session(session, run_id),
            label="Workflow run",
            entity_id=run_id,
            context_key="workflow_run_id",
        )

    async def list_by_workflow(
        self,
        workflow_id: UUID,
        *,
        tenant_id: UUID | None = None,
        limit: int = 100,
    ) -> list[WorkflowRun]:
        async with self.with_tenant(tenant_id) as session:
            stmt = (
                select(WorkflowRun)
                .where(WorkflowRun.workflow_id == workflow_id)
                .order_by(WorkflowRun.started_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def create(
        self,
        *,
        workflow_id: UUID,
        correlation_id: UUID,
        status: str = "running",
        tenant_id: UUID | None = None,
    ) -> WorkflowRun:
        """Convenience wrapper — self-managed transaction.

        Use :meth:`create_in_session` from inside an existing transaction
        when you need to compose the INSERT with another write (e.g.
        publishing an outbox event atomically, Story 4.2 T5.2).
        """
        async with self.with_tenant(tenant_id) as session:
            return await self.create_in_session(
                session,
                workflow_id=workflow_id,
                correlation_id=correlation_id,
                status=status,
                tenant_id=tenant_id,
            )

    async def create_in_session(
        self,
        session: AsyncSession,
        *,
        workflow_id: UUID,
        correlation_id: UUID,
        status: str = "running",
        checkpoint: dict[str, Any] | None = None,
        tenant_id: UUID | None = None,
    ) -> WorkflowRun:
        """INSERT inside the caller's transaction — caller owns commit.

        Story 4.2 T5.2 — mirror exact of ``WorkflowRepo.create_in_session``.
        Used by ``WorkflowExecutionService.start_run`` to publish
        ``workflow_engine.workflow_run.started`` in the same transaction as
        the row INSERT (atomicity with the outbox pattern, Story 1.4).

        ``checkpoint`` seeds the applicative summary at creation. Its one
        current use is stamping the caller's ``task_input`` so a run that
        crashes before LangGraph's first checkpoint can still be restarted
        from ``START`` (AC3) — ``_sync_checkpoint`` replaces the whole dict
        once the first node lands.
        """
        run = WorkflowRun(
            workflow_id=workflow_id,
            correlation_id=correlation_id,
            status=status,
            checkpoint=checkpoint,
            tenant_id=tenant_id,
        )
        session.add(run)
        await session.flush()
        await session.refresh(run)
        return run

    async def update_status(
        self,
        run_id: UUID,
        *,
        status: str,
        ended_at: datetime | None = None,
        metrics: dict[str, Any] | None = None,
        only_if_status: str | None = None,
        tenant_id: UUID | None = None,
    ) -> int:
        """Update an existing run. Returns the number of rows updated.

        Returns 0 when the run is filtered out by RLS (cross-tenant attempt),
        doesn't exist, or fails the ``only_if_status`` guard — all silent
        failures by design (the caller decides what a 0 means).

        ``only_if_status`` makes the transition CONDITIONAL, as a compare-and-
        set. Without it, ``completed`` → ``error`` was a legal write: a run
        that finished cleanly could be overwritten as failed by a late error
        on a teardown path, and two concurrent resumes of the same run could
        each stamp their own terminal status. Pass the status the caller
        believes the run is in (in practice ``"running"``), then treat a 0
        rowcount as "someone else already finished this run" — and in
        particular do NOT publish a lifecycle event for a transition that
        never happened.
        """
        async with self.with_tenant(tenant_id) as session:
            values: dict[str, Any] = {"status": status}
            if ended_at is not None:
                values["ended_at"] = ended_at
            if metrics is not None:
                values["metrics"] = metrics
            stmt = update(WorkflowRun).where(WorkflowRun.id == run_id).values(**values)
            if only_if_status is not None:
                stmt = stmt.where(WorkflowRun.status == only_if_status)
            result = await session.execute(stmt)
            # ``rowcount`` is exposed by SQLAlchemy CursorResult (DML executions)
            # but the static type is Result[Any]; getattr keeps mypy strict happy.
            rowcount = getattr(result, "rowcount", None)
            return int(rowcount or 0)

    async def update_checkpoint(
        self,
        run_id: UUID,
        *,
        checkpoint: dict[str, Any],
        last_checkpoint_at: datetime,
        tenant_id: UUID | None = None,
    ) -> int:
        """Sync the applicative checkpoint summary (Story 4.2 T2.2, AC2).

        Mirror ``update_status`` — silent no-op (``0`` rowcount) if the run
        is filtered out by RLS or no longer exists. This is NOT the
        LangGraph technical checkpoint (``checkpoints``/``checkpoint_writes``/
        ``checkpoint_blobs``, written natively by ``AsyncPostgresSaver`` —
        never through this repo) — it's the applicative summary
        (``last_node_id``, ``node_statuses``, ``node_outputs_preview``) read
        by Dashboard/Trace Explorer, plus the staleness timestamp
        :meth:`claim_stale_running` queries on.
        """
        async with self.with_tenant(tenant_id) as session:
            stmt = (
                update(WorkflowRun)
                .where(WorkflowRun.id == run_id)
                .values(checkpoint=checkpoint, last_checkpoint_at=last_checkpoint_at)
            )
            result = await session.execute(stmt)
            rowcount = getattr(result, "rowcount", None)
            return int(rowcount or 0)

    @staticmethod
    def _recovery_attempts_expr() -> Any:
        """``checkpoint->recovery_attempts`` as an int, ``0`` when absent.

        The ``jsonb_typeof`` guard is not paranoia: ``checkpoint`` is
        free-form JSONB, and a non-numeric value would abort the ``::int``
        cast for the WHOLE claim statement — taking every other orphaned run
        in the sweep down with it.
        """
        stored = WorkflowRun.checkpoint[RECOVERY_ATTEMPTS_KEY]
        return case(
            (func.jsonb_typeof(stored) == "number", cast(stored.astext, Integer)),
            else_=literal(0),
        )

    async def claim_stale_running(
        self, *, older_than: datetime, limit: int = 50
    ) -> list[WorkflowRun]:
        """Atomically CLAIM runs stuck in ``running`` with no recent
        checkpoint activity, and return them (Story 4.2 AC3).

        A claim, not a read. The previous ``SELECT``-only version left the
        row untouched, so every 30s tick re-selected the same run and spawned
        another LangGraph execution on the same ``thread_id`` — duplicate
        billed LLM calls, interleaved checkpoint writes, duplicate events.
        Stamping ``last_checkpoint_at = now()`` inside the same statement
        that selects the row makes a claimed run invisible to the next sweep
        for a full ``stale_threshold_s``, and ``FOR UPDATE SKIP LOCKED``
        extends that guarantee across processes (two API replicas can sweep
        concurrently without both claiming the same run).

        ``checkpoint->recovery_attempts`` is incremented on every claim so
        the caller can abandon a poison run instead of resuming it forever;
        it resets on its own as soon as a node completes, because
        ``WorkflowExecutionService._sync_checkpoint`` rewrites the dict.

        ``ORDER BY`` oldest-first makes the ``limit`` deterministic — without
        it, a backlog larger than ``limit`` could starve the same runs sweep
        after sweep.

        No ``tenant_id`` param — deliberate (Story 4.2 T2.3). The recovery
        worker is a system-level background job, not an HTTP-request-scoped
        caller. ``with_tenant(None)`` matches the RLS policy's
        ``tenant_id IS NULL`` clause, so in practice this sweeps the global
        (single-tenant MVP) rows only — every run this codebase writes today.
        Making it genuinely cross-tenant is a multi-tenancy concern, out of
        scope here.
        """
        staleness = func.coalesce(WorkflowRun.last_checkpoint_at, WorkflowRun.started_at)
        async with self.with_tenant(None) as session:
            claimable = (
                select(WorkflowRun.id)
                .where(WorkflowRun.status == "running", staleness < older_than)
                .order_by(staleness)
                .limit(limit)
                .with_for_update(skip_locked=True)
                .scalar_subquery()
            )
            stmt = (
                update(WorkflowRun)
                .where(WorkflowRun.id.in_(claimable))
                .values(
                    last_checkpoint_at=func.now(),
                    checkpoint=func.jsonb_set(
                        func.coalesce(WorkflowRun.checkpoint, cast(literal("{}"), JSONB)),
                        array([RECOVERY_ATTEMPTS_KEY]),
                        func.to_jsonb(self._recovery_attempts_expr() + literal(1)),
                        True,
                    ),
                )
                .returning(WorkflowRun)
                .execution_options(synchronize_session=False)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())
