"""AC1 — verify the migrated DB shape matches Story 1.5 expectations.

Reads ``information_schema.tables`` and ``information_schema.columns`` via
the ``agentive_owner`` session (only role with the SELECT privilege on
``information_schema.columns`` for non-public catalogs).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration

_CORE_TABLES = ("users", "sessions", "feature_flags", "namespaces")
_TENANT_READY_TABLES = (
    "memory_chunks",
    "chunk_embeddings",
    "workflows",
    "workflow_runs",
    "agent_templates",
    "agent_instances",
    "prompts",
    "outbox_events",
    "audit_events",
)
_AUDIT_PARTITIONS = (
    "audit_events_2026_04",
    "audit_events_2026_05",
    "audit_events_2026_06",
    "audit_events_2026_07",
    "audit_events_2026_08",
    "audit_events_2026_09",
    "audit_events_2026_10",
    "audit_events_2026_11",
    "audit_events_2026_12",
    "audit_events_2027_01",
    "audit_events_2027_02",
    "audit_events_2027_03",
    "audit_events_default",
)


async def _table_exists(session: AsyncSession, table: str) -> bool:
    result = await session.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": table},
    )
    return result.scalar_one_or_none() is not None


async def _column_exists(session: AsyncSession, table: str, column: str) -> bool:
    result = await session.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    )
    return result.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_core_tables_exist(
    owner_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with owner_session_factory() as session:
        for table in _CORE_TABLES:
            assert await _table_exists(session, table), f"missing core table {table}"


@pytest.mark.asyncio
async def test_tenant_ready_tables_exist_with_tenant_id_column(
    owner_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with owner_session_factory() as session:
        for table in _TENANT_READY_TABLES:
            assert await _table_exists(session, table), f"missing table {table}"
            assert await _column_exists(session, table, "tenant_id"), (
                f"table {table} is missing the tenant_id column"
            )


@pytest.mark.asyncio
async def test_audit_event_monthly_partitions_exist(
    owner_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with owner_session_factory() as session:
        for partition in _AUDIT_PARTITIONS:
            assert await _table_exists(session, partition), f"missing audit partition {partition}"


@pytest.mark.asyncio
async def test_rls_active_with_force_on_tenant_tables(
    owner_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with owner_session_factory() as session:
        for table in _TENANT_READY_TABLES:
            result = await session.execute(
                text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"),
                {"t": table},
            )
            row = result.one()
            assert row[0] is True, f"{table} should have RLS enabled"
            assert row[1] is True, f"{table} should have RLS forced (FORCE)"


@pytest.mark.asyncio
async def test_tenant_isolation_policy_present(
    owner_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with owner_session_factory() as session:
        result = await session.execute(
            text(
                "SELECT tablename FROM pg_policies "
                "WHERE policyname = 'tenant_isolation' AND schemaname = 'public'"
            )
        )
        tables_with_policy = {row[0] for row in result.all()}
        for table in _TENANT_READY_TABLES:
            assert table in tables_with_policy, (
                f"table {table} is missing the tenant_isolation policy"
            )
