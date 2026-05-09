"""Public API surface for :class:`Prompt`. ALL DB access must go through this class.

Prompts are versioned — each edit creates a new row (immutable history).
The unique constraint ``uq_prompt_template_version`` enforces this at DB
level.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentive_backend.infra.db.models import Prompt
from agentive_backend.shared.exceptions import ConflictError
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
            return await self.create_in_session(
                session,
                agent_template_id=agent_template_id,
                version=version,
                content=content,
                tenant_id=tenant_id,
            )

    async def create_in_session(
        self,
        session: AsyncSession,
        *,
        agent_template_id: UUID,
        version: int,
        content: str,
        tenant_id: UUID | None = None,
    ) -> Prompt:
        """INSERT inside the caller's transaction — Story 2.2 atomicity (P-02 pattern).

        Used by ``AgentRegistryService.update_template`` to compose the
        prompt bump with the template UPDATE and the outbox event publish in
        a single transaction.

        Raises:
            ConflictError: If ``(agent_template_id, version)`` already exists.
                The repo translates :class:`IntegrityError` into a domain
                error so feature code stays free of ``sqlalchemy`` imports
                (``import-linter`` Contract 3).
        """
        prompt = Prompt(
            agent_template_id=agent_template_id,
            version=version,
            content=content,
            tenant_id=tenant_id,
        )
        session.add(prompt)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise ConflictError(
                detail=(
                    f"Prompt for template '{agent_template_id}' (version {version}) already exists"
                ),
                context={"agent_template_id": str(agent_template_id), "version": version},
            ) from exc
        await session.refresh(prompt)
        return prompt
