"""Public API surface for :class:`Workflow` and :class:`WorkflowRun`.

ALL DB access must go through these classes — features must never import
``AsyncSession`` directly (enforced by ``import-linter`` Contract 3).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Integer, Numeric, case, cast, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB, array
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from agentive_backend.infra.db.models import Workflow, WorkflowRun
from agentive_backend.shared.exceptions import ConflictError, DependencyError
from agentive_backend.shared.repositories.base import BaseRepo

# Key inside the applicative ``checkpoint`` JSONB counting how many times the
# recovery worker has claimed a run (Story 4.2 AC3). Deliberately stored in
# the existing JSONB rather than a dedicated column: ``_sync_checkpoint``
# rewrites the whole dict after every completed node, so a run that actually
# makes progress resets its own counter for free.
RECOVERY_ATTEMPTS_KEY = "recovery_attempts"

# Name of the partial unique index declared on ``Workflow.__table_args__``
# and created by migration ``20260912_000002``. Story 4.8 AC1 keys idempotent
# replay on a violation of THIS index and nothing else.
REQUEST_FINGERPRINT_INDEX = "uq_workflow_request_fingerprint"

# Postgres ``unique_violation``. Anything else that arrives as an
# ``IntegrityError`` (23502 not-null, 23503 foreign-key, 23514 check, ...) is
# a server fault, not a replay.
_UNIQUE_VIOLATION_SQLSTATE = "23505"


def _is_request_fingerprint_violation(exc: IntegrityError) -> bool:
    """True only for a duplicate key on ``uq_workflow_request_fingerprint``.

    Story 4.8 review P2. ``create_in_session``'s ``ConflictError`` is not
    merely reported to the client — it routes ``create_workflow`` into the
    idempotent-replay branch, so it must mean one thing and only one thing.
    A blanket ``except IntegrityError`` would answer a NOT NULL or FK failure
    with "already created by an identical request".

    Read through the DBAPI exception rather than the message text: psycopg
    exposes ``sqlstate`` and ``diag.constraint_name`` on the original error.
    Both are checked, and a driver that exposes neither (or a non-Postgres
    backend) yields ``False`` — the conservative answer, since being wrong
    that way surfaces the real error instead of hiding it behind a 409.
    """
    orig = exc.orig
    if getattr(orig, "sqlstate", None) != _UNIQUE_VIOLATION_SQLSTATE:
        return False
    diag = getattr(orig, "diag", None)
    return getattr(diag, "constraint_name", None) == REQUEST_FINGERPRINT_INDEX


# Postgres ``lock_not_available`` — raised when a statement's wait for a row
# lock exceeds ``lock_timeout`` (Story 4.14 AC2). Distinct from
# ``57014 query_canceled`` (``statement_timeout``, not set here) and from
# ``40001 serialization_failure`` (not applicable — this repo never uses
# ``SERIALIZABLE``): only the exact GUC ``create_workflow`` sets is
# translated, so a different Postgres timeout elsewhere is never mistaken
# for this one.
_LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"


def _is_lock_timeout(exc: OperationalError) -> bool:
    """True only for a ``lock_timeout`` expiry, never any other
    ``OperationalError`` (connection loss, admin shutdown, ...).

    Mirrors :func:`_is_request_fingerprint_violation`'s discipline for the
    same reason: the caller turns this into a specific, typed 503, so a
    blanket ``except OperationalError`` would misreport a connection drop
    as "the lock timed out, retry me" instead of surfacing the real fault.
    """
    return getattr(exc.orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE_SQLSTATE


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

    async def get_by_request_fingerprint(
        self, fingerprint: str, *, tenant_id: UUID | None = None
    ) -> Workflow | None:
        """Fetch the workflow a creation request already produced (Story 4.8 AC1).

        Mirror ``AgentTemplateRepo.get_by_name_version`` — a lookup on a
        natural key rather than on the PK. Self-managed transaction ON
        PURPOSE: its only caller is ``WorkflowService.create_workflow``'s
        replay branch, which runs AFTER the INSERT transaction was aborted
        by the unique-index violation. There is no live session to compose
        with at that point, and a Postgres transaction poisoned by an error
        would refuse this SELECT anyway.

        The predicate is the FULL key of ``uq_workflow_request_fingerprint``
        — ``(request_fingerprint, tenant_id)`` — not the fingerprint alone,
        and RLS is not enough to make up the difference. The
        ``tenant_isolation`` policy is ``tenant_id IS NULL OR tenant_id =
        current_setting('app.tenant_id')``, so a tenant-bound session sees
        its own rows AND every global one. Since the index makes ``(fp,
        NULL)`` and ``(fp, X)`` two legal rows, filtering on the fingerprint
        alone would hand ``scalar_one_or_none()`` two rows and raise
        ``MultipleResultsFound`` — a raw sqlalchemy error escaping into
        feature code that ``import-linter`` Contract 3 keeps sqlalchemy-free
        — or, if the tenant's own row were gone, silently return the GLOBAL
        workflow to a tenant that does not own it.
        """
        async with self.with_tenant(tenant_id) as session:
            stmt = select(Workflow).where(
                Workflow.request_fingerprint == fingerprint,
                Workflow.tenant_id == tenant_id,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

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
        request_fingerprint: str | None = None,
    ) -> Workflow:
        """Convenience wrapper — self-managed transaction.

        Use :meth:`create_in_session` from inside an existing transaction
        when you need to compose the INSERT with another write (e.g.
        publishing an outbox event atomically, Story 4.1 T2).

        ``request_fingerprint`` defaults to ``None`` for backward
        compatibility, but a ``None`` here is not free: the unique index is
        PARTIAL on ``request_fingerprint IS NOT NULL``, so the row it writes
        is permanently exempt from the idempotence of Story 4.8 AC1 and a
        replay against it creates a twin. Omit the argument only for a
        creation path that is deliberately not idempotent.

        Raises:
            ConflictError: see :meth:`create_in_session`.
        """
        async with self.with_tenant(tenant_id) as session:
            return await self.create_in_session(
                session,
                name=name,
                dag=dag,
                version=version,
                status=status,
                tenant_id=tenant_id,
                request_fingerprint=request_fingerprint,
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
        request_fingerprint: str | None = None,
    ) -> Workflow:
        """INSERT inside the caller's transaction — caller owns commit.

        Story 4.1 T2 — mirror exact of ``AgentTemplateRepo.create_in_session``.
        Used by ``WorkflowService.create_workflow`` to publish
        ``workflow_engine.workflow.created`` in the same transaction as the
        row INSERT (atomicity with the outbox pattern, Story 1.4).

        Story 4.8 T2.1 — ``request_fingerprint`` is the idempotence key of
        ``POST /api/v1/workflows`` (AC1), and the ``IntegrityError`` it can
        raise is translated here into a domain :class:`ConflictError` for the
        same reason as in ``AgentTemplateRepo``: feature code must stay free
        of ``sqlalchemy`` imports (``import-linter`` Contract 3).

        That translation is DELIBERATELY NARROW, and this is where this
        method stops mirroring ``AgentTemplateRepo.create_in_session``.
        There, a blanket ``except IntegrityError`` is harmless: the caller
        surfaces the 409 and stops. Here the ``ConflictError`` STEERS
        ``create_workflow`` into its replay branch, so mislabelling a
        NOT NULL / FK / CHECK failure as "already created by an identical
        request" would answer a genuine server fault with a 409 asserting a
        duplicate that never existed. We therefore match on SQLSTATE 23505
        (``unique_violation``) AND on the index name, and let anything else
        propagate untouched to the 500 it deserves.

        Note that the flush failing ABORTS the caller's whole transaction —
        that is not a side effect to work around but the mechanism AC1 relies
        on. ``create_workflow`` publishes its outbox event AFTER this call in
        the same transaction, so a collision guarantees no second event was
        written without needing a guard to say so.

        Story 4.14 AC2 — when the caller opened this transaction with
        ``with_tenant(..., lock_timeout_ms=...)`` (``create_workflow`` does),
        a LOSING concurrent replay can wait on
        ``uq_workflow_request_fingerprint`` for longer than that bound. The
        resulting ``OperationalError`` (SQLSTATE ``55P03``,
        ``lock_not_available``) is translated to a domain
        :class:`DependencyError` for the same reason the unique violation
        above is: feature code must stay free of ``sqlalchemy`` imports, and
        the caller needs a typed, retriable signal — not a raw driver
        exception — for "the wait was bounded and it expired," which is a
        different fact from either "you already created this" (409) or
        "your input is invalid" (422).

        Raises:
            ConflictError: If ``(request_fingerprint, tenant_id)`` already
                exists, i.e. this exact creation request already produced a
                workflow. The caller turns that into an idempotent replay.
            DependencyError: The transaction's ``lock_timeout`` expired
                waiting for a conflicting writer to finish (503, retriable).
            IntegrityError: Any OTHER constraint violation, re-raised as-is.
            OperationalError: Any OTHER database-level failure, re-raised
                as-is.
        """
        workflow = Workflow(
            name=name,
            version=version,
            dag=dag,
            status=status,
            tenant_id=tenant_id,
            request_fingerprint=request_fingerprint,
        )
        session.add(workflow)
        try:
            await session.flush()
        except IntegrityError as exc:
            if not _is_request_fingerprint_violation(exc):
                raise
            raise ConflictError(
                detail=f"Workflow '{name}' was already created by an identical request",
                context={"name": name, "request_fingerprint": request_fingerprint},
            ) from exc
        except OperationalError as exc:
            if not _is_lock_timeout(exc):
                raise
            raise DependencyError(
                detail=f"Timed out waiting for a lock while creating workflow '{name}'",
                context={"name": name},
            ) from exc
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
        mise_en_place: dict[str, Any] | None = None,
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

        ``mise_en_place`` (Story 4.5 AC1/AC3, T4.3) — the pre-workflow report
        (``dataclasses.asdict`` of a ``MiseEnPlaceReport``), or ``None`` for
        callers that predate that story. Optional keyword, default-``None``,
        so the sole existing caller (``WorkflowExecutionService.start_run``,
        confirmed by grep — no other call site) stays source-compatible.
        """
        run = WorkflowRun(
            workflow_id=workflow_id,
            correlation_id=correlation_id,
            status=status,
            checkpoint=checkpoint,
            mise_en_place=mise_en_place,
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
        only_if_control_signal: str | None = None,
        clear_control: bool = False,
        touch_last_checkpoint: bool = False,
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

        ``clear_control`` (Story 4.6 T1.6) nulls ``control_signal`` and
        ``control_requested_at`` in the SAME statement as the transition, so
        "the run is now paused" and "the pause request has been consumed"
        commit together or not at all. Split across two statements, a crash
        in between would leave a ``paused`` run still carrying a ``"pause"``
        signal — which its next resume would observe and act on, pausing it
        again before a single node ran.

        ``only_if_status`` deliberately stays a single value rather than a
        tuple: every caller in Story 4.6 transitions from exactly one known
        status, and widening the guard would weaken the compare-and-set for
        the callers that do not need it.

        ``only_if_control_signal`` extends the compare-and-set to the SIGNAL
        (review lot 9, F2). ``only_if_status`` alone is not enough for the
        driver's settle write, because the status it guards on does not
        change during the window it needs to protect: see
        :meth:`update_status_in_session`.

        ``touch_last_checkpoint`` stamps ``last_checkpoint_at = now()`` in the
        same statement. Required on any transition BACK to ``running``: see
        :meth:`update_status_in_session`.

        Convenience wrapper — self-managed transaction. Use
        :meth:`update_status_in_session` from inside an existing transaction
        when the write must commit with something else (an outbox event).
        """
        async with self.with_tenant(tenant_id) as session:
            return await self.update_status_in_session(
                session,
                run_id,
                status=status,
                ended_at=ended_at,
                metrics=metrics,
                only_if_status=only_if_status,
                only_if_control_signal=only_if_control_signal,
                clear_control=clear_control,
                touch_last_checkpoint=touch_last_checkpoint,
            )

    async def update_status_in_session(
        self,
        session: AsyncSession,
        run_id: UUID,
        *,
        status: str,
        ended_at: datetime | None = None,
        metrics: dict[str, Any] | None = None,
        only_if_status: str | None = None,
        only_if_control_signal: str | None = None,
        clear_control: bool = False,
        touch_last_checkpoint: bool = False,
    ) -> int:
        """UPDATE inside the caller's transaction — caller owns commit.

        Mirror of :meth:`WorkflowRepo.create_in_session`. Story 4.6 AC1
        requires the control event and the status write to "commit together
        or not at all"; that is only true if the publish joins THIS session,
        which the self-managed wrapper above cannot offer (it commits on
        exit, before the caller's publish has even run).

        ``touch_last_checkpoint`` exists for the ``paused → running``
        transition. ``claim_stale_running`` decides staleness from
        ``COALESCE(last_checkpoint_at, started_at)``, so a run resumed after
        sitting paused longer than the recovery worker's stale threshold
        (``recovery.derive_stale_threshold_s``) would be
        claimed by the recovery sweep within one tick and driven a SECOND
        time, concurrently, on the same ``thread_id`` — duplicate billed LLM
        calls and interleaved checkpoint writes. Stamping the resume as
        activity is what keeps the sweep off a run that just came back.

        ``only_if_control_signal`` makes the write conditional on the signal
        still being the one the caller READ. It exists for exactly one
        caller, ``WorkflowExecutionService._observe_control``, and for
        exactly one race (review lot 9, F2):

        1. the driver reads ``control_signal = "pause"``;
        2. an operator escalates — ``POST /cancel`` passes
           :meth:`request_control_in_session`'s guard, because the status is
           still ``running`` and ``cancel`` is allowed to override a pending
           ``pause`` — and is answered **202**;
        3. the driver's settle write lands: ``status = 'paused'`` with
           ``clear_control=True``, which ERASES that cancel.

        The run then sits ``paused`` with no pending signal, so there is
        nothing left for anyone to observe: the caller was promised a
        cancellation that will never happen and no ``cancelled`` event will
        ever follow the ``cancel_requested`` already on the bus. Guarding on
        ``status`` cannot catch it — the status is ``running`` throughout the
        window and only the SIGNAL changed. Note that this race did not
        exist before ``Transition.overrides`` (review `BS1`) allowed a
        ``cancel`` to replace a pending ``pause``; letting the escalation
        through without the symmetric guard on the settle side is what
        opened it.
        """
        values: dict[str, Any] = {"status": status}
        if ended_at is not None:
            values["ended_at"] = ended_at
        if metrics is not None:
            values["metrics"] = metrics
        if clear_control:
            values["control_signal"] = None
            values["control_requested_at"] = None
        if touch_last_checkpoint:
            values["last_checkpoint_at"] = func.now()
        stmt = update(WorkflowRun).where(WorkflowRun.id == run_id).values(**values)
        if only_if_status is not None:
            stmt = stmt.where(WorkflowRun.status == only_if_status)
        if only_if_control_signal is not None:
            stmt = stmt.where(WorkflowRun.control_signal == only_if_control_signal)
        result = await session.execute(stmt)
        # ``rowcount`` is exposed by SQLAlchemy CursorResult (DML executions)
        # but the static type is Result[Any]; getattr keeps mypy strict happy.
        rowcount = getattr(result, "rowcount", None)
        return int(rowcount or 0)

    async def request_control(
        self,
        run_id: UUID,
        *,
        signal: str,
        only_if_status: str = "running",
        overrides: Sequence[str] = (),
        tenant_id: UUID | None = None,
    ) -> int:
        """Record a PENDING pause/cancel request (Story 4.6 T1.3, AC1).

        Compare-and-set on two conditions, and the second one is the
        interesting one:

        * ``status = only_if_status`` — the usual guard; a run that finished
          between the caller's read and this write must not gain a signal
          nobody will ever observe.
        * ``control_signal IS NULL`` — a run that ALREADY carries a pending
          request refuses a second one (rowcount 0, which the service turns
          into a 409). Without it, a ``cancel`` would silently overwrite a
          ``pause`` that the driver had not yet reached: the caller who asked
          to pause would get a cancelled run and no indication that their
          request had been dropped. Whoever asks first wins, and the loser is
          told.

        Returns the rowcount — 0 means "not applied", for any of those
        reasons or RLS, exactly like :meth:`update_status`.

        Convenience wrapper — self-managed transaction. Use
        :meth:`request_control_in_session` when the write must commit with
        its audit event (Story 4.6 AC1).
        """
        async with self.with_tenant(tenant_id) as session:
            return await self.request_control_in_session(
                session,
                run_id,
                signal=signal,
                only_if_status=only_if_status,
                overrides=overrides,
            )

    async def request_control_in_session(
        self,
        session: AsyncSession,
        run_id: UUID,
        *,
        signal: str,
        only_if_status: str = "running",
        overrides: Sequence[str] = (),
    ) -> int:
        """Record a pending request inside the caller's transaction.

        See :meth:`update_status_in_session` for why Story 4.6 AC1 needs
        this shape rather than the self-managed wrapper.

        ``overrides`` lists the pending signals this write may REPLACE, on top
        of the default "no signal pending". The domain owns which ones (see
        ``domain.run_control.Transition.overrides``); the repo only applies
        them. Empty keeps the strict first-writer-wins guard.
        """
        pending_ok: ColumnElement[bool] = WorkflowRun.control_signal.is_(None)
        if overrides:
            pending_ok = or_(pending_ok, WorkflowRun.control_signal.in_(tuple(overrides)))
        stmt = (
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run_id,
                WorkflowRun.status == only_if_status,
                pending_ok,
            )
            .values(control_signal=signal, control_requested_at=func.now())
        )
        result = await session.execute(stmt)
        rowcount = getattr(result, "rowcount", None)
        return int(rowcount or 0)

    # There is deliberately NO standalone `clear_control()`. Every path that
    # consumes or invalidates a signal also changes the status, so each one
    # uses `update_status(..., clear_control=True)` and gets the two writes in
    # ONE statement. A separate unconditional `UPDATE ... WHERE id = :id`
    # would have no status guard and no signal guard, so a caller "cleaning
    # up" a signal it had just consumed would also erase one written
    # microseconds earlier by a concurrent request — answering that caller
    # 202 for a cancellation that then never happens.

    async def get_control_signal(
        self, run_id: UUID, *, tenant_id: UUID | None = None
    ) -> str | None:
        """Read just ``control_signal`` (Story 4.6 T1.5).

        A targeted ``SELECT`` of one column, NOT ``get_by_id``: the driver
        calls this once per superstep, and the rest of the row — including
        the ``checkpoint`` JSONB, which holds every node's output preview —
        is not needed to answer "should I stop?".

        ``None`` covers both "no request pending" and "no such run"; the
        driver treats them identically (keep running), so distinguishing them
        would buy nothing.
        """
        async with self.with_tenant(tenant_id) as session:
            stmt = select(WorkflowRun.control_signal).where(WorkflowRun.id == run_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

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

    @staticmethod
    def _checkpoint_object_expr() -> Any:
        """``checkpoint`` coerced to a JSON OBJECT — the only shape
        ``jsonb_set`` can target (Story 4.14 AC1/T1, review-confirmed root
        cause of the intermittent ``cannot set path in scalar`` failure of
        :meth:`claim_stale_running`).

        A plain ``coalesce(checkpoint, '{}'::jsonb)`` — the previous
        guard — only substitutes on SQL ``NULL``. It does NOT cover a
        ``checkpoint`` holding the JSON literal ``null`` (nor any other
        scalar), which is non-NULL at the SQL level and makes
        ``jsonb_set``'s first argument a scalar — an error, for the WHOLE
        UPDATE, not just this row.

        That shape is not exotic: every :class:`WorkflowRun` is created
        with ``checkpoint=None`` before its first superstep
        (``create_in_session``'s default, used by every real
        ``start_run``). ``WorkflowRun.checkpoint`` is
        ``postgresql.JSONB`` with the SQLAlchemy default
        ``none_as_null=False``, so binding that Python ``None`` serializes
        to ``'null'::jsonb`` — NOT SQL ``NULL`` — the moment the ORM
        attribute is explicitly set (verified against a live Postgres: for
        such a row, ``checkpoint IS NULL`` is false and
        ``jsonb_typeof(checkpoint)`` is ``'null'``). A run that crashes
        before its first ``_sync_checkpoint_safely`` call — the exact
        scenario the recovery sweep exists to catch — keeps that shape
        forever, and the next sweep that reaches it fails to claim ANY of
        up to 50 batched orphans, not just the poisoned one.

        ``jsonb_typeof(...) = 'object'`` is the same defense
        :meth:`_recovery_attempts_expr` already applies one level down
        (there, on the VALUE stored at a key; here, on the checkpoint
        itself) — mirrored rather than special-cased, and it covers a
        missing column (SQL ``NULL``), a JSON ``null``, and any other
        scalar (number/string/bool) uniformly, falling back to an empty
        object in every case.
        """
        stored = WorkflowRun.checkpoint
        return case(
            (func.jsonb_typeof(stored) == "object", stored),
            else_=cast(literal("{}"), JSONB),
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
                        self._checkpoint_object_expr(),
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

    async def aggregate_routing_modes(
        self, workflow_id: UUID, *, tenant_id: UUID | None = None
    ) -> tuple[int, int, int]:
        """``(runs_counted, deterministic, llm_escalated)`` across every run
        of ``workflow_id`` (Story 4.3 AC3 T10.1).

        Tolerant of rows that predate this story (``metrics`` has no
        ``routing`` key at all — every Story 4.1/4.2 run already in the
        database) AND of a corrupted/non-numeric value under that key: either
        case counts as ``0`` for that row rather than aborting the whole
        aggregate (the cast is per-row, so ONE bad row would otherwise poison
        the count for every other run of the same workflow — mirror
        ``_recovery_attempts_expr``'s existing guard on this exact hazard).

        ``jsonb_typeof`` alone is NOT that guarantee: it answers ``'number'``
        for ``1.5`` exactly as for ``1``, and ``'1.5'::int`` raises
        ``invalid input syntax for type integer``. The cast therefore goes
        through ``numeric`` + ``floor`` — which accepts every JSON number —
        so the type guard and the cast agree on what they accept. Without it
        a single fractional value made ``GET /routing-stats`` return 500 for
        the entire workflow, which is precisely the poisoning this guard is
        here to prevent.
        """

        def _routing_count(key: str) -> Any:
            path = WorkflowRun.metrics[("routing", key)]
            return case(
                (
                    func.jsonb_typeof(path) == "number",
                    cast(func.floor(cast(path.astext, Numeric)), Integer),
                ),
                else_=literal(0),
            )

        async with self.with_tenant(tenant_id) as session:
            stmt = select(
                func.count(),
                func.coalesce(func.sum(_routing_count("deterministic")), literal(0)),
                func.coalesce(func.sum(_routing_count("llm_escalated")), literal(0)),
            ).where(WorkflowRun.workflow_id == workflow_id)
            result = await session.execute(stmt)
            row = result.one()
            return int(row[0]), int(row[1]), int(row[2])

    async def aggregate_token_reduction(
        self, workflow_id: UUID, *, tenant_id: UUID | None = None
    ) -> tuple[int, int, int]:
        """``(runs_counted, raw_tokens_replaced, summary_tokens)`` across
        every run of ``workflow_id`` (Story 4.7 AC3).

        Mirror ``aggregate_routing_modes`` EXACTLY — same defensive
        ``jsonb_typeof``/``numeric``/``floor`` cast per row (a single
        corrupted or fractional value must not poison the whole workflow's
        aggregate), reading ``metrics["handoffs"]`` instead of
        ``metrics["routing"]``. Tolerant of rows that predate this story
        (no ``handoffs`` key at all) the same way.
        """

        def _handoff_count(key: str) -> Any:
            path = WorkflowRun.metrics[("handoffs", key)]
            return case(
                (
                    func.jsonb_typeof(path) == "number",
                    cast(func.floor(cast(path.astext, Numeric)), Integer),
                ),
                else_=literal(0),
            )

        async with self.with_tenant(tenant_id) as session:
            stmt = select(
                func.count(),
                func.coalesce(func.sum(_handoff_count("raw_tokens_replaced")), literal(0)),
                func.coalesce(func.sum(_handoff_count("summary_tokens")), literal(0)),
            ).where(WorkflowRun.workflow_id == workflow_id)
            result = await session.execute(stmt)
            row = result.one()
            return int(row[0]), int(row[1]), int(row[2])
