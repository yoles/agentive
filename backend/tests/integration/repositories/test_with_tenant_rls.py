"""AC2 + AC3 — end-to-end RLS isolation via :meth:`BaseRepo.with_tenant`.

Plants two namespaces under tenant_a and tenant_b via the BYPASSRLS seed
role, then confirms an :class:`agentive_app` session bound to tenant_a
sees only the tenant_a row, and similarly for tenant_b.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.repositories import NamespaceRepo

pytestmark = pytest.mark.integration


async def _seed_two_namespaces(
    seed_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_a: str,
    tenant_b: str,
) -> tuple[str, str]:
    """Insert one namespace per tenant via the BYPASSRLS seed role.

    Returns the two namespace ids as strings (UUID stringified).
    """
    async with seed_factory() as session:
        result = await session.execute(
            text(
                "INSERT INTO namespaces (name, type, tenant_id) "
                "VALUES (:n, 'metier', :t) RETURNING id"
            ),
            {"n": "ns-tenant-a", "t": tenant_a},
        )
        id_a = str(result.scalar_one())
        result = await session.execute(
            text(
                "INSERT INTO namespaces (name, type, tenant_id) "
                "VALUES (:n, 'metier', :t) RETURNING id"
            ),
            {"n": "ns-tenant-b", "t": tenant_b},
        )
        id_b = str(result.scalar_one())
        await session.commit()
    return id_a, id_b


@pytest.mark.asyncio
async def test_with_tenant_a_sees_only_tenant_a_rows(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_a, tenant_b = uuid4(), uuid4()
    await _seed_two_namespaces(seed_session_factory, tenant_a=str(tenant_a), tenant_b=str(tenant_b))

    repo = NamespaceRepo(session_factory=app_session_factory)
    rows = await repo.list_by_type("metier", tenant_id=tenant_a)
    assert len(rows) == 1
    assert rows[0].name == "ns-tenant-a"
    assert str(rows[0].tenant_id) == str(tenant_a)


@pytest.mark.asyncio
async def test_with_tenant_b_sees_only_tenant_b_rows(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_a, tenant_b = uuid4(), uuid4()
    await _seed_two_namespaces(seed_session_factory, tenant_a=str(tenant_a), tenant_b=str(tenant_b))

    repo = NamespaceRepo(session_factory=app_session_factory)
    rows = await repo.list_by_type("metier", tenant_id=tenant_b)
    assert len(rows) == 1
    assert rows[0].name == "ns-tenant-b"
    assert str(rows[0].tenant_id) == str(tenant_b)


@pytest.mark.asyncio
async def test_insert_with_mismatched_tenant_id_is_rejected_by_with_check(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """RLS WITH CHECK clause must reject INSERTs targeting another tenant.

    The repo's :meth:`with_tenant` sets ``app.tenant_id = bound_tenant`` AND
    creates rows with ``tenant_id = bound_tenant`` — that path never
    violates the policy. To reproduce a cross-tenant write attempt, we
    bind ``app.tenant_id = bound_tenant`` then manually INSERT a row that
    targets ``tenant_id = other_tenant``. The WITH CHECK clause rejects it.
    """
    bound_tenant = uuid4()
    other_tenant = uuid4()

    repo = NamespaceRepo(session_factory=app_session_factory)
    # Postgres reports WITH CHECK violations as SQLSTATE 42501 (insufficient
    # privilege) wrapped by psycopg as either ``IntegrityError`` or
    # ``ProgrammingError`` depending on driver version. Match on both,
    # then verify the message identifies an RLS policy violation so a
    # network error or syntax bug doesn't masquerade as a passing test.
    with pytest.raises((IntegrityError, ProgrammingError)) as excinfo:
        async with repo.with_tenant(bound_tenant) as session:
            await session.execute(
                text(
                    "INSERT INTO namespaces (name, type, tenant_id) "
                    "VALUES ('mismatch', 'metier', :other)"
                ),
                {"other": str(other_tenant)},
            )
            await session.flush()
    msg = str(excinfo.value).lower()
    assert "row-level security" in msg or "row level security" in msg, (
        f"Expected RLS policy violation, got: {excinfo.value!r}"
    )


@pytest.mark.asyncio
async def test_with_tenant_none_filters_out_tenant_bound_rows(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """tenant_id=None + only-tenant-bound rows → 0 rows visible.

    With ``app.tenant_id`` unset, ``current_setting('app.tenant_id', true)``
    returns NULL. The policy USING clause then evaluates
    ``tenant_id IS NULL OR tenant_id = NULL::uuid`` — the first disjunct
    is FALSE for tenant-bound rows; the second is NULL (UNKNOWN). The
    WHERE filter treats the overall NULL result as false and silently
    hides the row. No exception is raised — the result is just empty.
    """
    tenant_a, tenant_b = uuid4(), uuid4()
    await _seed_two_namespaces(seed_session_factory, tenant_a=str(tenant_a), tenant_b=str(tenant_b))

    repo = NamespaceRepo(session_factory=app_session_factory)
    rows = await repo.list_by_type("metier", tenant_id=None)
    assert rows == []
