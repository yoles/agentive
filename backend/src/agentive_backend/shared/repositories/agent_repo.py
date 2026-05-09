"""Public API surface for :class:`AgentTemplate` and :class:`AgentInstance`.

ALL DB access must go through these classes. Features import ``ConflictError``
from ``shared.exceptions`` — they MUST NOT import ``sqlalchemy.exc`` directly
(``import-linter`` Contract 3 forbids ``sqlalchemy`` imports from
``agentive_backend.features``). The repo catches ``IntegrityError`` and
re-raises a domain :class:`ConflictError` so callers stay sqlalchemy-free.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentive_backend.infra.db.models import AgentInstance, AgentTemplate
from agentive_backend.shared.exceptions import ConflictError
from agentive_backend.shared.repositories.base import BaseRepo


class AgentTemplateRepo(BaseRepo):
    """Public API surface for AgentTemplate. ALL DB access must go through this class."""

    async def get_by_id(
        self, template_id: UUID, *, tenant_id: UUID | None = None
    ) -> AgentTemplate | None:
        async with self.with_tenant(tenant_id) as session:
            return await session.get(AgentTemplate, template_id)

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
        only via the audit event ``m2.agent_template.updated`` row in the
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

    async def create(
        self,
        *,
        template_id: UUID,
        template_version: int,
        snapshot: dict[str, Any],
        workflow_run_id: UUID | None = None,
        tenant_id: UUID | None = None,
    ) -> AgentInstance:
        async with self.with_tenant(tenant_id) as session:
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
