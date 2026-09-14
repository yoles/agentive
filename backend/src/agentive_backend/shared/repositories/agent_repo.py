"""Public API surface for :class:`AgentTemplate` and :class:`AgentInstance`.

ALL DB access must go through these classes. Features import ``ConflictError``
from ``shared.exceptions`` — they MUST NOT import ``sqlalchemy.exc`` directly
(``import-linter`` Contract 3 forbids ``sqlalchemy`` imports from
``agentive_backend.features``). The repo catches ``IntegrityError`` and
re-raises a domain :class:`ConflictError` so callers stay sqlalchemy-free.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import asc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentive_backend.infra.db.models import AgentInstance, AgentTemplate
from agentive_backend.shared.exceptions import ConflictError
from agentive_backend.shared.repositories.base import BaseRepo

if TYPE_CHECKING:
    from collections.abc import Collection


class AgentTemplateRepo(BaseRepo):
    """Public API surface for AgentTemplate. ALL DB access must go through this class."""

    async def get_by_id(
        self, template_id: UUID, *, tenant_id: UUID | None = None
    ) -> AgentTemplate | None:
        async with self.with_tenant(tenant_id) as session:
            return await session.get(AgentTemplate, template_id)

    async def require_by_id(
        self, template_id: UUID, *, tenant_id: UUID | None = None
    ) -> AgentTemplate:
        """Fetch by id or raise :class:`NotFoundError` (lookup-or-404, audit A-07)."""
        return self._require_found(
            await self.get_by_id(template_id, tenant_id=tenant_id),
            label="Agent template",
            entity_id=template_id,
            context_key="template_id",
        )

    async def list_by_ids(
        self, template_ids: Collection[UUID], *, tenant_id: UUID | None = None
    ) -> dict[UUID, AgentTemplate]:
        """Batch-resolve templates by id — self-managed transaction (Story 4.8 AC3).

        Wrapper over :meth:`list_by_ids_in_session` for callers with no
        transaction to compose with (mirror the ``create`` /
        ``create_in_session`` pairing above). Never locks: its caller is
        ``workflow_engine.service._load_templates``, a hot read on the run
        and dry-run paths, not a pre-write validation.
        """
        async with self.with_tenant(tenant_id) as session:
            return await self.list_by_ids_in_session(session, template_ids)

    async def list_by_ids_in_session(
        self,
        session: AsyncSession,
        template_ids: Collection[UUID],
        *,
        lock: bool = False,
    ) -> dict[UUID, AgentTemplate]:
        """Batch-resolve templates by id inside the caller's transaction.

        Story 4.8 AC3 — replaces the N sequential ``get_by_id`` calls that
        ``WorkflowService.create_workflow`` and
        ``workflow_engine.service._load_templates`` both used to make, one
        per DAG node. A 100-node workflow (the ``_MAX_NODES`` cap of Story
        4.1) paid 100 round trips; it now pays one.

        Returns a ``{id: template}`` map rather than a list: every caller
        needs random access by id, and rebuilding the map at each call site
        is how the two of them would drift. Ids absent from the result are
        ids that do not exist — the CALLER decides what that means, because
        the two callers disagree on purpose (a 422 at creation, where the id
        comes from the client's body; a 500 at execution, where it comes
        from a DAG the server itself validated).

        ``sorted(set(...))`` deduplicates — several nodes may reference the
        same template, and an ``IN`` list repeating an id is pure waste — and
        makes the statement itself deterministic, which is worth having for
        readable logs and stable query-plan caching.

        What it does NOT do is guarantee a lock-acquisition order. An earlier
        version of this docstring claimed it kept the query "deadlock-proof
        the day someone needs ``FOR UPDATE``"; that was wrong and dangerously
        so, because the next reader would have added ``FOR UPDATE`` on the
        strength of it. Postgres acquires row locks in the order the chosen
        PLAN emits rows, not in the order of the Python-side ``IN`` list. The
        ``ORDER BY`` below is closer to load-bearing (``LockRows`` sits above
        ``Sort``), but that too is a plan-shape assumption, not a promise.
        Harmless today because ``FOR SHARE`` never conflicts with itself — if
        you ever need ``FOR UPDATE`` here, establish the ordering properly
        instead of trusting this sort.

        Story 4.8 AC2 — ``lock=True`` emits ``FOR SHARE``, which blocks
        concurrent ``UPDATE``/``DELETE`` on these rows until the caller
        commits. That closes the window in which ``PUT /agents/templates/
        {id}`` could rewrite a ``config`` between the moment
        ``create_workflow`` validated against it and the moment it committed
        the workflow referencing it — no foreign key can do the job, since
        the ids live inside a JSONB document. This is the remedy the P-02
        note on ``WorkflowRunRepo.get_by_id_in_session`` already named (D42)
        and deferred "to Story 4.x".

        ``lock=False`` by default so a plain reader never takes a lock it
        did not ask for.
        """
        unique_ids = sorted(set(template_ids))
        if not unique_ids:
            # `IN ()` is a SQL syntax error in Postgres; SQLAlchemy renders a
            # provably-false expression instead, so the query would be valid
            # but pointless. Skip the round trip entirely — a DAG with no
            # nodes is rejected upstream, but `_load_templates` can legitimately
            # be handed an empty stored DAG.
            return {}
        stmt = (
            select(AgentTemplate).where(AgentTemplate.id.in_(unique_ids)).order_by(AgentTemplate.id)
        )
        if lock:
            stmt = stmt.with_for_update(read=True)
        result = await session.execute(stmt)
        return {row.id: row for row in result.scalars().all()}

    async def get_by_name_version(
        self,
        name: str,
        version: int,
        *,
        tenant_id: UUID | None = None,
    ) -> AgentTemplate | None:
        async with self.with_tenant(tenant_id) as session:
            stmt = select(AgentTemplate).where(
                AgentTemplate.name == name,
                AgentTemplate.version == version,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_latest_by_name(
        self,
        name: str,
        *,
        tenant_id: UUID | None = None,
    ) -> AgentTemplate | None:
        """La row de plus haute ``version`` portant ce ``name``, ou ``None``.

        Distincte de :meth:`get_by_name_version`, et pas par confort : la
        contrainte d'unicité porte sur ``(name, version, tenant_id)`` et
        ``update_in_session`` INCRÉMENTE ``version`` sur la row existante dès
        qu'un ``system_prompt`` change. Un provisioning idempotent qui
        chercherait ``(name, version=1)`` ne retrouverait donc plus un
        template déjà configuré, et en créerait un SECOND — le doublon
        silencieux que la Story 5.1 T2.3 doit rendre impossible.
        """
        async with self.with_tenant(tenant_id) as session:
            stmt = (
                select(AgentTemplate)
                .where(AgentTemplate.name == name)
                .order_by(AgentTemplate.version.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def create(
        self,
        *,
        name: str,
        archetype: str,
        config: dict[str, Any],
        version: int = 1,
        tenant_id: UUID | None = None,
    ) -> AgentTemplate:
        """Convenience wrapper — self-managed transaction.

        Use :meth:`create_in_session` from inside an existing transaction
        when you need to compose the INSERT with another write (e.g.
        publishing an outbox event atomically).

        Raises:
            ConflictError: If ``(name, version, tenant_id)`` already exists.
        """
        async with self.with_tenant(tenant_id) as session:
            return await self.create_in_session(
                session,
                name=name,
                archetype=archetype,
                config=config,
                version=version,
                tenant_id=tenant_id,
            )

    async def create_in_session(
        self,
        session: AsyncSession,
        *,
        name: str,
        archetype: str,
        config: dict[str, Any],
        version: int = 1,
        tenant_id: UUID | None = None,
    ) -> AgentTemplate:
        """INSERT inside the caller's transaction — caller owns commit.

        Story 2.1 P-02 — used by ``AgentRegistryService.create_template`` to
        publish the audit event in the same transaction as the row INSERT
        (atomicity with the outbox pattern, Story 1.4).

        Raises:
            ConflictError: If ``(name, version, tenant_id)`` already exists.
                The repo translates :class:`IntegrityError` into a domain
                error so feature code remains free of ``sqlalchemy`` imports
                (``import-linter`` Contract 3).
        """
        template = AgentTemplate(
            name=name,
            archetype=archetype,
            version=version,
            config=config,
            tenant_id=tenant_id,
        )
        session.add(template)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise ConflictError(
                detail=f"Template '{name}' (version {version}) already exists",
                context={"name": name, "version": version},
            ) from exc
        await session.refresh(template)
        return template

    async def get_by_id_in_session(
        self,
        session: AsyncSession,
        template_id: UUID,
    ) -> AgentTemplate | None:
        """SELECT by id inside the caller's transaction (Story 2.2).

        Same atomicity rationale as :meth:`create_in_session` — the service
        layer composes get + update + outbox publish in a single transaction.
        """
        return await session.get(AgentTemplate, template_id)

    async def require_by_id_in_session(
        self, session: AsyncSession, template_id: UUID
    ) -> AgentTemplate:
        """In-session fetch by id or raise :class:`NotFoundError` (audit A-07)."""
        return self._require_found(
            await self.get_by_id_in_session(session, template_id),
            label="Agent template",
            entity_id=template_id,
            context_key="template_id",
        )

    async def update_in_session(
        self,
        session: AsyncSession,
        *,
        template: AgentTemplate,
        config: dict[str, Any],
        new_version: int,
    ) -> AgentTemplate:
        """UPDATE config + version inside the caller's transaction (Story 2.2).

        ``template`` is the row already loaded via :meth:`get_by_id_in_session`
        in the same session — we mutate its attributes and let the unit-of-work
        flush them.

        Sprint 1 schema has no ``updated_at`` column ; for a versioning
        update (this method), the modification time is approximately
        ``prompts.created_at`` of the matching row inserted right after.
        For non-versioning updates (see :meth:`update_config_in_session`),
        no `prompts` row is created, so the modification time is captured
        only via the audit event ``agent_registry.agent_template.updated`` row in the
        outbox (P-17 docstring correction Story 2.2 code-review 2026-05-09).

        Raises:
            ConflictError: If the new ``(name, version, tenant_id)`` already
                exists (extremely unlikely Sprint 1 — single-tenant + no
                rename, but defensive against future race conditions).
        """
        template.config = config
        template.version = new_version
        try:
            await session.flush()
        except IntegrityError as exc:
            raise ConflictError(
                detail=f"Template '{template.name}' (version {new_version}) already exists",
                context={"name": template.name, "version": new_version},
            ) from exc
        await session.refresh(template)
        return template

    async def update_config_in_session(
        self,
        session: AsyncSession,
        *,
        template: AgentTemplate,
        config: dict[str, Any],
    ) -> AgentTemplate:
        """UPDATE config only (no version bump, no prompt insert) — Story 2.2.

        Used when the PUT payload changes only ancillary fields (e.g.
        ``llm_params``) and does not include ``system_prompt``. Versioning
        is anchored to prompt edits, so non-prompt edits are silent (no bump).
        """
        template.config = config
        await session.flush()
        await session.refresh(template)
        return template


class AgentInstanceRepo(BaseRepo):
    """Public API surface for AgentInstance. ALL DB access must go through this class."""

    async def get_by_id(
        self, instance_id: UUID, *, tenant_id: UUID | None = None
    ) -> AgentInstance | None:
        async with self.with_tenant(tenant_id) as session:
            return await session.get(AgentInstance, instance_id)

    async def require_by_id(
        self, instance_id: UUID, *, tenant_id: UUID | None = None
    ) -> AgentInstance:
        """Fetch by id or raise :class:`NotFoundError` (lookup-or-404, audit A-07)."""
        return self._require_found(
            await self.get_by_id(instance_id, tenant_id=tenant_id),
            label="Agent instance",
            entity_id=instance_id,
            context_key="instance_id",
        )

    async def create(
        self,
        *,
        template_id: UUID,
        template_version: int,
        snapshot: dict[str, Any],
        workflow_run_id: UUID | None = None,
        tenant_id: UUID | None = None,
    ) -> AgentInstance:
        """Self-managed transaction wrapper.

        Use :meth:`create_in_session` from inside an existing transaction
        when you need to compose the INSERT with another write (Story 2.4
        — instantiate_from_template publishes the audit event in the same
        transaction).
        """
        async with self.with_tenant(tenant_id) as session:
            return await self.create_in_session(
                session,
                template_id=template_id,
                template_version=template_version,
                snapshot=snapshot,
                workflow_run_id=workflow_run_id,
                tenant_id=tenant_id,
            )

    async def create_in_session(
        self,
        session: AsyncSession,
        *,
        template_id: UUID,
        template_version: int,
        snapshot: dict[str, Any],
        workflow_run_id: UUID | None = None,
        tenant_id: UUID | None = None,
    ) -> AgentInstance:
        """INSERT inside the caller's transaction — caller owns commit (Story 2.4).

        Same atomicity rationale as
        :meth:`AgentTemplateRepo.create_in_session` — the service layer
        composes template SELECT + instance INSERT + outbox publish in a
        single transaction.

        No ``IntegrityError`` translation here : ``agent_instances`` has no
        UNIQUE constraint (``id`` is server-generated UUID), so collisions
        are not expected. If a future schema change adds one, this method
        should mirror :meth:`AgentTemplateRepo.create_in_session` and
        translate via :class:`ConflictError`.
        """
        instance = AgentInstance(
            template_id=template_id,
            template_version=template_version,
            workflow_run_id=workflow_run_id,
            snapshot=snapshot,
            tenant_id=tenant_id,
        )
        session.add(instance)
        await session.flush()
        await session.refresh(instance)
        return instance

    async def get_by_id_in_session(
        self,
        session: AsyncSession,
        instance_id: UUID,
    ) -> AgentInstance | None:
        """SELECT by id inside the caller's transaction (Story 2.4).

        Symmetric with :meth:`AgentTemplateRepo.get_by_id_in_session`.
        Currently unused by Story 2.4 (the service uses the standalone
        :meth:`get_by_id` for the GET endpoint), but kept for consistency
        and future composability (e.g. Story 2.7 Playground may need it).
        """
        return await session.get(AgentInstance, instance_id)

    async def list_by_workflow_run_in_session(
        self,
        session: AsyncSession,
        workflow_run_id: UUID,
    ) -> list[AgentInstance]:
        """List all instances rattached to a given workflow_run (Story 2.4).

        Order ``created_at ASC`` for deterministic test assertions and a
        natural chronological UX (the consumer Story 8.x trace explorer
        renders runs left-to-right by start time).
        """
        # P-19 (CR 2026-05-10) — secondary sort on `id` for determinism.
        # Postgres `now()` returns the timestamp of the transaction START;
        # 2 INSERTs in the SAME transaction (Story 4.x once workflow_engine
        # batches instantiations) will have IDENTICAL `created_at` → order
        # is undefined without a secondary key. Random UUID v4 is enough to
        # pin a stable order for tests.
        stmt = (
            select(AgentInstance)
            .where(AgentInstance.workflow_run_id == workflow_run_id)
            .order_by(asc(AgentInstance.created_at), asc(AgentInstance.id))
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())
