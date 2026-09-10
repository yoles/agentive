"""Public API surface for :class:`Workflow` and :class:`WorkflowRun`.

ALL DB access must go through these classes — features must never import
``AsyncSession`` directly (enforced by ``import-linter`` Contract 3).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agentive_backend.infra.db.models import Workflow, WorkflowRun
from agentive_backend.shared.repositories.base import BaseRepo


class WorkflowRepo(BaseRepo):
    """Public API surface for Workflow. ALL DB access must go through this class."""

    async def get_by_id(
        self, workflow_id: UUID, *, tenant_id: UUID | None = None
    ) -> Workflow | None:
        async with self.with_tenant(tenant_id) as session:
            return await session.get(Workflow, workflow_id)

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
        async with self.with_tenant(tenant_id) as session:
            run = WorkflowRun(
                workflow_id=workflow_id,
                correlation_id=correlation_id,
                status=status,
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
        tenant_id: UUID | None = None,
    ) -> int:
        """Update an existing run. Returns the number of rows updated.

        Returns 0 when the run is filtered out by RLS (cross-tenant attempt)
        or simply doesn't exist — both are silent failures by design (the
        caller can ``raise`` if 0 is unexpected).
        """
        async with self.with_tenant(tenant_id) as session:
            values: dict[str, Any] = {"status": status}
            if ended_at is not None:
                values["ended_at"] = ended_at
            if metrics is not None:
                values["metrics"] = metrics
            stmt = update(WorkflowRun).where(WorkflowRun.id == run_id).values(**values)
            result = await session.execute(stmt)
            # ``rowcount`` is exposed by SQLAlchemy CursorResult (DML executions)
            # but the static type is Result[Any]; getattr keeps mypy strict happy.
            rowcount = getattr(result, "rowcount", None)
            return int(rowcount or 0)
