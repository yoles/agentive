"""Story 4.9 AC3/T3 — GRANT parity between the LangGraph checkpointer's own
table catalog and migration ``20260910_000001``'s GRANT block.

That migration's docstring makes a bet: ``checkpoint_migrations`` (created
by ``AsyncPostgresSaver.setup()`` alongside ``checkpoints``,
``checkpoint_blobs`` and ``checkpoint_writes``) is deliberately NOT granted
to ``agentive_app``, because the saver only ever reads it from inside
``setup()`` itself (run once, as ``agentive_owner``, by the migration) —
never during normal ``put``/``get``/``list`` traffic. That bet was verified
once by reading ``langgraph-checkpoint-postgres`` source by hand; nothing
re-verified it automatically.

This test closes that gap two ways, against the REAL testcontainer schema
(``migrated_db`` already ran every migration once, session-scoped — see
``test_request_fingerprint_migration.py`` on why no test re-runs
upgrade/downgrade against it):

1. The exact set of tables the checkpointer creates (introspected from
   ``information_schema.tables``, not re-typed) must equal the migration's
   own ``_CHECKPOINT_TABLES`` GRANT list plus exactly one deliberately
   excluded table, ``checkpoint_migrations``. A version bump that adds a
   FIFTH table fails this assertion loudly — never a silent skip — because
   it would be neither granted nor accounted for as the known exception.
2. Every table in ``_CHECKPOINT_TABLES`` actually carries the
   SELECT/INSERT/UPDATE/DELETE grant for ``agentive_app`` in
   ``information_schema.role_table_grants``, and ``checkpoint_migrations``
   carries none — so a change to either side (the saver's table catalog, or
   the migration's grant block) that breaks the pairing is caught here
   instead of at the next incident.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.agent_registry.conftest import (  # noqa: F401
    migrated_db,
    owner_session_factory,
)

pytestmark = pytest.mark.integration

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "20260910_000001_langgraph_checkpointer_setup.py"
)

# The one table `AsyncPostgresSaver.setup()` creates that the migration
# deliberately does NOT grant to `agentive_app` (module docstring, `upgrade()`
# GRANT loop). Named here, not just implied, so a future 5th checkpointer
# table has no way to sneak in as "the known exception" by accident.
_DELIBERATELY_UNGRANTED_TABLE = "checkpoint_migrations"

_GRANTED_PRIVILEGES = {"SELECT", "INSERT", "UPDATE", "DELETE"}


def _load_migration_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_langgraph_checkpointer_setup_migration", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _existing_checkpoint_tables(session: AsyncSession) -> set[str]:
    result = await session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name LIKE 'checkpoint%'"
        )
    )
    return {row[0] for row in result.all()}


async def _granted_privileges(session: AsyncSession, table: str, role: str) -> set[str]:
    result = await session.execute(
        text(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE table_schema = 'public' AND table_name = :table AND grantee = :role"
        ),
        {"table": table, "role": role},
    )
    return {row[0] for row in result.all()}


@pytest.mark.asyncio
async def test_checkpointer_table_catalog_matches_migration_expectations(
    migrated_db: str,  # noqa: F811
    owner_session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    """A checkpointer table that is neither in ``_CHECKPOINT_TABLES`` nor
    the one named exception is a version bump this migration has not been
    re-verified against — fail loudly, never silently pass or skip."""
    migration = _load_migration_module()
    expected = set(migration._CHECKPOINT_TABLES) | {_DELIBERATELY_UNGRANTED_TABLE}

    async with owner_session_factory() as session:
        actual = await _existing_checkpoint_tables(session)

    assert actual == expected, (
        "the checkpointer's real table catalog no longer matches this "
        f"migration's assumptions (found={actual!r}, expected={expected!r}) — "
        "a langgraph-checkpoint-postgres version bump likely added or "
        "renamed a table; re-verify its GRANT before updating this test"
    )


@pytest.mark.asyncio
async def test_checkpoint_tables_are_granted_to_app_role(
    migrated_db: str,  # noqa: F811
    owner_session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    migration = _load_migration_module()

    async with owner_session_factory() as session:
        for table in migration._CHECKPOINT_TABLES:
            granted = await _granted_privileges(session, table, "agentive_app")
            assert granted == _GRANTED_PRIVILEGES, (
                f"table {table!r} — expected {_GRANTED_PRIVILEGES}, got {granted}"
            )


@pytest.mark.asyncio
async def test_checkpoint_migrations_table_is_not_granted_to_app_role(
    migrated_db: str,  # noqa: F811
    owner_session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    """The saver reads ``checkpoint_migrations`` only from inside
    ``setup()`` (run once, as ``agentive_owner``) — never from ``put``/
    ``get``/``list``, which is why it is the one table this migration
    withholds from ``agentive_app``. If a future version starts reading it
    at runtime, this test is what would need to flip, not silently drift."""
    async with owner_session_factory() as session:
        granted = await _granted_privileges(session, _DELIBERATELY_UNGRANTED_TABLE, "agentive_app")

    assert granted == set()
