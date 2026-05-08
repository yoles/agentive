"""AC2 — publish() inserts into outbox in the caller's transaction."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.event_bus import publish
from agentive_backend.shared.event_bus.exceptions import (
    InvalidEventTypeError,
    MissingCorrelationIdError,
)

pytestmark = pytest.mark.integration


async def _count_rows(session: AsyncSession) -> int:
    result = await session.execute(text("SELECT COUNT(*) FROM outbox_events"))
    return int(result.scalar_one())


async def test_publish_then_commit_persists_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cid = uuid4()
    async with session_factory() as session:
        before = await _count_rows(session)
        event_id = await publish(
            "system.app.started",
            {"version": "0.1.0"},
            session=session,
            correlation_id=cid,
        )
        await session.commit()

    async with session_factory() as session:
        after = await _count_rows(session)
        result = await session.execute(
            text("SELECT correlation_id, event_type, payload FROM outbox_events WHERE id = :id"),
            {"id": str(event_id)},
        )
        row = result.mappings().one()

    assert after == before + 1
    assert str(row["correlation_id"]) == str(cid)
    assert row["event_type"] == "system.app.started"
    assert row["payload"] == {"version": "0.1.0"}


async def test_publish_with_rollback_does_not_persist(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cid = uuid4()
    async with session_factory() as session:
        await publish(
            "system.app.started",
            {"version": "0.1.0"},
            session=session,
            correlation_id=cid,
        )
        await session.rollback()

    async with session_factory() as session:
        rows = (await session.execute(text("SELECT id FROM outbox_events"))).all()

    assert rows == [], "rollback must drop the outbox row (atomicity guarantee)"


async def test_publish_rejects_invalid_event_type(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        with pytest.raises(InvalidEventTypeError):
            await publish(
                "BadName",
                {},
                session=session,
                correlation_id=uuid4(),
            )


async def test_publish_without_correlation_id_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No bound ContextVar AND no explicit correlation_id → fail fast."""
    from agentive_backend.shared.correlation import _correlation_id_var

    # Make sure the ContextVar is unbound for this test.
    token = _correlation_id_var.set(None)
    try:
        async with session_factory() as session:
            with pytest.raises(MissingCorrelationIdError):
                await publish(
                    "system.app.started",
                    {"version": "0.1.0"},
                    session=session,
                )
    finally:
        _correlation_id_var.reset(token)


async def test_publish_accepts_pydantic_basemodel(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from agentive_backend.shared.contracts.events import SystemStartedEvent

    cid = uuid4()
    async with session_factory() as session:
        event_id = await publish(
            SystemStartedEvent.event_type,
            SystemStartedEvent(version="0.1.0"),
            session=session,
            correlation_id=cid,
        )
        await session.commit()

    async with session_factory() as session:
        result = await session.execute(
            text("SELECT payload FROM outbox_events WHERE id = :id"),
            {"id": str(event_id)},
        )
        payload = result.scalar_one()
    assert payload == {"version": "0.1.0"}
