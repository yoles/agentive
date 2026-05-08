"""Public API surface for :class:`User`. ALL DB access must go through this class."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from agentive_backend.infra.db.models import User
from agentive_backend.shared.repositories.base import BaseRepo


class UserRepo(BaseRepo):
    """Public API surface for User. ALL DB access must go through this class."""

    async def get_by_id(self, user_id: UUID, *, tenant_id: UUID | None = None) -> User | None:
        async with self.with_tenant(tenant_id) as session:
            return await session.get(User, user_id)

    async def get_by_email(self, email: str, *, tenant_id: UUID | None = None) -> User | None:
        # Normalize so callers don't get bitten by case or whitespace mismatches
        # ("JOHN@..." vs "john@..."). Emails are case-insensitive in practice.
        normalized = email.strip().lower()
        async with self.with_tenant(tenant_id) as session:
            stmt = select(User).where(User.email == normalized)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def create(
        self,
        *,
        email: str,
        name: str,
        role: str,
        tenant_id: UUID | None = None,
    ) -> User:
        normalized_email = email.strip().lower()
        async with self.with_tenant(tenant_id) as session:
            user = User(email=normalized_email, name=name, role=role, tenant_id=tenant_id)
            session.add(user)
            await session.flush()
            await session.refresh(user)
            return user
