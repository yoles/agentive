"""Integration test fixtures for ``shared.event_bus``.

Bootstraps a minimal schema (``outbox_events`` only) in the testcontainers
Postgres instance — running the full Alembic migration would also create
``agentive_app`` / ``agentive_audit_admin`` roles, RLS policies, and audit
partitions, which is unnecessary surface area for testing the event bus.

The shape of ``outbox_events`` matches the production migration
(``backend/alembic/versions/20260419_000000_initial.py:352-376``). When that
schema evolves (cf. Story 1.4 AC1 escape hatch), keep the inline DDL below in
sync.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress

import psycopg
import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agentive_backend.shared.event_bus import OutboxWorker
from agentive_backend.shared.event_bus.subscriber import _clear_subscriptions_for_tests

_OUTBOX_DDL = """
CREATE TABLE IF NOT EXISTS outbox_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id UUID NOT NULL,
    event_type VARCHAR(255) NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ NULL,
    tenant_id UUID NULL
);
CREATE INDEX IF NOT EXISTS ix_outbox_unprocessed
    ON outbox_events (created_at) WHERE processed_at IS NULL;
"""


@pytest.fixture(scope="session")
def event_bus_dsn(database_url: str) -> Iterator[str]:
    """Route the publisher + worker DSN at the testcontainer Postgres (P18).

    Pydantic v2 ``@computed_field`` is a class-level data descriptor — instance
    ``__dict__`` does NOT shadow it. So we monkeypatch ``_psycopg_dsn()``
    directly, which is the single function the publisher and outbox worker
    use to derive the connection string. This keeps the override scoped and
    obvious instead of fighting Pydantic internals.

    Cleaner future alternative (Story 1.5+) : provision an ``agentive_app``
    role inside the testcontainer schema setup so the production
    ``database_url`` computed_field naturally points at it via env vars.
    """
    from agentive_backend.shared.event_bus import publisher as _publisher_mod

    canonical = str(database_url).replace("postgresql+psycopg2://", "postgresql+psycopg://", 1)
    psycopg_dsn = canonical.replace("postgresql+psycopg://", "postgresql://", 1)

    original_dsn_fn = _publisher_mod._psycopg_dsn
    _publisher_mod._psycopg_dsn = lambda: psycopg_dsn  # type: ignore[assignment]

    # Mirror the override on the worker module — it imports `_psycopg_dsn` by
    # name at import time, so reassigning `publisher._psycopg_dsn` doesn't
    # update the worker's binding. Re-bind explicitly.
    from agentive_backend.shared.event_bus import outbox as _outbox_mod

    original_outbox_dsn_fn = _outbox_mod._psycopg_dsn
    _outbox_mod._psycopg_dsn = lambda: psycopg_dsn  # type: ignore[assignment]

    os.environ["DATABASE_URL"] = canonical
    try:
        yield canonical
    finally:
        _publisher_mod._psycopg_dsn = original_dsn_fn
        _outbox_mod._psycopg_dsn = original_outbox_dsn_fn
        os.environ.pop("DATABASE_URL", None)


@pytest.fixture(scope="session")
def outbox_schema(event_bus_dsn: str) -> Iterator[None]:
    """Create the ``outbox_events`` table once per session."""
    sync_dsn = event_bus_dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(sync_dsn, autocommit=True) as conn:
        conn.execute(_OUTBOX_DDL)
    yield
    with psycopg.connect(sync_dsn, autocommit=True) as conn, suppress(Exception):
        conn.execute("DROP TABLE IF EXISTS outbox_events CASCADE")


@pytest.fixture
async def session_factory(
    event_bus_dsn: str, outbox_schema: None
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Per-test session factory bound to the testcontainer.

    Opens a fresh engine per test to avoid event-loop reuse issues across
    pytest-asyncio function-scoped loops.
    """
    engine = create_async_engine(event_bus_dsn, pool_pre_ping=True)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    yield factory
    await engine.dispose()


@pytest.fixture(autouse=True)
async def clean_outbox_table(event_bus_dsn: str, outbox_schema: None) -> AsyncIterator[None]:
    """Truncate ``outbox_events`` before every test for isolation."""
    sync_dsn = event_bus_dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(sync_dsn, autocommit=True) as conn:
        conn.execute("TRUNCATE TABLE outbox_events RESTART IDENTITY")
    yield


@pytest.fixture(autouse=True)
def clean_subscriptions() -> Iterator[None]:
    """Reset the in-process subscriber registry before/after each test."""
    _clear_subscriptions_for_tests()
    yield
    _clear_subscriptions_for_tests()


@pytest.fixture
async def outbox_worker(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[OutboxWorker]:
    """Started worker for the duration of a test, stopped on teardown.

    Use a faster poll interval than production so tests don't wait 5s on the
    NOTIFY fallback path.
    """
    worker = OutboxWorker(session_factory=session_factory, poll_interval_s=0.5)
    await worker.start()
    yield worker
    await worker.stop()
