"""Smoke test confirming the migrated_db + role fixtures wire up correctly."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_app_session_factory_can_select_users(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with app_session_factory() as session:
        result = await session.execute(text("SELECT COUNT(*) FROM users"))
        count = result.scalar_one()
        assert count >= 1, "seed user John should be present after migration"


@pytest.mark.asyncio
async def test_seed_role_has_bypassrls(
    seed_session_factory: async_sessionmaker[AsyncSession],
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Confirm BYPASSRLS by demonstrating the seed role sees a tenant row
    that the ``app`` role would filter out.

    Plant a single ``memory_chunks`` row with ``tenant_id = uuid_a`` via
    the seed role (BYPASSRLS lets it pass the WITH CHECK clause). Then:

    1. Read from the seed factory WITHOUT binding ``app.tenant_id`` →
       BYPASSRLS lets the SELECT see the row even though
       ``current_setting('app.tenant_id', true)`` is NULL. A non-bypassing
       role would either crash on the cast or return 0 rows.
    2. Read from the app factory WITHOUT binding → RLS policy filters all
       tenant-scoped rows out (the ``tenant_id IS NULL OR ...`` clause
       short-circuits to false for our planted row), so count is 0.

    The asymmetry between the two reads is the proof of BYPASSRLS — a
    dummy ``COUNT(*)`` over an empty table would pass for the wrong
    reason.
    """
    namespace_id = uuid4()
    tenant_a = uuid4()

    # First create a namespace so memory_chunks FK is satisfied. Use the
    # seed role so we can plant a tenant_a row across roles.
    async with seed_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO namespaces (id, name, type, embedding_backend, tenant_id) "
                "VALUES (:nid, :name, 'metier', 'cloud', :tid)"
            ),
            {"nid": str(namespace_id), "name": f"seed-bypass-{namespace_id}", "tid": str(tenant_a)},
        )
        await session.execute(
            text(
                "INSERT INTO memory_chunks (namespace_id, content, tenant_id) "
                "VALUES (:nsid, 'bypass-marker', :tid)"
            ),
            {"nsid": str(namespace_id), "tid": str(tenant_a)},
        )
        await session.commit()

    # Seed (BYPASSRLS) sees the planted row without any GUC binding.
    async with seed_session_factory() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM memory_chunks WHERE tenant_id = :tid"),
            {"tid": str(tenant_a)},
        )
        seed_count = result.scalar_one()

    # App (no BYPASSRLS) does NOT see the row without binding — the policy
    # filters tenant-scoped rows when ``app.tenant_id`` is unbound.
    async with app_session_factory() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM memory_chunks WHERE tenant_id = :tid"),
            {"tid": str(tenant_a)},
        )
        app_count = result.scalar_one()

    assert seed_count == 1, f"Seed role with BYPASSRLS should see 1 tenant_a row; got {seed_count}"
    assert app_count == 0, (
        f"App role without GUC binding should see 0 tenant_a rows (filtered by RLS); got {app_count}"
    )
