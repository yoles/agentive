"""Public API surface for :class:`Workflow` and :class:`WorkflowRun`.

ALL DB access must go through these classes — features must never import
``AsyncSession`` directly (enforced by ``import-linter`` Contract 3).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Integer, Numeric, asc, case, cast, func, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, array
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from agentive_backend.infra.db.models import Workflow, WorkflowRun
from agentive_backend.shared.exceptions import ConflictError, DependencyError
from agentive_backend.shared.repositories.base import BaseRepo, is_lock_timeout

# Key inside the applicative ``checkpoint`` JSONB counting how many times the
# recovery worker has claimed a run (Story 4.2 AC3). Deliberately stored in
# the existing JSONB rather than a dedicated column: ``_sync_checkpoint``
# rewrites the whole dict after every completed node, so a run that actually
# makes progress resets its own counter for free.
RECOVERY_ATTEMPTS_KEY = "recovery_attempts"

# Key for the cluster-wide advisory lock the checkpoint purge holds. Arbitrary
# but FIXED: every replica must name the same integer or the lease excludes
# nobody. Kept here, beside the only method that takes it, so a second
# advisory lock cannot be added elsewhere with a colliding value by accident.
CHECKPOINT_PURGE_LOCK_KEY = 4_100_000_001

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


# Upper bound of the `INTEGER` the metrics aggregates cast into. Clamped
# BEFORE the narrowing cast, which would otherwise raise `22003` and abort
# the whole aggregate. See `WorkflowRunRepo._non_negative_metrics_count`.
_METRICS_COUNT_CEILING = 2_147_483_647


def _terminal_at() -> ColumnElement[datetime]:
    """When a terminal run ended, with a fallback (Story 4.15 AC3).

    ``list_purgeable`` used ``ended_at IS NOT NULL`` as a hard filter, which
    made a terminal run whose ``ended_at`` was never stamped invisible to the
    purge **forever**, with nothing reporting it. The case is not demonstrated
    reachable — every terminal write site in the application passes
    ``ended_at``, verified — so this is a defensive hole rather than an
    observed bug.

    It is CLOSED rather than merely signalled: a terminal run is finished by
    definition, so its blobs are dead weight whatever column dates its end.
    ``COALESCE`` always yields something to compare against the retention
    window, and ``started_at`` is ``NOT NULL``, so the expression can never be
    NULL and silently drop a row again.

    A ``CHECK`` constraint was rejected as the alternative: it would cost a
    migration on a potentially large table for a case never observed, and it
    would make a write FAIL rather than repair the omission.
    """
    return func.coalesce(
        WorkflowRun.ended_at, WorkflowRun.last_checkpoint_at, WorkflowRun.started_at
    )


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

    async def list_by_name(
        self, name: str, *, tenant_id: UUID | None = None, limit: int = 100
    ) -> list[Workflow]:
        """Tous les workflows portant ce nom (Story 5.2 T5.2).

        Rend une LISTE, et c'est le fait que cette méthode existe pour
        rendre visible : ``workflows.name`` n'est **pas** unique
        (``infra/db/models.py``), et l'empreinte d'idempotence de
        ``create_workflow`` couvre le DAG. Changer le DAG d'un workflow de
        provisioning crée donc une SECONDE ligne homonyme, et le
        ``workflow_id`` que l'opérateur avait noté continue de pointer sur
        l'ancienne. Un appelant qui veut « le » workflow d'un nom doit
        d'abord constater combien il y en a.

        Ordonné par ``created_at, id`` : le provisioning doit pouvoir dire
        « la plus ancienne » sans dépendre de l'ordre de retour de Postgres.

        ``limit`` par défaut à 100, comme :meth:`list_active` juste en dessous
        (revue 5.2). La méthode était sans borne, alors que sa raison d'être
        est précisément d'observer une croissance non bornée : dans le scénario
        qui la motive, elle chargeait tous les ``Workflow`` homonymes — leur
        ``dag`` JSONB compris — pour n'en afficher que les ``id``.
        """
        async with self.with_tenant(tenant_id) as session:
            stmt = (
                select(Workflow)
                .where(Workflow.name == name, Workflow.tenant_id == tenant_id)
                .order_by(asc(Workflow.created_at), asc(Workflow.id))
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

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
            # Narrows the boundary translation in `BaseRepo.with_tenant` to a
            # message that names the workflow. Not the only guard: the
            # transaction-scoped `lock_timeout` arms EVERY statement of the
            # body, so `with_tenant` catches whatever expires outside this
            # flush (the `SELECT ... FOR SHARE` above it, notably).
            if not is_lock_timeout(exc):
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
        status: str | None = None,
    ) -> list[WorkflowRun]:
        """Les runs d'un workflow, du plus récent au plus ancien.

        ``status`` filtre EN SQL, et ce n'est pas une commodité : filtrer
        après la troncature fait que ``limit`` compte des runs qui seront
        jetés. Vingt runs en échec récents masquaient ainsi un historique
        `completed` arbitrairement long, et l'appelant retombait sur son
        estimation heuristique en croyant n'avoir aucune mesure.
        """
        async with self.with_tenant(tenant_id) as session:
            stmt = select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id)
            if status is not None:
                stmt = stmt.where(WorkflowRun.status == status)
            stmt = stmt.order_by(WorkflowRun.started_at.desc()).limit(limit)
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
        acknowledgement: dict[str, Any] | None = None,
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

        ``acknowledgement`` (Story 5.1 AC2) — same shape of keyword, same
        reason, and deliberately written HERE rather than by a later
        ``UPDATE``: the client may connect to the SSE stream the instant the
        ``201`` lands, so the row must already carry what the first frame
        has to say. Unlike ``checkpoint``, it is never rewritten.
        """
        run = WorkflowRun(
            workflow_id=workflow_id,
            correlation_id=correlation_id,
            status=status,
            checkpoint=checkpoint,
            mise_en_place=mise_en_place,
            acknowledgement=acknowledgement,
            tenant_id=tenant_id,
        )
        session.add(run)
        await session.flush()
        await session.refresh(run)
        return run

    async def count_running(
        self,
        workflow_id: UUID,
        *,
        tenant_id: UUID | None = None,
        alive_since: datetime | None = None,
    ) -> int:
        """How many runs of ``workflow_id`` currently sit ``running``
        (Story 4.9 AC2). Self-managed session — an early, best-effort
        admission check called BEFORE the (expensive) Mise en Place gate,
        so a request already over the concurrency cap does not pay for
        MCP probes or a Dry Run just to be refused. Racy by construction
        against a concurrent create/resume; :meth:`count_running_in_session`
        is the authoritative check taken in the same transaction as the
        write.
        """
        async with self.with_tenant(tenant_id) as session:
            return await self.count_running_in_session(
                session, workflow_id=workflow_id, alive_since=alive_since
            )

    async def count_running_in_session(
        self,
        session: AsyncSession,
        *,
        workflow_id: UUID,
        alive_since: datetime | None = None,
    ) -> int:
        """Same count as :meth:`count_running`, taken in the caller's own
        transaction — the authoritative check, run immediately before the
        row write it gates (``create_in_session``'s INSERT, or
        ``update_status_in_session``'s resume UPDATE) so the window between
        counting and writing is as small as this repo's transactions get.

        Still not a hard linearizability guarantee: two concurrent callers
        in ``READ COMMITTED`` can both count the same value before either
        commits its write, same as every other count-then-act check in this
        module. Closing that race needs a serialized lock (e.g. an advisory
        lock keyed on ``workflow_id``) that AC2 never asked for — this is a
        mechanical ceiling against runaway bursts, not a linearizable quota.

        ``alive_since`` excludes ``running`` rows whose driver has gone
        silent for longer than it — the same ``COALESCE(last_checkpoint_at,
        started_at)`` staleness :meth:`claim_stale_running` uses, so both
        answer "is this run still being driven?" the same way. Without it a
        crashed process's rows counted against the cap until the recovery
        sweep reclaimed them (~1548 s at default settings), which turned one
        backend crash into ~26 minutes of 429 on every affected workflow.
        ``None`` counts every ``running`` row.
        """
        stmt = (
            select(func.count())
            .select_from(WorkflowRun)
            .where(WorkflowRun.workflow_id == workflow_id, WorkflowRun.status == "running")
        )
        if alive_since is not None:
            stmt = stmt.where(
                func.coalesce(WorkflowRun.last_checkpoint_at, WorkflowRun.started_at) >= alive_since
            )
        result = await session.execute(stmt)
        return result.scalar_one()

    async def update_status(
        self,
        run_id: UUID,
        *,
        status: str,
        ended_at: datetime | None = None,
        metrics: dict[str, Any] | None = None,
        only_if_status: str | None = None,
        only_if_control_signal: str | None = None,
        only_if_control_signal_unset: bool = False,
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
                only_if_control_signal_unset=only_if_control_signal_unset,
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
        only_if_control_signal_unset: bool = False,
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

        ``only_if_control_signal_unset`` is the same compare-and-set for a
        caller that read NO signal: ``only_if_control_signal=None`` cannot
        express it, since ``None`` already means "do not guard".
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
        if only_if_control_signal_unset:
            stmt = stmt.where(WorkflowRun.control_signal.is_(None))
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

        ``control_requested_at`` moves only when the SIGNAL changes. It
        answers "how long has this request been waiting?", which is what a
        caller reads to tell a request the driver has simply not reached yet
        from one whose driver is dead. A replayed request — the same signal
        written again, which ``overrides`` deliberately permits so a retrying
        client gets 202 rather than 409 — is the same request retried, not a
        new one, so re-stamping it would peg the field at "just now" for
        exactly the client behaviour that replay support exists to serve. An
        ESCALATION (``pause`` -> ``cancel``) genuinely is a new request and
        does move it.
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
            .values(
                control_signal=signal,
                control_requested_at=case(
                    (
                        WorkflowRun.control_signal.is_distinct_from(signal),
                        func.now(),
                    ),
                    else_=WorkflowRun.control_requested_at,
                ),
            )
        )
        result = await session.execute(stmt)
        rowcount = getattr(result, "rowcount", None)
        return int(rowcount or 0)

    # There is deliberately NO standalone, UNCONDITIONAL `clear_control()`.
    # Every path that CONSUMES a signal also changes the status, so each one
    # uses `update_status(..., clear_control=True)` and gets the two writes in
    # ONE statement. A separate unconditional `UPDATE ... WHERE id = :id`
    # would have no status guard and no signal guard, so a caller "cleaning
    # up" a signal it had just consumed would also erase one written
    # microseconds earlier by a concurrent request — answering that caller
    # 202 for a cancellation that then never happens.
    #
    # `retract_control_in_session` below is NOT that method: it is
    # conditional on `control_signal IS NOT NULL` (Story 4.9 AC6/T6.5),
    # which closes exactly the gap the paragraph above warns about — a
    # retraction only ever clears a signal that is STILL there to clear, so
    # it cannot erase one a concurrent request writes afterwards, and it
    # never touches `status`.

    async def retract_control_in_session(
        self, session: AsyncSession, run_id: UUID, *, only_if_status: str = "running"
    ) -> int:
        """Clear a pending, not-yet-observed `pause`/`cancel` (Story 4.9
        AC6/T6.5) — "I asked for the wrong thing, undo it before the driver
        acts on it."

        Compare-and-set on TWO conditions, mirroring
        :meth:`request_control_in_session`'s own discipline in reverse:

        * ``status = only_if_status`` — a run that already settled (the
          driver observed the signal and moved on, or reached a terminal
          status by itself) has nothing left to retract.
        * ``control_signal IS NOT NULL`` — nothing pending means nothing to
          clear; a 0 here answers the same way a stale request does.

        Never touches ``status`` — retraction is purely a signal-column
        operation, unlike every other write in this class.
        """
        stmt = (
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run_id,
                WorkflowRun.status == only_if_status,
                WorkflowRun.control_signal.is_not(None),
            )
            .values(control_signal=None, control_requested_at=None)
        )
        result = await session.execute(stmt)
        rowcount = getattr(result, "rowcount", None)
        return int(rowcount or 0)

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

        How a row acquires that shape: ``WorkflowRun.checkpoint`` is
        ``postgresql.JSONB`` with the SQLAlchemy default
        ``none_as_null=False``, so binding a Python ``None`` serializes to
        ``'null'::jsonb`` — NOT SQL ``NULL`` — the moment the ORM attribute
        is explicitly set (verified against a live Postgres: for such a row,
        ``checkpoint IS NULL`` is false and ``jsonb_typeof(checkpoint)`` is
        ``'null'``). One poisoned row fails the claim for ALL of up to 50
        batched orphans, not just itself — which is what made the failure
        intermittent and suite-wide.

        **Correction (review 4.14, B-01).** Story 4.14 justified this guard
        by claiming "every ``WorkflowRun`` is created with
        ``checkpoint=None`` before its first superstep, used by every real
        ``start_run``". That is **false**, and the same claim was repeated in
        the story's Completion Notes and in commit ``5506fb8``'s message.
        :meth:`WorkflowExecutionService.start_run` always passes a populated
        ``checkpoint={"task_input": ..., TEMPLATE_FINGERPRINTS_KEY: ...}``
        (it has to — a run that dies before LangGraph's first checkpoint is
        restarted from ``START`` using exactly that stored input), so no row
        it creates has this shape. The only path reaching
        ``create_in_session``'s ``checkpoint=None`` default is the thin
        :meth:`WorkflowRunRepo.create` wrapper, whose callers in this repo
        are all **test harnesses**. The bug that was fixed was therefore a
        test-harness artefact, not a class of poisoned production rows.

        The guard is kept regardless, and deliberately: it is two SQL
        operators wide, it makes the batch resilient to any non-object shape
        from any future writer, and "no current caller produces this" is a
        property of today's call sites rather than of the column, whose type
        admits every JSON shape. What is corrected here is the *claim*, not
        the code.

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

    @staticmethod
    def _non_negative_metrics_count(metrics_key: str, field: str) -> Any:
        """One ``metrics[(metrics_key, field)]`` value, floored to a
        non-negative int — ``0`` for anything else: missing, non-numeric,
        or (Story 4.13 AC1) NEGATIVE.

        Shared by :meth:`aggregate_routing_modes` and
        :meth:`aggregate_token_reduction` — a single copy specifically so
        the two cannot drift the way they had: both carried the identical
        ``jsonb_typeof(...) = 'number'`` guard (closing the non-numeric/
        fractional case, Story 4.3/4.7), and both left it silently open to
        a NEGATIVE JSON number, which the guard's own type check accepts
        (a sign is not a type) and ``floor``/``sum`` propagate straight
        through to the response's ``Field(ge=0)`` — a 500 for every OTHER
        run of the workflow, from one corrupted row.

        The clamp is deliberately SILENT, like its sibling guard's
        treatment of a non-numeric value — not a rejected/logged row. A
        negative token count or a negative routing tally cannot be
        partially salvaged into a meaningful positive number, so folding it
        to ``0`` (i.e. "this row contributed nothing knowable") is the same
        answer the aggregate already gives a row with no ``routing``/
        ``handoffs`` key at all. Consistency with the existing corruption
        handling was chosen over inventing a second, differently-shaped
        response (reject + log) for a sibling failure mode of the same
        field.

        The clamp runs in ``numeric`` space, BEFORE the cast to ``INTEGER``,
        and bounds both sides. Clamping after the cast defends nothing:
        ``CAST(... AS INTEGER)`` is evaluated first and raises
        ``22003 numeric_value_out_of_range`` on a value outside int32,
        aborting the statement for every other row. ``jsonb`` stores numbers
        as ``numeric``, so only the narrowing cast can overflow.

        Both bounds live in the result expression, never in the outer
        condition: ``jsonb_typeof(...) = 'number' AND <arithmetic>`` would
        let the planner evaluate the arithmetic against a non-numeric value
        in whatever order it likes, which is the hazard the outer guard
        exists to avoid.
        """
        path = WorkflowRun.metrics[(metrics_key, field)]
        return case(
            (
                func.jsonb_typeof(path) == "number",
                cast(
                    func.least(
                        func.greatest(func.floor(cast(path.astext, Numeric)), literal(0)),
                        literal(_METRICS_COUNT_CEILING),
                    ),
                    Integer,
                ),
            ),
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

    @asynccontextmanager
    async def purge_lease(self) -> AsyncIterator[bool]:
        """Hold a cluster-wide lease on the checkpoint purge, or report that
        somebody else has it.

        Yields ``True`` when this process may purge, ``False`` when another
        already is. A Postgres SESSION-level advisory lock rather than a
        transaction-level one, because the pass it guards spans many
        transactions: the claim, each ``adelete_thread``, and each marker
        write are separate units, and a purge is only safe to duplicate in
        the sense that nothing corrupts — two processes still issue the same
        deletes twice and contend on ``checkpoint_writes``/``checkpoint_blobs``.

        Why a lock at all: ``list_purgeable`` takes no row locks, and that
        is deliberate — a purge stamps ``checkpoint_purged_at`` only AFTER
        the delete succeeds, so a crash in between leaves the run eligible
        for a harmless retry instead of marking blobs purged that still
        exist. Claiming rows up front would trade that safety for exclusion.
        This lease buys the exclusion without touching the ordering.

        Released explicitly, and by Postgres anyway if the process dies —
        so a crashed replica cannot wedge the purge the way a claimed-row
        scheme would.
        """
        async with self._session_factory() as session:
            acquired = bool(
                (
                    await session.execute(
                        text("SELECT pg_try_advisory_lock(:key)"),
                        {"key": CHECKPOINT_PURGE_LOCK_KEY},
                    )
                ).scalar_one()
            )
            try:
                yield acquired
            finally:
                if acquired:
                    with suppress(Exception):
                        await session.execute(
                            text("SELECT pg_advisory_unlock(:key)"),
                            {"key": CHECKPOINT_PURGE_LOCK_KEY},
                        )

    async def list_purgeable(
        self,
        *,
        terminal_statuses: Sequence[str],
        older_than: datetime,
        limit: int = 1000,
        exclude_run_ids: Sequence[UUID] = (),
    ) -> list[WorkflowRun]:
        """Terminal runs whose checkpoint history has not been purged yet,
        ended before ``older_than`` (Story 4.10 AC1).

        ``terminal_statuses`` is a parameter, not a literal in this method,
        because ``shared/`` cannot import ``domain.run_control.TERMINAL_STATUSES``
        (``import-linter`` Contract 2 — the layering runs app → features →
        infra → shared, never the reverse). The caller
        (``CheckpointRetentionWorker``, a feature-layer module) supplies it.

        Read-only — unlike :meth:`claim_stale_running`, nothing here takes
        ``FOR UPDATE SKIP LOCKED``. Claiming rows up front would require
        stamping them BEFORE the delete, and the marker is deliberately
        written after: a crash in between then leaves the run eligible for a
        harmless retry rather than marking blobs purged that still exist.
        Exclusion between replicas comes from :meth:`purge_lease` instead,
        which buys it without touching that ordering — the worker is started
        unconditionally in every process, so nothing else would.

        ``exclude_run_ids`` skips runs the caller already failed to purge in
        this pass. A successful purge stamps ``checkpoint_purged_at`` and so
        drops out of this query; a FAILED one does not, so a batching caller
        without this parameter re-claims the same failing rows every
        iteration. Mirror of ``MemoryArchivalWorker``'s ``failed_ids``.
        """
        async with self.with_tenant(None) as session:
            stmt = (
                select(WorkflowRun)
                .where(
                    WorkflowRun.status.in_(terminal_statuses),
                    _terminal_at() < older_than,
                    WorkflowRun.checkpoint_purged_at.is_(None),
                )
                .order_by(_terminal_at())
                .limit(limit)
            )
            if exclude_run_ids:
                stmt = stmt.where(WorkflowRun.id.not_in(exclude_run_ids))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def count_visible_runs(self) -> int:
        """How many ``workflow_runs`` rows THIS session can see.

        Exists for one caller — the orphan sweep's safety gate. See
        :meth:`list_orphan_checkpoint_threads` for why a count is the thing
        that makes a destructive negative predicate safe to act on.
        """
        async with self.with_tenant(None) as session:
            result = await session.execute(text("SELECT count(*) FROM workflow_runs"))
            return int(result.scalar_one())

    async def list_orphan_checkpoint_threads(
        self, *, limit: int = 1000, exclude_threads: Sequence[str] = ()
    ) -> list[str]:
        """LangGraph ``thread_id``s whose ``workflow_runs`` row no longer
        exists (Story 4.15 AC2).

        :meth:`list_purgeable` is the only other discovery path for checkpoint
        blobs, and it JOINS on ``workflow_runs``. ``workflow_runs`` cascades
        from ``workflows`` (``ondelete="CASCADE"``), so deleting a workflow
        takes its runs with it and leaves their
        ``checkpoints``/``checkpoint_writes``/``checkpoint_blobs`` rows
        unreachable by anything — permanently. That is exactly the unbounded
        growth Story 4.10 AC1 exists to close, reached through a door its
        design does not look at.

        **⚠️ This predicate is NEGATIVE and its caller DELETES. Read this
        before changing it.** Every other purge path in this repo fails safe:
        ``list_purgeable`` reads under the same RLS restriction, where being
        unable to see a row means *skip*. This one inverts the sign — not
        seeing a row means *delete*. The two blind spots that follow are
        therefore the difference between a no-op and data loss, and neither is
        closed by this query alone.

        **Blind spot 1 — Row-Level Security (review 4.15, three layers
        converged).** ``workflow_runs`` carries ``ENABLE`` *and* ``FORCE ROW
        LEVEL SECURITY`` with ``USING (tenant_id IS NULL OR tenant_id =
        current_setting('app.tenant_id', true)::uuid)``; ``checkpoints`` is a
        LangGraph table and carries none. ``with_tenant(None)`` never sets the
        GUC, so the policy collapses to ``tenant_id IS NULL``: the moment any
        run row carries a non-NULL ``tenant_id`` it becomes INVISIBLE here,
        its live thread reads as an orphan, and its checkpoints are deleted
        out from under a running graph. Inert today — every write passes
        ``tenant_id=None`` — but it arms itself with the first tenant-scoped
        write, silently, in a daily background job. Registered as a blocker on
        Story 4.9 AC1 (multi-tenant propagation) beside the site inventory.

        **Blind spot 2 — anything else that could hide rows** (a future
        policy, a replica reading a lagging snapshot, a role change). The
        class is open, so the guard must be too.

        Neither is closed here, because a session cannot prove from the inside
        what RLS is hiding from it: ``SELECT count(*) WHERE tenant_id IS NOT
        NULL`` returns 0 whether or not such rows exist. The caller therefore
        gates on :meth:`count_visible_runs` and on a blast-radius ceiling —
        cause-agnostic checks that turn "the predicate went wrong, for any
        reason" into a refusal and an ERROR line instead of a mass delete.

        **Discovery spans all three checkpoint tables**, not just
        ``checkpoints``. ``adelete_thread`` issues separate DELETEs, so an
        interrupted purge (pod eviction, pool close during the 2s stop window)
        can remove the ``checkpoints`` rows and leave
        ``checkpoint_blobs``/``checkpoint_writes`` behind. Keying discovery on
        ``checkpoints`` alone would make those residues invisible to this
        sweep too — the very unbounded growth it exists to close, one table
        down.

        **Why the join is on ``uuid`` and not on ``text``.** ``thread_id`` is
        free-form text, so ``wr.id::text = c.thread_id`` would put the cast on
        the INDEXED side and defeat the primary key, full-scanning
        ``workflow_runs`` — the largest table in the schema, and the one this
        story exists because it grows. Instead each candidate is shape-checked
        and cast ONCE in a ``MATERIALIZED`` CTE (materialized so Postgres
        cannot push the cast into a context where a non-UUID value would make
        the whole statement raise), and the anti-join runs against the PK. A
        ``thread_id`` that is not a UUID can belong to no run and is reported
        as an orphan by construction.
        """
        async with self.with_tenant(None) as session:
            result = await session.execute(
                text(
                    "WITH candidate AS MATERIALIZED ("
                    "  SELECT thread_id FROM checkpoints"
                    "  UNION SELECT thread_id FROM checkpoint_blobs"
                    "  UNION SELECT thread_id FROM checkpoint_writes"
                    "), typed AS MATERIALIZED ("
                    "  SELECT thread_id,"
                    "         CASE WHEN thread_id ~* "
                    "              '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                    "[0-9a-f]{4}-[0-9a-f]{12}$'"
                    "              THEN thread_id::uuid END AS run_id"
                    "  FROM candidate"
                    ") "
                    "SELECT t.thread_id FROM typed t "
                    "WHERE NOT (t.thread_id = ANY(:excluded)) "
                    "  AND ("
                    "    t.run_id IS NULL"
                    "    OR NOT EXISTS (SELECT 1 FROM workflow_runs wr WHERE wr.id = t.run_id)"
                    "  ) "
                    "ORDER BY t.thread_id "
                    "LIMIT :limit"
                ),
                {"limit": limit, "excluded": list(exclude_threads)},
            )
            return [row[0] for row in result.all()]

    async def mark_checkpoint_purged(self, run_id: UUID) -> int:
        """Stamp ``checkpoint_purged_at = now()`` (Story 4.10 AC1) — called
        only after :meth:`~.retention.CheckpointRetentionWorker`'s
        ``adelete_thread`` call actually succeeds, so a crash between the
        two leaves the run eligible for (harmless) re-purging on the next
        pass rather than falsely marked done.

        Returns the ``rowcount``: 0 means the run disappeared between
        :meth:`list_purgeable` and this write, reachable because
        ``workflow_runs.workflow_id`` carries ``ondelete="CASCADE"``.
        """
        async with self.with_tenant(None) as session:
            result = await session.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == run_id)
                .values(checkpoint_purged_at=func.now())
            )
            # `rowcount` is exposed by SQLAlchemy CursorResult (DML) but the
            # static type is Result[Any]; getattr keeps mypy strict happy —
            # same shape as `update_status_in_session`.
            rowcount = getattr(result, "rowcount", None)
            return int(rowcount) if rowcount is not None else 0

    async def count_stale_paused(
        self, *, older_than: datetime
    ) -> tuple[int, UUID | None, datetime | None]:
        """``(count, oldest_run_id, oldest_last_checkpoint_at)`` for
        ``paused`` runs whose ``last_checkpoint_at`` predates ``older_than``
        (Story 4.10 AC2) — read-only, no action taken.

        ``last_checkpoint_at`` is the best available proxy for "since when
        has this run been paused": there is no dedicated ``paused_at``
        column, and a run's last real checkpoint write happens immediately
        before the driver settles a pending ``pause`` (interruption takes
        effect AT the next superstep boundary, which is exactly where a
        checkpoint just committed) — so a stale ``last_checkpoint_at`` on a
        `paused` run means "idle since roughly when it paused", not merely
        "idle since its last node ran".
        """
        staleness = func.coalesce(WorkflowRun.last_checkpoint_at, WorkflowRun.started_at)
        async with self.with_tenant(None) as session:
            count_stmt = select(func.count()).where(
                WorkflowRun.status == "paused", staleness < older_than
            )
            count = (await session.execute(count_stmt)).scalar_one()
            if count == 0:
                return 0, None, None
            # A second, targeted query for WHICH run is oldest — simpler
            # than pairing `count` with an `ORDER BY`-dependent value via a
            # window function for something read once per sweep tick.
            oldest_stmt = (
                select(WorkflowRun.id, staleness)
                .where(WorkflowRun.status == "paused", staleness < older_than)
                .order_by(staleness)
                .limit(1)
            )
            # `.first()`, not `.one()`: the two statements run in READ
            # COMMITTED with separate snapshots, so `count > 0` does not
            # guarantee this one still matches a row — the last stale run
            # being resumed or cancelled in between would raise
            # `NoResultFound` out of the whole retention pass.
            oldest = (await session.execute(oldest_stmt)).first()
            if oldest is None:
                return 0, None, None
            return int(count), oldest[0], oldest[1]

    async def aggregate_routing_modes(
        self, workflow_id: UUID, *, since: datetime, tenant_id: UUID | None = None
    ) -> tuple[int, int, int]:
        """``(runs_counted, deterministic, llm_escalated)`` across runs of
        ``workflow_id`` started at or after ``since`` (Story 4.3 AC3 T10.1;
        windowed by Story 4.10 AC4).

        ``since`` is REQUIRED, not defaulted to "the beginning of time" —
        Story 4.10 AC4 found this aggregate's cost growing linearly with a
        workflow's ENTIRE historical run count, a number the caller (not the
        operator) controls. Silently defaulting the window here would let a
        future call site reintroduce the same unbounded scan by omission;
        the caller (``router.py``) computes it from
        ``settings.workflow_routing_stats_window_days`` (or a client-chosen
        override, same bounds) so the decision stays visible at the one call
        site that matters.

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

        Story 4.13 AC1 — neither this guard nor its cast rejects a NEGATIVE
        number, which ``sum()`` then propagates straight to the response's
        ``Field(ge=0)`` — a 500 for every OTHER run of the workflow, from
        one corrupted row. See :meth:`_non_negative_metrics_count`, which
        both this method and :meth:`aggregate_token_reduction` now share.
        """

        def _routing_count(key: str) -> Any:
            return self._non_negative_metrics_count("routing", key)

        async with self.with_tenant(tenant_id) as session:
            stmt = select(
                func.count(),
                func.coalesce(func.sum(_routing_count("deterministic")), literal(0)),
                func.coalesce(func.sum(_routing_count("llm_escalated")), literal(0)),
            ).where(WorkflowRun.workflow_id == workflow_id, WorkflowRun.started_at >= since)
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
        (no ``handoffs`` key at all) the same way. Story 4.13 AC1 — and, as
        of this story, of a NEGATIVE value the same way too; see
        :meth:`_non_negative_metrics_count`, shared with the method above so
        the two guards cannot drift the way they had (both left this exact
        gap open, independently, until now).
        """

        def _handoff_count(key: str) -> Any:
            return self._non_negative_metrics_count("handoffs", key)

        async with self.with_tenant(tenant_id) as session:
            stmt = select(
                func.count(),
                func.coalesce(func.sum(_handoff_count("raw_tokens_replaced")), literal(0)),
                func.coalesce(func.sum(_handoff_count("summary_tokens")), literal(0)),
            ).where(WorkflowRun.workflow_id == workflow_id)
            result = await session.execute(stmt)
            row = result.one()
            return int(row[0]), int(row[1]), int(row[2])
