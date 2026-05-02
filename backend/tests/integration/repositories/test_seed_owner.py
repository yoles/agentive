"""AC6 — the migration must seed exactly one owner row (John)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.repositories import UserRepo

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_owner_john_seeded_with_tenant_id_null(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = UserRepo(session_factory=app_session_factory)
    user = await repo.get_by_email("john@agentive.local")
    assert user is not None, "seed user John must be present"
    assert user.name == "John"
    assert user.role == "owner"
    assert user.tenant_id is None
