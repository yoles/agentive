"""Cross-tenant isolation — ``audit_events`` table (defense-in-depth).

Two complementary guarantees protect ``audit_events``:

1. **Grant layer** — ``agentive_app`` (the runtime role) is REVOKEd from
   SELECT/INSERT/UPDATE/DELETE on ``audit_events``. Any attempt by a
   feature module to read or write directly via ``app_session_factory``
   surfaces as a Postgres ``permission denied`` (DBAPIError /
   ProgrammingError).
2. **RLS layer** — the ``tenant_isolation`` policy filters rows by
   ``current_setting('app.tenant_id')``. Even when the audit-admin role
   reads ``audit_events`` (which it has SELECT on), the policy enforces
   tenant scoping when ``with_tenant(tenant_id)`` is used.

Sprint 0 status: ``AuditEventRepo.record()`` raises ``NotImplementedError``
until Story 9.1 wires the production ``audit_admin`` writer. Tests use the
BYPASSRLS seed role to plant data, then assert each layer separately.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_audit_events_blocked_by_grants_for_app_role(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``agentive_app`` has no SELECT grant on audit_events → permission denied.

    This validates the **grant layer** of defense — RLS never even gets a
    chance to evaluate the row. The test is intentionally narrow: it does
    not measure how many rows would have been returned, only that the
    REVOKE blocks the read entirely.
    """
    tenant_a = uuid4()
    correlation_a = uuid4()

    # Plant one audit row via the BYPASSRLS seed role.
    async with seed_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO audit_events "
                "(actor, action, correlation_id, tenant_id) "
                "VALUES ('actor-a', 'test.tenant.a', :cid, :tid)"
            ),
            {"cid": str(correlation_a), "tid": str(tenant_a)},
        )
        await session.commit()

    # ``agentive_app`` has been REVOKEd from any operation on
    # ``audit_events`` (see migration ``20260419_000000_initial.py:529``).
    # Match on the Postgres "permission denied" code (``42501``) so the
    # assertion fails for the right reason and not for an unrelated
    # connection/network error.
    async with app_session_factory() as session:
        with pytest.raises(ProgrammingError) as excinfo:
            await session.execute(text("SELECT COUNT(*) FROM audit_events"))
        assert (
            "permission denied" in str(excinfo.value).lower()
            or getattr(excinfo.value.orig, "sqlstate", None) == "42501"
        )


@pytest.mark.asyncio
async def test_audit_events_filtered_by_rls_when_audit_admin_reads_with_tenant_binding(
    audit_admin_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``agentive_audit_admin`` + ``with_tenant(uuid_a)`` sees only tenant_a rows.

    This validates the **RLS layer** end-to-end on ``audit_events``: the
    audit-admin role has SELECT grant (no permission denied) but no
    BYPASSRLS, so the ``tenant_isolation`` policy applies. After binding
    ``set_config('app.tenant_id', uuid_a, true)``, only tenant_a rows are
    returned; the tenant_b row is silently filtered.
    """
    tenant_a, tenant_b = uuid4(), uuid4()
    correlation_a, correlation_b = uuid4(), uuid4()

    # Plant one row per tenant via the BYPASSRLS seed role — guarantees
    # the rows are present regardless of RLS state.
    async with seed_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO audit_events "
                "(actor, action, correlation_id, tenant_id) "
                "VALUES ('actor-a', 'test.tenant.a', :cid, :tid)"
            ),
            {"cid": str(correlation_a), "tid": str(tenant_a)},
        )
        await session.execute(
            text(
                "INSERT INTO audit_events "
                "(actor, action, correlation_id, tenant_id) "
                "VALUES ('actor-b', 'test.tenant.b', :cid, :tid)"
            ),
            {"cid": str(correlation_b), "tid": str(tenant_b)},
        )
        await session.commit()

    # Audit-admin reads with tenant_a binding → only tenant_a rows visible.
    async with audit_admin_session_factory() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_a)},
        )
        result = await session.execute(
            text("SELECT correlation_id FROM audit_events ORDER BY correlation_id")
        )
        visible = [row[0] for row in result.all()]

    assert correlation_a in visible
    assert correlation_b not in visible
    assert len(visible) == 1, (
        f"Expected exactly 1 row visible under tenant_a binding, got {len(visible)}"
    )

    # Cleanup: audit_events is excluded from clean_repository_tables
    # (partitions make TRUNCATE expensive). Delete this test's rows under
    # the seed role (BYPASSRLS allows DELETE, REVOKE does not apply).
    async with seed_session_factory() as session:
        await session.execute(
            text("DELETE FROM audit_events WHERE correlation_id IN (:a, :b)"),
            {"a": str(correlation_a), "b": str(correlation_b)},
        )
        await session.commit()
