"""Service layer — Agent Registry orchestration (Story 2.1 + 2.2).

The service sits between the FastAPI router and the repositories. It :

1. Validates the requested archetype against the in-process registry.
2. Builds the default ``agent_templates.config`` JSON from the archetype.
3. Composes the row INSERT and the audit-event publish in a SINGLE
   transaction (Story 2.1 P-02 atomicity fix). The repository's
   :meth:`AgentTemplateRepo.create_in_session` accepts an external session
   so the service owns the transaction boundary, then ``event_bus.publish``
   (no commit) writes to ``outbox_events`` in the same transaction. Commit
   happens when the ``with_tenant`` context exits.
4. The mapping ``IntegrityError`` → :class:`ConflictError` lives in the
   repository layer so ``features/`` stay free of ``sqlalchemy`` imports
   (``import-linter`` Contract 3 — Story 2.1 P-01).
5. After the transaction commits, best-effort ``emit_notify`` to wake the
   outbox worker. NOTIFY failure is non-fatal (poll fallback Story 1.4).

Story 2.2 adds :meth:`update_template` (PUT semantics, PATCH-like) and
:meth:`get_template_by_id` (GET detail). The same single-transaction
atomicity contract holds: template UPDATE + prompt INSERT + outbox INSERT
in one transaction.

The audit events ``agent_registry.agent_template.created`` (Story 2.1) and
``agent_registry.agent_template.updated`` (Story 2.2) are published via the event-bus
bypass pattern documented in Epic 1 retrospective 2026-05-08 (to be
migrated to ``AuditEventRepo.record()`` in Story 9.1).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from agentive_backend.features.agent_registry.archetypes import ArchetypeDefinition
from agentive_backend.features.agent_registry.schemas import (
    AgentInstanceDetailResponse,
    AgentToolsResponse,
    ArchetypeDetail,
    ArchetypeSummary,
    AssignedToolView,
    ContractSkeletonView,
    CreateTemplateResponse,
    InstantiateTemplateResponse,
    TemplateDetailResponse,
    UpdateTemplateRequest,
    UpdateTemplateResponse,
)
from agentive_backend.shared.contracts.events import (
    AgentInstanceCreatedEvent,
    AgentTemplateCreatedEvent,
    AgentTemplateToolAssignedEvent,
    AgentTemplateToolUnassignedEvent,
    AgentTemplateUpdatedEvent,
)
from agentive_backend.shared.event_bus import notify_best_effort, publish
from agentive_backend.shared.exceptions import NotFoundError, ValidationError
from agentive_backend.shared.logging import get_logger

if TYPE_CHECKING:
    from agentive_backend.shared.repositories import (
        AgentInstanceRepo,
        AgentTemplateRepo,
        AgentTemplateToolRepo,
        PromptRepo,
        ToolRepo,
        WorkflowRunRepo,
    )

_log = get_logger(__name__)


class AgentRegistryService:
    """Orchestrate archetype lookup + template creation/update + instance lifecycle (Story 2.4)."""

    def __init__(
        self,
        *,
        registry: Mapping[str, ArchetypeDefinition],
        template_repo: AgentTemplateRepo,
        prompt_repo: PromptRepo,
        instance_repo: AgentInstanceRepo,
        workflow_run_repo: WorkflowRunRepo,
        tool_repo: ToolRepo,
        assignment_repo: AgentTemplateToolRepo,
    ) -> None:
        # P-16 (CR 2026-05-10) — invariant: for Story 2.1 P-02 atomicity
        # (with_tenant + same session for template SELECT + instance
        # INSERT + outbox publish) to hold, the 4 repos MUST share the
        # same session_factory. The assertion lives in `_build_service`
        # (router.py), which produces the production wiring; here the
        # constructor accepts heterogeneous repos to keep testability
        # (unit tests mock each repo separately).
        self._registry = registry
        self._template_repo = template_repo
        self._prompt_repo = prompt_repo
        self._instance_repo = instance_repo
        self._workflow_run_repo = workflow_run_repo
        self._tool_repo = tool_repo
        self._assignment_repo = assignment_repo

    # ─── Archetypes ───────────────────────────────────────────────

    def list_archetypes(self) -> list[ArchetypeSummary]:
        """Return the 8 archetypes as lean summaries (no prompt_base / contracts)."""
        return [
            ArchetypeSummary(
                id=a.id,
                display_name=a.display_name,
                icon_name=a.icon_name,
                description=a.description,
                default_role=a.default_role,
            )
            for a in self._registry.values()
        ]

    def get_archetype(self, archetype_id: str) -> ArchetypeDetail:
        """Return one archetype with its full skeleton."""
        archetype = self._registry.get(archetype_id)
        if archetype is None:
            raise NotFoundError(
                detail=f"Unknown archetype '{archetype_id}'",
                context={"archetype_id": archetype_id},
            )
        return ArchetypeDetail(
            id=archetype.id,
            display_name=archetype.display_name,
            icon_name=archetype.icon_name,
            description=archetype.description,
            default_role=archetype.default_role,
            prompt_base=archetype.prompt_base,
            input_contract=ContractSkeletonView(**archetype.input_contract.model_dump()),
            output_contract=ContractSkeletonView(**archetype.output_contract.model_dump()),
        )

    # ─── Templates ────────────────────────────────────────────────

    async def create_template(
        self,
        *,
        name: str,
        archetype_id: str,
        tenant_id: UUID | None = None,
    ) -> CreateTemplateResponse:
        """Persist a new ``agent_templates`` row + publish the audit event atomically.

        Story 2.1 P-02 — the row INSERT and the outbox INSERT share a single
        transaction so a partial-failure mid-flight cannot leave the system
        in a state where the template exists with no audit trace. The bound
        ``correlation_id`` ContextVar (set by ``CorrelationIdMiddleware``)
        is auto-propagated by ``publish`` — no explicit pass.

        Raises
        ------
        ValidationError
            ``archetype_id`` is not one of the 8 universal archetypes.
        ConflictError
            ``(name, version=1, tenant_id)`` already exists. Translated by
            :meth:`AgentTemplateRepo.create_in_session` from the underlying
            ``IntegrityError`` so this module stays free of ``sqlalchemy``
            imports (``import-linter`` Contract 3 — Story 2.1 P-01).
        """
        archetype = self._registry.get(archetype_id)
        if archetype is None:
            valid = ", ".join(sorted(self._registry.keys()))
            raise ValidationError(
                detail=f"Unknown archetype '{archetype_id}'. Valid archetypes: {valid}",
                context={"valid_archetypes": sorted(self._registry.keys())},
            )

        config = archetype.to_template_config()
        event_type = AgentTemplateCreatedEvent.event_type

        # ─── Single transaction: row INSERT + outbox INSERT ───
        # `with_tenant` opens an AsyncSession, binds tenant via SET LOCAL,
        # commits on `__aexit__` if no exception. `create_in_session` and
        # `publish(session=...)` both operate on the same session, so the
        # transaction is atomic.
        # TODO Story 9.1 — migrate to AuditEventRepo.record() — audit-event bypass cleanup (Epic 1 retro 2026-05-08).
        async with self._template_repo.with_tenant(tenant_id) as session:
            template = await self._template_repo.create_in_session(
                session,
                name=name,
                archetype=archetype.id,
                config=config,
                version=1,
                tenant_id=tenant_id,
            )
            event = AgentTemplateCreatedEvent(
                template_id=template.id,
                name=template.name,
                archetype=template.archetype,
                version=template.version,
                actor="system",
                tenant_id=tenant_id,
            )
            event_id = await publish(event_type, event, session=session)
            # commit happens at __aexit__ if no exception is raised.

        # ─── Post-commit: best-effort NOTIFY ───
        # Worker poll fallback (Story 1.4) covers any missed NOTIFY, so a
        # failure here is logged but never re-raised — the row + outbox
        # are already durable.
        await notify_best_effort(event_id, event_type)

        _log.info(
            "agent_template_created",
            template_id=str(template.id),
            name=template.name,
            archetype=template.archetype,
            version=template.version,
        )

        return CreateTemplateResponse(
            template_id=template.id,
            name=template.name,
            archetype=template.archetype,
            version=template.version,
            created_at=template.created_at,
        )

    # ─── Template detail (Story 2.2 AC6) ──────────────────────────

    async def get_template_by_id(
        self,
        template_id: UUID,
        *,
        tenant_id: UUID | None = None,
    ) -> TemplateDetailResponse:
        """Read one template — Story 2.2 AC6.

        Raises:
            NotFoundError: ``template_id`` does not exist (or is filtered
                out by RLS for the bound tenant).
        """
        template = await self._template_repo.get_by_id(template_id, tenant_id=tenant_id)
        if template is None:
            raise NotFoundError(
                detail=f"Agent template '{template_id}' not found",
                context={"template_id": str(template_id)},
            )
        return TemplateDetailResponse(
            template_id=template.id,
            name=template.name,
            archetype=template.archetype,
            version=template.version,
            config=template.config,
            created_at=template.created_at,
        )

    # ─── Template update (Story 2.2 AC1) ──────────────────────────

    async def update_template(
        self,
        template_id: UUID,
        payload: UpdateTemplateRequest,
        *,
        tenant_id: UUID | None = None,
    ) -> UpdateTemplateResponse:
        """Apply a PATCH-like update to ``agent_templates.config`` — Story 2.2.

        Atomicity (Story 2.1 P-02 pattern) — fetch, update, prompt insert
        and outbox publish all share a single transaction. ``with_tenant``
        commits at ``__aexit__``; any exception triggers a rollback so a
        partial state is impossible.

        ``system_prompt`` is the only field that triggers a version bump:
        a row is added to ``prompts`` with ``version = old_version + 1``
        and the template row's ``version`` column is incremented. Other
        fields are merged into ``config`` without bumping version.

        Raises:
            NotFoundError: ``template_id`` does not exist.
        """
        event_type = AgentTemplateUpdatedEvent.event_type

        async with self._template_repo.with_tenant(tenant_id) as session:
            existing = await self._template_repo.get_by_id_in_session(session, template_id)
            if existing is None:
                raise NotFoundError(
                    detail=f"Agent template '{template_id}' not found",
                    context={"template_id": str(template_id)},
                )

            old_version = existing.version
            merged_config = dict(existing.config or {})

            # P-01 fix — bump version + insert prompts row ONLY when the
            # `system_prompt` actually changes (vs identical to current).
            # Without this check, the default UX path (Save without touching
            # the prompt) duplicates byte-identical `prompts` rows and
            # inflates `agent_templates.version` on every click.
            current_system_prompt = merged_config.get("system_prompt")
            system_prompt_changed = (
                payload.system_prompt is not None and payload.system_prompt != current_system_prompt
            )

            if payload.system_prompt is not None:
                merged_config["system_prompt"] = payload.system_prompt
            if payload.input_contract is not None:
                merged_config["input_contract"] = payload.input_contract.model_dump()
            if payload.output_contract is not None:
                merged_config["output_contract"] = payload.output_contract.model_dump()
            if payload.llm_model is not None:
                merged_config["llm_model"] = payload.llm_model
            if payload.llm_params is not None:
                merged_config["llm_params"] = payload.llm_params.model_dump()
            if payload.provider_chain is not None:
                merged_config["provider_chain"] = list(payload.provider_chain)
            if payload.error_policy is not None:
                merged_config["error_policy"] = payload.error_policy.model_dump()

            bump_version = system_prompt_changed
            if bump_version:
                new_version = old_version + 1
                updated = await self._template_repo.update_in_session(
                    session,
                    template=existing,
                    config=merged_config,
                    new_version=new_version,
                )
                # `payload.system_prompt` is non-None — guarded by `system_prompt_changed`
                # which short-circuits if `payload.system_prompt is None`.
                await self._prompt_repo.create_in_session(
                    session,
                    agent_template_id=template_id,
                    version=new_version,
                    content=payload.system_prompt,  # type: ignore[arg-type]  # guarded
                    tenant_id=tenant_id,
                )
            else:
                new_version = old_version
                updated = await self._template_repo.update_config_in_session(
                    session,
                    template=existing,
                    config=merged_config,
                )

            event = AgentTemplateUpdatedEvent(
                template_id=updated.id,
                name=updated.name,
                archetype=updated.archetype,
                old_version=old_version,
                new_version=new_version,
                actor="system",
                tenant_id=tenant_id,
            )
            # TODO Story 9.1 — migrate to AuditEventRepo.record() — audit-event bypass cleanup (Epic 1 retro 2026-05-08).
            event_id = await publish(event_type, event, session=session)

            # P-05 fix — `updated_at` captured INSIDE the with_tenant block,
            # right before the commit (which runs at `__aexit__`). Without
            # this, the returned value included the post-commit notify
            # latency (up to several hundred ms under load) — inconsistent
            # for a timestamp that claims to reflect the DB write.
            updated_at = datetime.now(tz=UTC)
            # commit happens at __aexit__ if no exception is raised.

        # ─── Post-commit: best-effort NOTIFY ───
        await notify_best_effort(event_id, event_type)

        _log.info(
            "agent_template_updated",
            template_id=str(updated.id),
            name=updated.name,
            archetype=updated.archetype,
            old_version=old_version,
            new_version=new_version,
            bump_version=bump_version,
        )

        # `updated_at` Sprint 1 — the table has no dedicated column. The
        # returned value is captured right before the commit (P-05); if
        # needed, historical state can be recovered via
        # ``MAX(prompts.created_at) WHERE agent_template_id = ?`` for rows
        # that triggered a version bump.
        return UpdateTemplateResponse(
            template_id=updated.id,
            name=updated.name,
            archetype=updated.archetype,
            version=updated.version,
            config=updated.config,
            updated_at=updated_at,
        )

    # ─── Agent instances (Story 2.4 — distinction template vs instance) ─

    async def instantiate_from_template(
        self,
        *,
        template_id: UUID,
        workflow_run_id: UUID | None = None,
        tenant_id: UUID | None = None,
    ) -> InstantiateTemplateResponse:
        """Create a new ``agent_instances`` row with a frozen snapshot of the
        template config — Story 2.4 AC1 + AC2.

        Atomicity (Story 2.1 P-02 pattern) — fetch template + (optional
        validate workflow_run) + INSERT instance + outbox publish all share
        a single transaction. ``with_tenant`` commits at ``__aexit__`` ;
        any exception triggers a rollback so a partial state is impossible.

        The snapshot shape is canonical Sprint 1 (cf Story 2.4 §"Décisions
        intégrées" #11) :

            {
                "template_id": "<uuid str>",
                "template_version": <int>,
                "name": "<str>",
                "archetype": "<str>",
                "config": <full template.config dict copy>,
            }

        Modifying the template AFTER this call does NOT propagate to the
        instance — the snapshot is immutable by design (FR12).

        Raises:
            NotFoundError: ``template_id`` does not exist, OR
                ``workflow_run_id`` was provided and does not exist.
        """
        event_type = AgentInstanceCreatedEvent.event_type

        async with self._instance_repo.with_tenant(tenant_id) as session:
            # 1. SELECT template — must exist (404 sinon).
            template = await self._template_repo.get_by_id_in_session(session, template_id)
            if template is None:
                raise NotFoundError(
                    detail=f"Agent template '{template_id}' not found",
                    context={"template_id": str(template_id)},
                )

            # 2. Validate workflow_run FK if provided (404 sinon — strict).
            if workflow_run_id is not None:
                run = await self._workflow_run_repo.get_by_id_in_session(session, workflow_run_id)
                if run is None:
                    raise NotFoundError(
                        detail=f"Workflow run '{workflow_run_id}' not found",
                        context={"workflow_run_id": str(workflow_run_id)},
                    )

            # 3. Build the immutable snapshot. P-15 (CR 2026-05-10) — deep
            #    copy via `copy.deepcopy` (was shallow `dict(...)`) because
            #    `template.config` holds nested dicts (`llm_params`,
            #    `error_policy`, `input_contract`, `provider_chain` list)
            #    that stayed shared by reference under shallow copy. Sprint 1
            #    SQLAlchemy JSONB returns a fresh dict on every fetch so it
            #    is fine in practice; deep copy = defense-in-depth against
            #    future paths that would reuse the in-session object.
            snapshot: dict[str, Any] = {
                "template_id": str(template.id),
                "template_version": template.version,
                "name": template.name,
                "archetype": template.archetype,
                "config": copy.deepcopy(template.config or {}),
            }

            # 4. INSERT instance.
            instance = await self._instance_repo.create_in_session(
                session,
                template_id=template.id,
                template_version=template.version,
                snapshot=snapshot,
                workflow_run_id=workflow_run_id,
                tenant_id=tenant_id,
            )

            # 5. Publish audit event in the SAME transaction (P-02 atomicity).
            event = AgentInstanceCreatedEvent(
                instance_id=instance.id,
                template_id=template.id,
                template_version=template.version,
                workflow_run_id=workflow_run_id,
                actor="system",  # D1 defer Story 9.1 — auth context resolution.
                tenant_id=tenant_id,
            )
            # TODO Story 9.1 — migrate to AuditEventRepo.record() — audit-event bypass cleanup (Epic 1 retro 2026-05-08).
            event_id = await publish(event_type, event, session=session)
            # commit happens at __aexit__ if no exception.

        # ─── Post-commit: best-effort NOTIFY (Story 1.4 outbox pattern).
        await notify_best_effort(event_id, event_type)

        _log.info(
            "agent_instance_created",
            instance_id=str(instance.id),
            template_id=str(template.id),
            template_version=template.version,
            workflow_run_id=str(workflow_run_id) if workflow_run_id else None,
            # P-10 (CR 2026-05-10) — log shape aligned with event payload
            # (AC1 spec). `actor`/`tenant_id` reflect Sprint 1 hardcoded
            # values ; will surface real values once Story 9.1 wires the
            # auth context resolution and Story 12 enables multi-tenant.
            actor="system",
            tenant_id=None,
        )

        return InstantiateTemplateResponse(
            instance_id=instance.id,
            template_id=instance.template_id,
            template_version=instance.template_version,
            workflow_run_id=instance.workflow_run_id,
            # P-17 (CR 2026-05-10) — output symmetric copy: consistent with
            # the defensive copy on input (snapshot built via deepcopy).
            # Avoids any aliasing of the ORM object `instance.snapshot`
            # post-session-close (paranoia: response middleware could
            # technically mutate the object).
            snapshot=dict(instance.snapshot),
            created_at=instance.created_at,
        )

    async def get_instance_by_id(
        self,
        instance_id: UUID,
        *,
        tenant_id: UUID | None = None,
    ) -> AgentInstanceDetailResponse:
        """Read one instance — Story 2.4 AC4.

        Raises:
            NotFoundError: ``instance_id`` does not exist (or is filtered
                out by RLS for the bound tenant).
        """
        instance = await self._instance_repo.get_by_id(instance_id, tenant_id=tenant_id)
        if instance is None:
            raise NotFoundError(
                detail=f"Agent instance '{instance_id}' not found",
                context={"instance_id": str(instance_id)},
            )
        return AgentInstanceDetailResponse(
            instance_id=instance.id,
            template_id=instance.template_id,
            template_version=instance.template_version,
            workflow_run_id=instance.workflow_run_id,
            snapshot=dict(instance.snapshot),  # P-17 — output defensive copy.
            created_at=instance.created_at,
        )

    async def list_instances_by_workflow_run(
        self,
        workflow_run_id: UUID,
        *,
        tenant_id: UUID | None = None,
    ) -> list[AgentInstanceDetailResponse]:
        """List instances rattachées à un workflow_run — Story 2.4 AC3.

        Strict 404 if the workflow_run itself does not exist (not an empty
        list — cf Story 2.4 §"Décisions intégrées" #7). 200 + ``[]`` when
        the run exists but has no instances.
        """
        async with self._instance_repo.with_tenant(tenant_id) as session:
            run = await self._workflow_run_repo.get_by_id_in_session(session, workflow_run_id)
            if run is None:
                raise NotFoundError(
                    detail=f"Workflow run '{workflow_run_id}' not found",
                    context={"workflow_run_id": str(workflow_run_id)},
                )
            instances = await self._instance_repo.list_by_workflow_run_in_session(
                session, workflow_run_id
            )

        return [
            AgentInstanceDetailResponse(
                instance_id=instance.id,
                template_id=instance.template_id,
                template_version=instance.template_version,
                workflow_run_id=instance.workflow_run_id,
                snapshot=dict(instance.snapshot),  # P-17 — output defensive copy.
                created_at=instance.created_at,
            )
            for instance in instances
        ]

    # ─── Tool assignment (Story 2.5 — junction agent_template_tools) ──

    async def replace_template_tools(
        self,
        template_id: UUID,
        tool_ids: list[UUID],
        *,
        tenant_id: UUID | None = None,
    ) -> AgentToolsResponse:
        """REPLACE the list of tools assigned to a template — Story 2.5 AC2.

        Atomic single-transaction (P-02 Story 2.1) :
        1. Validate template exists.
        2. Validate ALL tool_ids exist (404 if any single one missing — no
           partial success, cf décision #8).
        3. Compute (added, removed) diffs vs current assignments.
        4. DELETE removed + INSERT added in junction.
        5. Publish 1 event per added (tool_assigned) + 1 event per removed
           (tool_unassigned) — all in the same transaction.
        6. Return the current assignments list.

        Raises
        ------
        NotFoundError
            ``template_id`` does not exist, OR any ``tool_id`` does not exist.
        """
        async with self._template_repo.with_tenant(tenant_id) as session:
            # 1. Validate template exists.
            template = await self._template_repo.get_by_id_in_session(session, template_id)
            if template is None:
                raise NotFoundError(
                    detail=f"Agent template '{template_id}' not found",
                    context={"template_id": str(template_id)},
                )

            # 2. Validate ALL tools exist (decision #8 — strict 404, no partial).
            tools_by_id: dict[UUID, Any] = {}
            for tid in tool_ids:
                tool = await self._tool_repo.get_by_id_in_session(session, tid)
                if tool is None:
                    raise NotFoundError(
                        detail=f"Tool '{tid}' not found",
                        context={"tool_id": str(tid)},
                    )
                tools_by_id[tool.id] = tool

            # 3 + 4. Compute diff + DML inside the same session.
            added, removed = await self._assignment_repo.replace_in_session(
                session,
                template_id=template_id,
                new_tool_ids=tool_ids,
                actor="system",
                tenant_id=tenant_id,
            )

            # 5. Publish events (1 per added, 1 per removed). Track each
            # event_id alongside its real event_type so the post-commit
            # NOTIFY (P-05) can fire with the correct channel — listeners
            # filtering by event_type would miss a fabricated label.
            events_emitted: list[tuple[UUID, str]] = []
            # TODO Story 9.1 — migrate to AuditEventRepo.record() — audit-event bypass cleanup (Epic 1 retro 2026-05-08).
            for tid in added:
                tool = tools_by_id[tid]
                event = AgentTemplateToolAssignedEvent(
                    template_id=template_id,
                    tool_id=tid,
                    tool_name=tool.name,
                    actor="system",
                    tenant_id=tenant_id,
                )
                eid = await publish(
                    AgentTemplateToolAssignedEvent.event_type, event, session=session
                )
                events_emitted.append((eid, AgentTemplateToolAssignedEvent.event_type))
            # TODO Story 9.1 — migrate to AuditEventRepo.record() — audit-event bypass cleanup (Epic 1 retro 2026-05-08).
            for tid in removed:
                event_unassign = AgentTemplateToolUnassignedEvent(
                    template_id=template_id,
                    tool_id=tid,
                    actor="system",
                    tenant_id=tenant_id,
                )
                eid = await publish(
                    AgentTemplateToolUnassignedEvent.event_type,
                    event_unassign,
                    session=session,
                )
                events_emitted.append((eid, AgentTemplateToolUnassignedEvent.event_type))

            # 6. Re-fetch current assignments to return (after the DML).
            assigned_tools = await self._assignment_repo.list_by_template_in_session(
                session, template_id
            )
            # commit at __aexit__.

        # Post-commit best-effort NOTIFY (1 per event with its real event_type).
        for eid, event_type in events_emitted:
            await notify_best_effort(eid, event_type)

        _log.info(
            "agent_template_tools_replaced",
            template_id=str(template_id),
            added_count=len(added),
            removed_count=len(removed),
            actor="system",
            tenant_id=None,
        )

        return AgentToolsResponse(
            template_id=template_id,
            assigned_tools=[
                AssignedToolView(
                    tool_id=tool.id,
                    name=tool.name,
                    description=tool.description,
                    server_id=tool.server_id,
                    input_schema=dict(tool.input_schema or {}),
                    output_schema=tool.output_schema,
                    assigned_at=assigned_at,
                )
                for tool, assigned_at in assigned_tools
            ],
        )

    async def list_template_tools(
        self,
        template_id: UUID,
        *,
        tenant_id: UUID | None = None,
    ) -> AgentToolsResponse:
        """List the tools currently assigned to a template — Story 2.5 AC4.

        Raises
        ------
        NotFoundError
            ``template_id`` does not exist.
        """
        async with self._template_repo.with_tenant(tenant_id) as session:
            template = await self._template_repo.get_by_id_in_session(session, template_id)
            if template is None:
                raise NotFoundError(
                    detail=f"Agent template '{template_id}' not found",
                    context={"template_id": str(template_id)},
                )
            assigned_tools = await self._assignment_repo.list_by_template_in_session(
                session, template_id
            )

        return AgentToolsResponse(
            template_id=template_id,
            assigned_tools=[
                AssignedToolView(
                    tool_id=tool.id,
                    name=tool.name,
                    description=tool.description,
                    server_id=tool.server_id,
                    input_schema=dict(tool.input_schema or {}),
                    output_schema=tool.output_schema,
                    assigned_at=assigned_at,
                )
                for tool, assigned_at in assigned_tools
            ],
        )

    async def unassign_tool(
        self,
        template_id: UUID,
        tool_id: UUID,
        *,
        tenant_id: UUID | None = None,
    ) -> None:
        """Remove a single tool from a template's assignments — Story 2.5 AC3.

        Strict idempotency : if the assignment does NOT exist, raises 404
        (re-DELETE returns 404, not 204 — décision #10).

        Raises
        ------
        NotFoundError
            The assignment ``(template_id, tool_id)`` does not exist.
        """
        async with self._assignment_repo.with_tenant(tenant_id) as session:
            deleted = await self._assignment_repo.unassign_in_session(
                session, template_id=template_id, tool_id=tool_id
            )
            if not deleted:
                raise NotFoundError(
                    detail=(f"Assignment (template={template_id}, tool={tool_id}) not found"),
                    context={
                        "template_id": str(template_id),
                        "tool_id": str(tool_id),
                    },
                )

            event = AgentTemplateToolUnassignedEvent(
                template_id=template_id,
                tool_id=tool_id,
                actor="system",
                tenant_id=tenant_id,
            )
            # TODO Story 9.1 — migrate to AuditEventRepo.record() — audit-event bypass cleanup (Epic 1 retro 2026-05-08).
            event_id = await publish(
                AgentTemplateToolUnassignedEvent.event_type, event, session=session
            )
            # commit at __aexit__.

        await notify_best_effort(event_id, AgentTemplateToolUnassignedEvent.event_type)

        _log.info(
            "agent_template_tool_unassigned",
            template_id=str(template_id),
            tool_id=str(tool_id),
            actor="system",
            tenant_id=None,
        )


__all__ = ["AgentRegistryService"]
