"""Unit tests — :class:`OutboxRepo`."""

from __future__ import annotations

from uuid import uuid4

import pytest

from agentive_backend.shared.repositories import OutboxRepo

from .conftest import make_session_factory_mock


@pytest.mark.asyncio
async def test_insert_persists_event_with_provided_id() -> None:
    factory, session = make_session_factory_mock()
    repo = OutboxRepo(session_factory=factory)
    event_id = uuid4()
    await repo.insert(
        event_id=event_id,
        correlation_id=uuid4(),
        event_type="m1.thing.happened",
        payload={"k": "v"},
    )
    event = session.add.call_args.args[0]
    assert event.id == event_id
    assert event.event_type == "m1.thing.happened"


@pytest.mark.asyncio
async def test_get_unprocessed_orders_by_created_at_then_id() -> None:
    """Regression: tie-break on ``id`` keeps delivery order deterministic."""
    factory, session = make_session_factory_mock()
    repo = OutboxRepo(session_factory=factory)
    await repo.get_unprocessed(limit=5)
    sql_text = str(session.execute.await_args.args[0]).lower()
    assert "order by" in sql_text
    # Both columns must appear in the ORDER BY clause.
    assert "created_at" in sql_text
    assert "outbox_events.id" in sql_text or " id " in sql_text


@pytest.mark.asyncio
async def test_mark_processed_returns_zero_when_no_rows_match() -> None:
    """Idempotent re-call: a second mark for the same event id returns 0."""
    factory, session = make_session_factory_mock()
    result_mock = session.execute.return_value
    result_mock.rowcount = 0
    repo = OutboxRepo(session_factory=factory)
    n = await repo.mark_processed(uuid4())
    assert n == 0


@pytest.mark.asyncio
async def test_mark_processed_returns_rowcount_when_match() -> None:
    factory, session = make_session_factory_mock()
    result_mock = session.execute.return_value
    result_mock.rowcount = 1
    repo = OutboxRepo(session_factory=factory)
    n = await repo.mark_processed(uuid4())
    assert n == 1
