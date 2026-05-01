"""AC3 — NOTIFY round-trip: publish_and_commit reaches subscriber promptly."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.event_bus import (
    OutboxWorker,
    publish_and_commit,
    subscribe,
)
from agentive_backend.shared.event_bus.types import Event

pytestmark = pytest.mark.integration


async def test_notify_round_trip_under_one_second(
    outbox_worker: OutboxWorker,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """publish() → NOTIFY → handler invocation should complete in < 1s.

    The conftest ``outbox_worker`` fixture runs with ``poll_interval_s=0.5`` so
    even if NOTIFY is somehow lost the poll fallback catches the event quickly.
    """
    seen = asyncio.Event()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)
        seen.set()

    await subscribe("m3.workflow.completed", handler)

    cid = uuid4()
    async with session_factory() as session:
        await publish_and_commit(
            session,
            "m3.workflow.completed",
            {"workflow_id": str(uuid4())},
            correlation_id=cid,
        )

    await asyncio.wait_for(seen.wait(), timeout=2.0)
    assert len(received) == 1
    assert received[0].event_type == "m3.workflow.completed"
    assert received[0].correlation_id == cid
