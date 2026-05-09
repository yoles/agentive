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

The audit events ``m2.agent_template.created`` (Story 2.1) and
``m2.agent_template.updated`` (Story 2.2) are published via the event-bus
bypass pattern documented in Epic 1 retrospective 2026-05-08 (to be
migrated to ``AuditEventRepo.record()`` in Story 9.1).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from agentive_backend.features.m2_agent_registry.archetypes import ArchetypeDefinition
from agentive_backend.features.m2_agent_registry.schemas import (
    ArchetypeDetail,
    ArchetypeSummary,
    ContractSkeletonView,
    CreateTemplateResponse,
    TemplateDetailResponse,
    UpdateTemplateRequest,
    UpdateTemplateResponse,
)
from agentive_backend.shared.contracts.events import (
    AgentTemplateCreatedEvent,
    AgentTemplateUpdatedEvent,
)
from agentive_backend.shared.event_bus import emit_notify, publish
from agentive_backend.shared.exceptions import NotFoundError, ValidationError
from agentive_backend.shared.logging import get_logger

if TYPE_CHECKING:
    from agentive_backend.shared.repositories import AgentTemplateRepo, PromptRepo

_log = get_logger(__name__)


class AgentRegistryService:
    """Orchestrate archetype lookup + template creation/update."""

    def __init__(
        self,
        *,
        registry: Mapping[str, ArchetypeDefinition],
        template_repo: AgentTemplateRepo,
        prompt_repo: PromptRepo,
    ) -> None:
        self._registry = registry
        self._template_repo = template_repo
        self._prompt_repo = prompt_repo

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
        try:
            await emit_notify(event_id, event_type)
        except Exception:
            _log.warning(
                "event_bus_notify_failed_will_be_polled",
                event_id=str(event_id),
                event_type=event_type,
            )

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

            bump_version = payload.system_prompt is not None
            if bump_version:
                new_version = old_version + 1
                updated = await self._template_repo.update_in_session(
                    session,
                    template=existing,
                    config=merged_config,
                    new_version=new_version,
                )
                # `payload.system_prompt` is non-None — guarded above. Asserting
                # for the type-checker, not the runtime (no logical change).
                assert payload.system_prompt is not None
                await self._prompt_repo.create_in_session(
                    session,
                    agent_template_id=template_id,
                    version=new_version,
                    content=payload.system_prompt,
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
            # commit happens at __aexit__ if no exception is raised.

        # ─── Post-commit: best-effort NOTIFY ───
        try:
            await emit_notify(event_id, event_type)
        except Exception:
            _log.warning(
                "event_bus_notify_failed_will_be_polled",
                event_id=str(event_id),
                event_type=event_type,
            )

        _log.info(
            "agent_template_updated",
            template_id=str(updated.id),
            name=updated.name,
            archetype=updated.archetype,
            old_version=old_version,
            new_version=new_version,
            bump_version=bump_version,
        )

        # `updated_at` Sprint 1 — la table n'a pas de colonne dédiée. La
        # valeur retournée correspond à l'instant de fin de transaction côté
        # service (≈ commit DB à la milliseconde près). Au besoin, l'état
        # exact est retrouvable via ``MAX(prompts.created_at) WHERE agent_template_id = ?``.
        return UpdateTemplateResponse(
            template_id=updated.id,
            name=updated.name,
            archetype=updated.archetype,
            version=updated.version,
            config=updated.config,
            updated_at=datetime.now(tz=UTC),
        )


__all__ = ["AgentRegistryService"]
