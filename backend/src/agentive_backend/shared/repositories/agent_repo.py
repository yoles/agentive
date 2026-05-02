"""Public API surface for :class:`AgentTemplate` and :class:`AgentInstance`.

ALL DB access must go through these classes.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from agentive_backend.infra.db.models import AgentInstance, AgentTemplate
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
        async with self.with_tenant(tenant_id) as session:
            template = AgentTemplate(
                name=name,
                archetype=archetype,
                version=version,
                config=config,
                tenant_id=tenant_id,
            )
            session.add(template)
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
