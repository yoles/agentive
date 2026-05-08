"""Public API surface for :class:`Prompt`. ALL DB access must go through this class.

Prompts are versioned — each edit creates a new row (immutable history).
The unique constraint ``uq_prompt_template_version`` enforces this at DB
level.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from agentive_backend.infra.db.models import Prompt
from agentive_backend.shared.repositories.base import BaseRepo


class PromptRepo(BaseRepo):
    """Public API surface for Prompt. ALL DB access must go through this class."""

    async def get_by_id(self, prompt_id: UUID, *, tenant_id: UUID | None = None) -> Prompt | None:
        async with self.with_tenant(tenant_id) as session:
            return await session.get(Prompt, prompt_id)

    async def get_by_template_version(
        self,
        agent_template_id: UUID,
        version: int,
        *,
        tenant_id: UUID | None = None,
    ) -> Prompt | None:
        async with self.with_tenant(tenant_id) as session:
            stmt = select(Prompt).where(
                Prompt.agent_template_id == agent_template_id,
                Prompt.version == version,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def create(
        self,
        *,
        agent_template_id: UUID,
        version: int,
        content: str,
        tenant_id: UUID | None = None,
    ) -> Prompt:
        async with self.with_tenant(tenant_id) as session:
            prompt = Prompt(
                agent_template_id=agent_template_id,
                version=version,
                content=content,
                tenant_id=tenant_id,
            )
            session.add(prompt)
            await session.flush()
            await session.refresh(prompt)
            return prompt
